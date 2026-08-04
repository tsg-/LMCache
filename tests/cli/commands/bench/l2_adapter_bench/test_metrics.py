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
import threading
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
    PHASE_MEASURED,
    PHASE_WARMUP,
    PHASE_WARMUP_AND_MEASURED,
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


class _TripwireList(list):
    """A list that records every whole-list read performed on it.

    Appending is free, which is what the benchmark hot path does. Any
    operation that could walk the list -- iterating, indexing, slicing,
    ``len`` -- is counted, so a test can assert the scrape path performed
    none of them.
    """

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.scans = 0

    def __iter__(self):  # type: ignore[no-untyped-def]
        self.scans += 1
        return super().__iter__()

    def __len__(self) -> int:
        self.scans += 1
        return super().__len__()

    def __getitem__(self, item):  # type: ignore[no-untyped-def]
        self.scans += 1
        return super().__getitem__(item)


def _scrape(state: BenchMetricsState) -> str:
    registry = CollectorRegistry()
    registry.register(_BenchCollector(state))
    return generate_latest(registry).decode()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _series(text: str, metric: str, **labels: str) -> float:
    """Return the value of one sample, or raise if it is not present.

    Matches on the exact label set, so a test asserting
    ``phase="measured"`` cannot be satisfied by a warmup sample.
    """
    rendered = ",".join(f'{k}="{v}"' for k, v in labels.items())
    prefix = f"lmcache_bench_l2_{metric}{{{rendered}}} "
    for line in text.splitlines():
        if line.startswith(prefix):
            return float(line[len(prefix) :])
    raise AssertionError(f"no sample {prefix!r} in:\n{text}")


def _names(text: str, metric: str) -> list[str]:
    """Return the label blocks present for *metric*, in scrape order."""
    prefix = f"lmcache_bench_l2_{metric}{{"
    return [
        line[len(prefix) : line.index("}")]
        for line in text.splitlines()
        if line.startswith(prefix)
    ]


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
    state.register(result.operation, result, PHASE_MEASURED)

    first = _scrape(state)

    # Simulate what run_sustained_window does inside its harvest loop.
    for _ in range(3):
        result.completed_submits += 1
        result.record_success(result.num_keys)
        result.record_latency(0.5)

    second = _scrape(state)

    labels = {"operation": "Store", "phase": PHASE_MEASURED}
    assert _series(first, "completed_submits_total", **labels) == 0.0
    assert _series(second, "completed_submits_total", **labels) == 3.0
    # 3 submits x 2 keys.
    assert _series(second, "success_keys_total", **labels) == 6.0
    assert _series(second, "success_bytes_total", **labels) == float(6 * _MB)


def test_a_scrape_during_a_concurrent_harvest_stays_coherent() -> None:
    """Establishes the actual contract: stale, never torn, never falling.

    The runner mutates its result from the producer thread while the HTTP
    server thread collects. This drives both at once -- a sequential
    mutate-then-scrape test would pass even if the collector read a
    half-updated list. Each counter must be nondecreasing across scrapes,
    which is all rate() requires; exact agreement between counters is
    NOT promised, since the fields are read independently.
    """
    state = BenchMetricsState()
    result = _result()
    state.register(result.operation, result, PHASE_MEASURED)
    total = 2000
    stop = threading.Event()

    def produce() -> None:
        for _ in range(total):
            result.record_success(result.num_keys)
            result.record_latency(0.001)
            result.completed_submits += 1
        stop.set()

    producer = threading.Thread(target=produce)
    producer.start()
    labels = {"operation": "Store", "phase": PHASE_MEASURED}
    seen: list[float] = []
    try:
        while not stop.is_set() or len(seen) < 2:
            text = _scrape(state)
            seen.append(_series(text, "completed_submits_total", **labels))
            # A torn read would raise here rather than return a float.
            _series(text, "success_bytes_total", **labels)
            _series(text, "submit_latency_seconds_total", **labels)
    finally:
        producer.join(timeout=30)

    assert seen == sorted(seen), f"counter went backwards: {seen}"
    assert seen[-1] == float(total)


