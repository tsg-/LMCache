# SPDX-License-Identifier: Apache-2.0
"""Tests for the scrape-time ACC gRPC exporter."""

# Future
from __future__ import annotations

# Standard
from pathlib import Path
from types import ModuleType
import importlib.util
import sys
import threading

SCRIPT = (
    Path(__file__).parents[2]
    / "docs/design/v1/platform/ipu-poc/instrumentation/host/bin"
    / "acc_grpc_exporter.py"
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("acc_grpc_exporter", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Reader:
    def __init__(self, value: int, barrier: threading.Barrier) -> None:
        self.value = value
        self.barrier = barrier
        self.failed = False
        self.reconnects = 0

    def collect(self) -> tuple[dict[str, int], dict[str, dict[str, int]]]:
        self.barrier.wait(timeout=1)
        if self.failed:
            raise RuntimeError("RPC failed")
        return (
            {"bytes_from_ulp_rc": self.value, "bytes_to_ulp": self.value + 1},
            {
                "global": {
                    "bytes_from_ulp_rc": self.value,
                    "bytes_to_ulp": self.value + 1,
                },
                "rx": {"rx_dropped_conn_rsrc": 0},
            },
        )

    def reconnect(self) -> None:
        self.reconnects += 1


class _ReconnectFailureReader(_Reader):
    def reconnect(self) -> None:
        raise RuntimeError("channel close failed")


def test_scrape_reads_four_accs_in_parallel_and_emits_valid_families() -> None:
    """A scrape reads every ACC and emits each metric family declaration once."""
    exporter = _load_module()
    barrier = threading.Barrier(4)
    readers = {f"acc{i}": _Reader(i * 100, barrier) for i in range(1, 5)}

    output = exporter.collect_metrics(readers)

    assert output.count("# TYPE acc_tele_field counter") == 1
    assert output.count("# TYPE acc_telemetry_bytes_total counter") == 1
    assert output.count("# TYPE acc_telemetry_collector_success gauge") == 1
    assert (
        'acc_tele_field{acc="acc4",section="global",field="bytes_to_ulp"} 401' in output
    )
    assert 'acc_telemetry_collector_success{acc="acc1"} 1' in output
    assert "acc_telemetry_exporter_complete 1" in output.splitlines()
    assert output.count("acc_telemetry_rpc_duration_seconds{acc=") == 4
    assert "acc_telemetry_scrape_duration_seconds " in output


def test_failed_acc_omits_samples_and_marks_fleet_incomplete() -> None:
    """A failed RPC cannot repeat the last counter or expose a partial fleet total."""
    exporter = _load_module()
    barrier = threading.Barrier(4)
    readers = {f"acc{i}": _Reader(i * 100, barrier) for i in range(1, 5)}
    assert (
        "acc_telemetry_exporter_complete 1"
        in exporter.collect_metrics(readers).splitlines()
    )
    readers["acc2"].failed = True

    output = exporter.collect_metrics(readers)

    assert 'acc_telemetry_collector_success{acc="acc2"} 0' in output
    assert 'acc_tele_field{acc="acc2",' not in output
    assert 'acc_telemetry_bytes_total{acc="acc2",' not in output
    assert "acc_telemetry_exporter_complete 0" in output.splitlines()
    assert readers["acc2"].reconnects == 1


def test_failed_reconnect_still_returns_other_acc_counters() -> None:
    """Channel cleanup failure must not turn a partial scrape into HTTP 500."""
    exporter = _load_module()
    barrier = threading.Barrier(4)
    readers = {f"acc{i}": _Reader(i * 100, barrier) for i in range(1, 5)}
    readers["acc2"] = _ReconnectFailureReader(200, barrier)
    readers["acc2"].failed = True

    output = exporter.collect_metrics(readers)

    assert 'acc_telemetry_collector_success{acc="acc2"} 0' in output
    assert 'acc_tele_field{acc="acc2",' not in output
    assert 'acc_tele_field{acc="acc1",' in output
    assert "acc_telemetry_exporter_complete 0" in output.splitlines()


def test_two_acc_target_reports_complete_only_when_both_reads_succeed() -> None:
    """The two-ACC target must expose healthy totals and hide partial scrapes."""
    exporter = _load_module()
    barrier = threading.Barrier(2)
    readers = {f"acc{i}": _Reader(i * 100, barrier) for i in (5, 6)}

    output = exporter.collect_metrics(readers)

    assert "acc_telemetry_exporter_complete 1" in output.splitlines()
    assert (
        'acc_tele_field{acc="acc5",section="global",field="bytes_to_ulp"} 501' in output
    )
    assert (
        'acc_tele_field{acc="acc6",section="global",field="bytes_to_ulp"} 601' in output
    )
    readers["acc6"].failed = True

    output = exporter.collect_metrics(readers)

    assert "acc_telemetry_exporter_complete 0" in output.splitlines()
    assert 'acc_tele_field{acc="acc6",' not in output
    assert 'acc_telemetry_collector_success{acc="acc6"} 0' in output
