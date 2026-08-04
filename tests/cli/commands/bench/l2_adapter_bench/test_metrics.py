# SPDX-License-Identifier: Apache-2.0
"""Tests for the ``bench l2`` Prometheus endpoint.

The point of the endpoint is to expose a run WHILE it is running, so the
tests here scrape a result that is still being mutated rather than only
checking the final values.
"""

# Standard
from pathlib import Path
from unittest.mock import MagicMock
import json
import socket
import urllib.request

# Third Party
from prometheus_client import CollectorRegistry, generate_latest
import pytest

# First Party
from lmcache.cli.commands.bench.l2_adapter_bench.command import (
    add_l2_arguments,
    run_l2_adapter_bench,
)
from lmcache.cli.commands.bench.l2_adapter_bench.metrics import (
    BenchMetricsState,
    MetricsServerError,
    _BenchCollector,
    start_metrics_server,
)
from lmcache.cli.commands.bench.l2_adapter_bench.result import BenchMode, BenchResult

_MB = 1024 * 1024


def _result(operation: str = "Store", mode: BenchMode = BenchMode.SUSTAINED):
    return BenchResult(
        operation=operation,
        in_flight=4,
        num_keys=2,
        data_size_bytes=_MB,
        mode=mode,
    )


def _scrape(state: BenchMetricsState) -> str:
    registry = CollectorRegistry()
    registry.register(_BenchCollector(state))
    return generate_latest(registry).decode()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Collector
# ---------------------------------------------------------------------------


def test_nothing_registered_scrapes_cleanly() -> None:
    """A scrape before any phase starts must not error."""
    text = _scrape(BenchMetricsState())

    assert "lmcache_bench_l2_completed_submits" in text


def test_a_scrape_reflects_progress_mid_run() -> None:
    """The whole point: values must move while the phase is still running.

    A snapshot taken at registration time would report zeros forever, so
    this asserts the second scrape sees the intervening completions.
    """
    state = BenchMetricsState()
    result = _result()
    state.register(result.operation, result)

    first = _scrape(state)

    # Simulate what run_sustained_window does inside its harvest loop.
    for _ in range(3):
        result.completed_submits += 1
        result.success_counts.append(result.num_keys)
        result.submit_latencies.append(0.5)

    second = _scrape(state)

    assert 'completed_submits_total{operation="Store"} 0.0' in first
    assert 'completed_submits_total{operation="Store"} 3.0' in second
    # 3 submits x 2 keys.
    assert 'success_keys_total{operation="Store"} 6.0' in second
    # Rendered in exponential notation by the exposition format.
    assert 'success_bytes_total{operation="Store"} 6.291456e+06' in second


def test_latency_percentiles_are_exposed_in_seconds() -> None:
    """Prometheus convention is base units; the result reports ms."""
    state = BenchMetricsState()
    result = _result()
    state.register(result.operation, result)
    result.submit_latencies.extend([0.1] * 10)

    text = _scrape(state)

    # 100 ms -> 0.1 s at every quantile, since all samples are equal.
    assert 'submit_latency_seconds{operation="Store",quantile="0.5"} 0.1' in text
    assert 'submit_latency_seconds{operation="Store",quantile="0.99"} 0.1' in text


def test_phases_are_separate_series() -> None:
    """Store and Load must not share a counter.

    They run sequentially over the same endpoint, so a shared series
    would look like a counter reset and break rate().
    """
    state = BenchMetricsState()
    store, load = _result("Store"), _result("Load")
    state.register("Store", store)
    state.register("Load", load)
    store.completed_submits = 7
    load.completed_submits = 2

    text = _scrape(state)

    assert 'completed_submits_total{operation="Store"} 7.0' in text
    assert 'completed_submits_total{operation="Load"} 2.0' in text


def test_re_registering_a_phase_replaces_it() -> None:
    """A rounds-mode phase registers, then _strip_warmup makes a new
    result; the endpoint must not end up with two Store series."""
    state = BenchMetricsState()
    first, second = _result("Store"), _result("Store")
    state.register("Store", first)
    first.completed_submits = 5
    state.register("Store", second)
    second.completed_submits = 1

    text = _scrape(state)

    assert 'completed_submits_total{operation="Store"} 1.0' in text
    assert len(state.snapshot()) == 1


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


def test_the_endpoint_serves_over_http() -> None:
    state = BenchMetricsState()
    result = _result()
    state.register(result.operation, result)
    result.completed_submits = 11
    port = _free_port()

    start_metrics_server(port, state)
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
        body = resp.read().decode()

    assert 'completed_submits_total{operation="Store"} 11.0' in body


def test_a_bound_port_raises_rather_than_serving_nothing() -> None:
    """A silent failure would leave the operator watching a dead panel."""
    with socket.socket() as held:
        held.bind(("0.0.0.0", 0))
        held.listen(1)
        port = held.getsockname()[1]

        with pytest.raises(MetricsServerError, match="could not bind"):
            start_metrics_server(port, BenchMetricsState())


def test_the_registry_is_private_to_the_benchmark() -> None:
    """Only the benchmark's own series may appear in the scrape.

    The endpoint must not re-export whatever a transitively imported
    module registered on the process-global REGISTRY.
    """
    state = BenchMetricsState()
    port = _free_port()

    start_metrics_server(port, state)
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
        body = resp.read().decode()

    assert "lmcache_bench_l2_completed_submits" in body
    assert "python_gc_objects_collected" not in body
    assert "process_virtual_memory_bytes" not in body


# ---------------------------------------------------------------------------
# CLI integration
# ---------------------------------------------------------------------------


def _parse(*argv: str, adapter_json: str):
    # Standard
    import argparse

    parser = argparse.ArgumentParser()
    add_l2_arguments(parser)
    return parser.parse_args(["--l2-adapter", adapter_json, *argv])


def test_serve_metrics_defaults_to_off() -> None:
    args = _parse(adapter_json="{}")

    assert args.serve_metrics == 0


@pytest.mark.parametrize("port", ["0", "70000", "-1"])
def test_an_out_of_range_port_is_rejected(port: str, tmp_path: Path) -> None:
    """0 is argparse's "off" sentinel, so only 0 may mean off."""
    if port == "0":
        pytest.skip("0 is the documented off value, not an error")
    args = _parse(
        "--serve-metrics",
        port,
        "--only",
        "load",
        adapter_json=json.dumps({"type": "fs", "base_path": str(tmp_path)}),
    )

    with pytest.raises(SystemExit) as exc:
        run_l2_adapter_bench(MagicMock(), args)

    assert exc.value.code == 2


def test_a_run_with_serve_metrics_exposes_its_phases(tmp_path: Path) -> None:
    """End-to-end: a real run must leave scrapable per-phase series.

    Scraped after the run rather than during it -- the mid-run behaviour
    is covered above without a race.
    """
    port = _free_port()
    args = _parse(
        "--serve-metrics",
        str(port),
        "--key-prefix",
        "metrics-e2e",
        "--num-keys",
        "2",
        "--in-flight",
        "2",
        "--data-size-kb",
        "4",
        "--rounds",
        "1",
        "--warmup-rounds",
        "0",
        adapter_json=json.dumps({"type": "fs", "base_path": str(tmp_path / "l2")}),
    )

    run_l2_adapter_bench(MagicMock(), args)

    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
        body = resp.read().decode()

    assert 'operation="Store"' in body
    assert 'operation="Load"' in body
    # The store phase actually wrote: 1 round x 2 in-flight x 2 keys.
    assert 'success_keys_total{operation="Store"} 4.0' in body