def test_cumulative_latency_is_exposed_in_seconds() -> None:
    """Prometheus convention is base units, and a sum, not a percentile.

    Percentiles were removed deliberately: they cannot be computed
    without walking the whole history. See
    ``test_a_scrape_does_not_walk_the_latency_history``.
    """
    state = BenchMetricsState()
    result = _result()
    state.register(result.operation, result, PHASE_MEASURED)
    for _ in range(10):
        result.record_latency(0.1)

    text = _scrape(state)

    labels = {"operation": "Store", "phase": PHASE_MEASURED}
    assert _series(text, "submit_latency_seconds_total", **labels) == pytest.approx(1.0)
    # A mean is recoverable from the pair; a distribution is not.
    result.completed_submits = 10
    text = _scrape(state)
    mean = _series(text, "submit_latency_seconds_total", **labels) / _series(
        text, "completed_submits_total", **labels
    )
    assert mean == pytest.approx(0.1)


def test_a_scrape_does_not_walk_the_latency_or_success_history() -> None:
    """The load-bearing regression test for LMCache-qge.

    The HTTP server shares the interpreter and the GIL with the single
    benchmark producer thread, so per-scrape work proportional to the run
    length stalls the thread being measured -- worst during exactly the
    long saturation runs the endpoint exists to observe. Before the fix a
    scrape sorted the full latency list (~0.5 s at 1M samples) and summed
    the full success list.

    Asserting on wall-clock time would be flaky on a loaded CI box, so
    this asserts the stronger structural property instead: the collector
    never touches either list at all.
    """
    state = BenchMetricsState()
    result = _result()
    result.submit_latencies = _TripwireList()
    result.success_counts = _TripwireList()
    state.register(result.operation, result, PHASE_MEASURED)
    for _ in range(5000):
        result.record_success(result.num_keys)
        result.record_latency(0.001)
        result.completed_submits += 1
    # record_* appends, which the tripwire does not count; prove the
    # recording path itself stayed clean before scraping.
    assert result.submit_latencies.scans == 0
    assert result.success_counts.scans == 0

    text = _scrape(state)

    assert result.submit_latencies.scans == 0, "scrape walked submit_latencies"
    assert result.success_counts.scans == 0, "scrape walked success_counts"
    # And the values are still right, read from the running totals.
    labels = {"operation": "Store", "phase": PHASE_MEASURED}
    assert _series(text, "completed_submits_total", **labels) == 5000.0
    assert _series(text, "success_keys_total", **labels) == 10000.0
    assert _series(text, "submit_latency_seconds_total", **labels) == pytest.approx(5.0)


def test_scrape_cost_is_flat_as_history_grows() -> None:
    """Same guarantee as above, stated as a cost comparison.

    The structural test is the primary guard; this one catches a
    regression that walks the history somewhere the tripwire list does
    not cover (a C-level sum, say). Ratio-based with a wide tolerance, so
    a slow or noisy machine does not fail it -- a genuine O(n) regression
    is a ~100x gap at these sizes, not a 5x one.
    """
    # Standard
    import time

    def cost(samples: int) -> float:
        state = BenchMetricsState()
        result = _result()
        state.register(result.operation, result, PHASE_MEASURED)
        for _ in range(samples):
            result.record_success(result.num_keys)
            result.record_latency(0.001)
            result.completed_submits += 1
        # Warm the code path so import/first-call cost is not charged to
        # the small case.
        _scrape(state)
        best = float("inf")
        for _ in range(5):
            start = time.perf_counter()
            _scrape(state)
            best = min(best, time.perf_counter() - start)
        return best

    small, large = cost(1_000), cost(200_000)

    # 200x the history. Sorting it would be far more than 20x the work.
    assert large < small * 20, f"scrape cost grew with history: {small=} {large=}"


def test_no_quantile_series_is_exposed() -> None:
    """The removal is the contract, so it needs a test of its own.

    Re-adding a percentile gauge would silently reintroduce a full sort
    on the scrape path. This fails if anyone does.
    """
    state = BenchMetricsState()
    result = _result()
    state.register(result.operation, result, PHASE_MEASURED)
    for _ in range(5):
        result.record_latency(0.02)

    text = _scrape(state)

    assert "quantile=" not in text


