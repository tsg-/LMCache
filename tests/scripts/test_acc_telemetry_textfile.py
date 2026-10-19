# SPDX-License-Identifier: Apache-2.0
"""Tests for the ACC telemetry node_exporter textfile collector."""

from __future__ import annotations

# Standard
import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

# Third Party
import pytest


SCRIPT = (
    Path(__file__).parents[2]
    / "docs/design/v1/platform/ipu-poc/instrumentation/host/bin"
    / "acc_telemetry_textfile.py"
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("acc_telemetry_textfile", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collector = _load_module()


def _proto_message(**values: int | float | bool) -> SimpleNamespace:
    """Build a protobuf-like message with the supplied scalar fields."""
    return SimpleNamespace(
        DESCRIPTOR=SimpleNamespace(
            fields=[SimpleNamespace(name=name) for name in values]
        ),
        **values,
    )


class _FakeReader:
    """Return one complete telemetry sample through the loop's reader boundary."""

    def __init__(self) -> None:
        """Initialize the fixed sample and call counters."""
        self.calls = 0
        self.reconnects = 0

    def collect(self) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
        """Return the counters from one RPC response."""
        self.calls += 1
        return (
            {"bytes_from_ulp_rc": 123, "bytes_to_ulp": 456},
            {
                "global": {
                    "bytes_from_ulp_rc": 123,
                    "bytes_to_ulp": 456,
                },
                "rx": {"rx_dropped_conn_rsrc": 3},
                "tx": {"tx_retransmitted": 5},
                "rue": {"rue_events": 9},
            },
        )

    def reconnect(self) -> None:
        """Record a reconnect requested by a failed collection."""
        self.reconnects += 1


class _FailOnceReader(_FakeReader):
    """Fail the first collection, then return a complete ACC sample."""

    def collect(self) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
        """Raise once to exercise the persistent loop's recovery path."""
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient gRPC failure")
        return super().collect()


def test_render_metrics_exports_both_rc_byte_counters() -> None:
    """Render both ACC RC byte counters as Prometheus counters."""
    rendered = collector.render_metrics(
        {"bytes_from_ulp_rc": 123, "bytes_to_ulp": 456},
        success=True,
        timestamp=100,
    )

    assert "# TYPE acc_telemetry_bytes_total counter" in rendered
    assert (
        'acc_telemetry_bytes_total{counter="bytes_from_ulp_rc",ulp="rdma"} 123'
        in rendered
    )
    assert (
        'acc_telemetry_bytes_total{counter="bytes_to_ulp",ulp="rdma"} 456' in rendered
    )
    assert "acc_telemetry_collector_success 1" in rendered
    assert "acc_telemetry_collector_timestamp_seconds 100" in rendered


def test_render_metrics_labels_a_shadow_collector_per_acc() -> None:
    """A shadow collector cannot collide with another ACC's textfile series."""
    rendered = collector.render_metrics(
        {"bytes_from_ulp_rc": 123, "bytes_to_ulp": 456},
        success=True,
        timestamp=100,
        acc="acc1",
    )

    assert (
        'acc_telemetry_bytes_total{acc="acc1",counter="bytes_from_ulp_rc",'
        'ulp="rdma"} 123'
    ) in rendered


def test_extract_telemetry_fields_reads_every_numeric_section() -> None:
    """A missing section would silently remove a Falcon diagnostic counter."""
    fields = collector.extract_telemetry_fields(
        SimpleNamespace(
            global_counters=_proto_message(bytes_from_ulp_rc=123),
            rx_counters=_proto_message(rx_dropped_conn_rsrc=3, enabled=True),
            tx_counters=_proto_message(tx_retransmitted=5.0),
            rue_counters=_proto_message(rue_events=9),
        )
    )

    assert fields == {
        "global": {"bytes_from_ulp_rc": 123},
        "rx": {"rx_dropped_conn_rsrc": 3},
        "tx": {"tx_retransmitted": 5.0},
        "rue": {"rue_events": 9},
    }


def test_normalize_telemetry_response_preserves_byte_counter_compatibility() -> None:
    """Replacing tele_cli must retain the existing byte-counter metric values."""
    counters, fields = collector.normalize_telemetry_response(
        SimpleNamespace(
            global_counters=_proto_message(
                bytes_from_ulp_rc=123,
                bytes_to_ulp=456,
            ),
            rx_counters=_proto_message(rx_dropped_conn_rsrc=3),
            tx_counters=_proto_message(tx_retransmitted=5),
            rue_counters=_proto_message(rue_events=9),
        )
    )

    assert counters == {"bytes_from_ulp_rc": 123, "bytes_to_ulp": 456}
    assert fields["rx"] == {"rx_dropped_conn_rsrc": 3}


def test_collection_loop_writes_one_acc_sample_on_its_fixed_schedule(
    tmp_path: Path,
) -> None:
    """Serializing other ACCs must not delay this instance's next deadline."""
    reader = _FakeReader()
    waits: list[float] = []

    collector.run_collection_loop(
        reader,
        output_dir=tmp_path,
        output_filename="acc_grpc_acc1.prom",
        acc="acc1",
        interval_seconds=2.0,
        now=lambda: 100.5,
        monotonic=lambda: 0.0,
        wait=lambda delay: waits.append(delay) or True,
    )

    rendered = (tmp_path / "acc_grpc_acc1.prom").read_text(encoding="ascii")

    assert reader.calls == 1
    assert waits == [2.0]
    assert (
        'acc_telemetry_read_timestamp_seconds{acc="acc1",source="grpc"} 100.5'
        in rendered
    )
    assert (
        'acc_tele_field{acc="acc1",section="tx",field="tx_retransmitted"} 5'
    ) in rendered


def test_collection_loop_publishes_failure_then_reconnects_and_continues(
    tmp_path: Path,
) -> None:
    """A transient RPC failure cannot stop this ACC's persistent collector."""
    reader = _FailOnceReader()
    output = tmp_path / "acc_grpc_acc1.prom"
    snapshots: list[str] = []

    def wait(_delay: float) -> bool:
        """Capture each published sample and stop after the recovered sample."""
        snapshots.append(output.read_text(encoding="ascii"))
        return len(snapshots) == 2

    collector.run_collection_loop(
        reader,
        output_dir=tmp_path,
        output_filename=output.name,
        acc="acc1",
        interval_seconds=2.0,
        now=lambda: 100.5,
        monotonic=lambda: 0.0,
        wait=wait,
    )

    assert 'acc_telemetry_collector_success{acc="acc1"} 0' in snapshots[0]
    assert 'acc_telemetry_reconnects_total{acc="acc1"} 1' in snapshots[0]
    assert 'acc_telemetry_collector_success{acc="acc1"} 1' in snapshots[1]
    assert reader.reconnects == 1


def test_render_metrics_exports_all_falcon_counter_sections() -> None:
    """Dropping a gRPC section would leave existing dashboard series absent."""
    rendered = collector.render_metrics(
        {"bytes_from_ulp_rc": 123, "bytes_to_ulp": 456},
        success=True,
        timestamp=100,
        acc="acc1",
        tele_fields={
            "global": {
                "bytes_from_ulp_rc": 123,
                "bytes_to_ulp": 456,
                "cache_active": 7,
            },
            "rx": {"rx_dropped_conn_rsrc": 3},
            "tx": {"tx_retransmitted": 5},
            "rue": {"rue_events": 9},
        },
    )

    assert (
        'acc_tele_field{acc="acc1",section="global",field="cache_active"} 7'
    ) in rendered
    assert (
        'acc_tele_field{acc="acc1",section="rx",field="rx_dropped_conn_rsrc"} 3'
    ) in rendered
    assert (
        'acc_tele_field{acc="acc1",section="tx",field="tx_retransmitted"} 5'
    ) in rendered
    assert ('acc_tele_field{acc="acc1",section="rue",field="rue_events"} 9') in rendered
    assert (
        'acc_telemetry_bytes_total{acc="acc1",counter="bytes_from_ulp_rc",'
        'ulp="rdma"} 123'
    ) in rendered


def test_render_failure_has_no_stale_counters() -> None:
    """A failed ACC query publishes only freshness and failure signals."""
    rendered = collector.render_metrics({}, success=False, timestamp=100)

    assert "acc_telemetry_collector_success 0" in rendered
    assert "acc_telemetry_bytes_total" not in rendered


def test_write_textfile_replaces_old_counters_after_failure(tmp_path: Path) -> None:
    """A failed collection must replace, rather than preserve, prior counters."""
    collector.write_textfile(
        tmp_path,
        collector.render_metrics(
            {"bytes_from_ulp_rc": 123, "bytes_to_ulp": 456},
            success=True,
            timestamp=100,
        ),
    )
    collector.write_textfile(
        tmp_path,
        collector.render_metrics({}, success=False, timestamp=101),
    )

    rendered = (tmp_path / collector.OUTPUT_FILENAME).read_text(encoding="ascii")

    assert "acc_telemetry_collector_success 0" in rendered
    assert "acc_telemetry_bytes_total" not in rendered


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"bytes_from_ulp_rc": 1},
        {"bytes_from_ulp_rc": -1, "bytes_to_ulp": 2},
        {"bytes_from_ulp_rc": "1", "bytes_to_ulp": 2},
    ],
)
def test_validate_counters_rejects_incomplete_or_invalid_values(
    values: dict[str, object],
) -> None:
    """Both ACC counters must be non-negative integers."""
    with pytest.raises(ValueError):
        collector.validate_counters(values)


def test_validate_counters_accepts_zero_values() -> None:
    """An idle link may legitimately return zero for either counter."""
    values = collector.validate_counters({"bytes_from_ulp_rc": 0, "bytes_to_ulp": 0})

    assert values == {"bytes_from_ulp_rc": 0, "bytes_to_ulp": 0}