def test_operations_are_separate_series() -> None:
    """Store and Load must not share a counter.

    They run sequentially over the same endpoint, so a shared series
    would look like a counter reset and break rate().
    """
    state = BenchMetricsState()
    store, load = _result("Store"), _result("Load")
    state.register("Store", store, PHASE_MEASURED)
    state.register("Load", load, PHASE_MEASURED)
    store.completed_submits = 7
    load.completed_submits = 2

    text = _scrape(state)

    assert (
        _series(
            text, "completed_submits_total", operation="Store", phase=PHASE_MEASURED
        )
        == 7.0
    )
    assert (
        _series(text, "completed_submits_total", operation="Load", phase=PHASE_MEASURED)
        == 2.0
    )


def test_warmup_and_measured_are_separate_series() -> None:
    """Warmup is exposed but must never merge into the measured series.

    One series carrying warmup then measured values would show a counter
    reset that rate() misreads as a process restart.
    """
    state = BenchMetricsState()
    warmup, measured = _result("Store"), _result("Store")
    state.register("Store", warmup, PHASE_WARMUP)
    state.register("Store", measured, PHASE_MEASURED)
    warmup.completed_submits = 9
    measured.completed_submits = 4

    text = _scrape(state)

    assert (
        _series(text, "completed_submits_total", operation="Store", phase=PHASE_WARMUP)
        == 9.0
    )
    assert (
        _series(
            text, "completed_submits_total", operation="Store", phase=PHASE_MEASURED
        )
        == 4.0
    )


def test_re_registering_the_same_phase_replaces_it() -> None:
    """A rounds-mode phase registers, then _strip_warmup makes a new
    result; the endpoint must not end up with two Store series."""
    state = BenchMetricsState()
    first, second = _result("Store"), _result("Store")
    state.register("Store", first, PHASE_MEASURED)
    first.completed_submits = 5
    state.register("Store", second, PHASE_MEASURED)
    second.completed_submits = 1

    text = _scrape(state)

    assert (
        _series(
            text, "completed_submits_total", operation="Store", phase=PHASE_MEASURED
        )
        == 1.0
    )
    assert len(state.snapshot()) == 1


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------


def _get(port: int) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics", timeout=5) as resp:
        return resp.read().decode()


def test_the_endpoint_serves_over_http() -> None:
    state = BenchMetricsState()
    result = _result()
    state.register(result.operation, result, PHASE_MEASURED)
    result.completed_submits = 11
    port = _free_port()

    stop = start_metrics_server(port, state)
    try:
        body = _get(port)
    finally:
        stop()

    assert (
        _series(
            body, "completed_submits_total", operation="Store", phase=PHASE_MEASURED
        )
        == 11.0
    )


def test_shutdown_releases_the_port_and_is_idempotent() -> None:
    """Without this, every in-process run leaks a listening socket."""
    port = _free_port()

    stop = start_metrics_server(port, BenchMetricsState())
    stop()
    stop()

    # The port must be rebindable, which is the observable effect.
    second = start_metrics_server(port, BenchMetricsState())
    try:
        assert "lmcache_bench_l2_completed_submits" in _get(port)
    finally:
        second()


def test_a_bound_port_raises_rather_than_serving_nothing() -> None:
    """A silent failure would leave the operator watching a dead panel."""
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]

        with pytest.raises(MetricsServerError, match="could not bind"):
            start_metrics_server(port, BenchMetricsState())


def test_the_default_bind_reaches_the_socket_as_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unauthenticated endpoint must not be reachable off-box unless
    the operator asked for that explicitly.

    Asserts on the ``addr`` actually handed to ``prometheus_client``, not
    on the signature default: keeping ``address="127.0.0.1"`` while
    dropping ``addr=address`` from the call would still bind all
    interfaces, since all-interfaces is prometheus_client's own default.
    Probing a second local address instead would be platform-dependent
    -- which extra addresses are assignable varies.
    """
    # Third Party
    import prometheus_client

    seen: dict[str, object] = {}
    real = prometheus_client.start_http_server

    def spy(port, addr="0.0.0.0", registry=None):  # noqa: S104
        seen["addr"] = addr
        return real(port, addr=addr, registry=registry)

    monkeypatch.setattr(prometheus_client, "start_http_server", spy)

    stop = start_metrics_server(_free_port(), BenchMetricsState())
    try:
        assert seen["addr"] == "127.0.0.1"
    finally:
        stop()


def test_an_explicit_bind_address_is_honoured() -> None:
    """The address argument must reach the socket, not be ignored.

    Uses a TEST-NET-3 address that cannot be local, so a bind that
    ignored it would succeed instead of raising.
    """
    with pytest.raises(MetricsServerError, match="endpoint on 203.0.113.1"):
        start_metrics_server(_free_port(), BenchMetricsState(), address="203.0.113.1")


def test_the_cli_forwards_its_bind_address(tmp_path: Path) -> None:
    """--metrics-bind-address must reach start_metrics_server.

    A CLI that parsed the flag and then ignored it would bind loopback
    successfully and run; requesting an unassignable address makes the
    forwarding observable as the exit code.
    """
    args = _parse(
        "--serve-metrics",
        str(_free_port()),
        "--metrics-bind-address",
        "203.0.113.1",
        "--only",
        "load",
        adapter_json=json.dumps({"type": "fs", "base_path": str(tmp_path)}),
    )

    with pytest.raises(SystemExit) as exc:
        run_l2_adapter_bench(MagicMock(), args)

    assert exc.value.code == 2


def test_the_registry_is_private_to_the_benchmark() -> None:
    """Only the benchmark's own series may appear in the scrape.

    The endpoint must not re-export whatever a transitively imported
    module registered on the process-global REGISTRY.
    """
    port = _free_port()

    stop = start_metrics_server(port, BenchMetricsState())
    try:
        body = _get(port)
    finally:
        stop()

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
    assert args.metrics_bind_address == "127.0.0.1"


@pytest.mark.parametrize("port", ["70000", "-1"])
def test_an_out_of_range_port_is_rejected(port: str, tmp_path: Path) -> None:
    """0 is argparse's "off" sentinel, so only 0 may mean off."""
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


def test_a_bind_failure_never_constructs_the_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The endpoint binds before the adapter exists.

    The port-clash exit is outside the try/finally that closes the
    adapter, so constructing the adapter first would leak it on every
    clash. Asserted on the constructor rather than on a thread count: an
    adapter with no worker thread would satisfy a thread-count check
    while still being leaked.
    """
    # First Party
    from lmcache.v1.distributed import l2_adapters

    created = MagicMock()
    monkeypatch.setattr(l2_adapters, "create_l2_adapter", created)

    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        port = held.getsockname()[1]
        args = _parse(
            "--serve-metrics",
            str(port),
            "--only",
            "load",
            adapter_json=json.dumps({"type": "fs", "base_path": str(tmp_path)}),
        )

        with pytest.raises(SystemExit) as exc:
            run_l2_adapter_bench(MagicMock(), args)

        assert exc.value.code == 2
        created.assert_not_called()


def test_an_adapter_failure_releases_the_metrics_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mirror obligation of binding first.

    Adapter construction exits before the cleanup try/finally, so that
    path has to release the listener itself or every failed run leaks one.
    """
    # First Party
    from lmcache.v1.distributed import l2_adapters

    monkeypatch.setattr(
        l2_adapters,
        "create_l2_adapter",
        MagicMock(side_effect=RuntimeError("no adapter for you")),
    )
    port = _free_port()
    args = _parse(
        "--serve-metrics",
        str(port),
        "--only",
        "load",
        adapter_json=json.dumps({"type": "fs", "base_path": str(tmp_path)}),
    )

    with pytest.raises(SystemExit) as exc:
        run_l2_adapter_bench(MagicMock(), args)

    assert exc.value.code == 1
    # Rebindable, which it is not if the listener was left open.
    stop = start_metrics_server(port, BenchMetricsState())
    stop()


def _e2e_args(tmp_path: Path, port: int, *extra: str, prefix: str):
    return _parse(
        "--serve-metrics",
        str(port),
        "--key-prefix",
        prefix,
        "--num-keys",
        "2",
        "--in-flight",
        "2",
        "--data-size-kb",
        "4",
        *extra,
        adapter_json=json.dumps({"type": "fs", "base_path": str(tmp_path / "l2")}),
    )


def _capture_state(monkeypatch: pytest.MonkeyPatch) -> dict[str, BenchMetricsState]:
    """Intercept the state the CLI hands to the endpoint.

    The CLI releases the port in its cleanup block, so the series cannot
    be scraped over HTTP after the run; keeping a reference to the state
    lets the assertions read exactly what a scraper would have seen.
    """
    # First Party
    from lmcache.cli.commands.bench.l2_adapter_bench import metrics as metrics_mod

    captured: dict[str, BenchMetricsState] = {}
    real_start = metrics_mod.start_metrics_server

    def spy(port: int, state: BenchMetricsState, address: str = "127.0.0.1"):
        captured["state"] = state
        return real_start(port, state, address)

    monkeypatch.setattr(metrics_mod, "start_metrics_server", spy)
    return captured


def test_a_run_with_serve_metrics_exposes_its_phases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: a real run must leave scrapable per-phase series."""
    captured = _capture_state(monkeypatch)
    port = _free_port()
    args = _e2e_args(
        tmp_path, port, "--rounds", "1", "--warmup-rounds", "0", prefix="metrics-e2e"
    )

    run_l2_adapter_bench(MagicMock(), args)

    text = _scrape(captured["state"])
    labels = {"phase": PHASE_MEASURED}
    # The store phase actually wrote: 1 round x 2 in-flight x 2 keys.
    assert _series(text, "success_keys_total", operation="Store", **labels) == 4.0
    assert _series(text, "success_keys_total", operation="Load", **labels) == 4.0
    # The cleanup block released the port.
    with pytest.raises(OSError):
        _get(port)


def test_a_rounds_run_with_warmup_labels_the_combined_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rounds mode cannot separate warmup live, so it must not claim to.

    One BenchResult carries warmup and measured rounds; ``_strip_warmup``
    only splits them afterwards. A ``phase="measured"`` label on that
    live series would be a false claim, and the counter would appear to
    drop when the stripped copy replaced it.
    """
    captured = _capture_state(monkeypatch)
    port = _free_port()
    args = _e2e_args(
        tmp_path, port, "--rounds", "2", "--warmup-rounds", "1", prefix="metrics-warmup"
    )

    run_l2_adapter_bench(MagicMock(), args)

    text = _scrape(captured["state"])
    phases = {block.split(",")[1] for block in _names(text, "completed_submits_total")}
    assert phases == {f'phase="{PHASE_WARMUP_AND_MEASURED}"'}, text
    # 3 rounds (1 warmup + 2 measured) x 2 in-flight submits, and the
    # count must not have been rewound by _strip_warmup.
    assert (
        _series(
            text,
            "completed_submits_total",
            operation="Store",
            phase=PHASE_WARMUP_AND_MEASURED,
        )
        == 6.0
    )


def test_a_sustained_run_separates_its_warmup_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sustained mode has two distinct results, so both are published."""
    captured = _capture_state(monkeypatch)
    port = _free_port()
    args = _e2e_args(
        tmp_path,
        port,
        "--only",
        "store",
        "--duration-sec",
        "0.3",
        "--warmup-sec",
        "0.2",
        prefix="metrics-sustained",
    )

    run_l2_adapter_bench(MagicMock(), args)

    assert {key for key, _ in captured["state"].snapshot()} == {
        ("Store", PHASE_WARMUP),
        ("Store", PHASE_MEASURED),
    }
    text = _scrape(captured["state"])
    # Both series carry real work, and neither was folded into the other.
    warmup = _series(
        text, "completed_submits_total", operation="Store", phase=PHASE_WARMUP
    )
    measured = _series(
        text, "completed_submits_total", operation="Store", phase=PHASE_MEASURED
    )
    assert warmup > 0
    assert measured > 0
