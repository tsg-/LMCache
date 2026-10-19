# SPDX-License-Identifier: Apache-2.0
"""Tests for fleet benchmark counter arithmetic."""

from __future__ import annotations

# Standard
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

# Third Party
import pytest


SCRIPT = Path(__file__).parents[2] / "scripts/ipu-poc/measure_mmg_fleet.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("measure_mmg_fleet", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_counter_deltas_use_each_acc_read_span() -> None:
    """Skewed ACC reads must not share a fixed fleet denominator."""
    fleet = _load_module()
    start = {
        "acc": {
            "acc1": {"timestamp": 100.0, "write": 1000, "read": 100},
            "acc2": {"timestamp": 100.2, "write": 2000, "read": 200},
            "acc3": {"timestamp": 100.3, "write": 3000, "read": 300},
            "acc4": {"timestamp": 100.4, "write": 4000, "read": 400},
        },
        "nvme": {"timestamp": 100.1, "read": 10000, "write": 1000},
        "app": {
            "mmgi0": {"timestamp": 100.1, "success_bytes": 1000},
            "mmgi1": {"timestamp": 100.1, "success_bytes": 2000},
            "mmgi2": {"timestamp": 100.1, "success_bytes": 3000},
            "mmgi3": {"timestamp": 100.1, "success_bytes": 4000},
        },
    }
    end = {
        "acc": {
            "acc1": {"timestamp": 110.0, "write": 2000, "read": 110},
            "acc2": {"timestamp": 110.2, "write": 3000, "read": 210},
            "acc3": {"timestamp": 111.3, "write": 4000, "read": 310},
            "acc4": {"timestamp": 110.4, "write": 5000, "read": 410},
        },
        "nvme": {"timestamp": 110.1, "read": 14000, "write": 1000},
        "app": {
            "mmgi0": {"timestamp": 110.1, "success_bytes": 2000},
            "mmgi1": {"timestamp": 110.1, "success_bytes": 3000},
            "mmgi2": {"timestamp": 110.1, "success_bytes": 4000},
            "mmgi3": {"timestamp": 110.1, "success_bytes": 5000},
        },
    }

    totals = fleet.counter_deltas(start, end)

    assert totals["rdma_write_bytes"] == 4000
    assert totals["nvme_read_bytes"] == 4000
    assert totals["app_success_bytes"] == 4000
    assert totals["rdma_write_gbps_decimal"] < totals["nvme_read_gbps_decimal"]


def test_counter_reset_rejects_a_fleet_result() -> None:
    """A reset between snapshots cannot become a valid benchmark rate."""
    fleet = _load_module()
    start = {
        "acc": {"acc1": {"timestamp": 100.0, "write": 1000, "read": 100}},
        "nvme": {"timestamp": 100.0, "read": 0, "write": 0},
        "app": {"mmgi0": {"timestamp": 100.0, "success_bytes": 0}},
    }
    end = {
        "acc": {"acc1": {"timestamp": 110.0, "write": 10, "read": 200}},
        "nvme": {"timestamp": 110.0, "read": 10, "write": 0},
        "app": {"mmgi0": {"timestamp": 110.0, "success_bytes": 10}},
    }

    try:
        fleet.counter_deltas(start, end)
    except ValueError as exc:
        assert "acc1.write" in str(exc)
    else:
        raise AssertionError("counter reset was accepted")


def test_mixed_app_snapshot_keeps_load_and_store_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One scrape gives two direction counters and one shared phase start."""
    fleet = _load_module()
    text = "\n".join(
        f'lmcache_bench_l2_{metric}{{operation="{operation}",phase="measured"}} {value}'
        for operation, success, start in (
            ("Load", 500, 100),
            ("Store", 100, 100),
        )
        for metric, value in (
            ("success_bytes_total", success),
            ("phase_start_time_seconds", start),
        )
    )
    monkeypatch.setattr(fleet, "_sample_time_and_body", lambda *_args: (101.0, text))

    sample = fleet.read_app("mmgi0", 9101, mode="mixed")

    assert sample == {
        "timestamp": 101.0,
        "load_success_bytes": 500.0,
        "store_success_bytes": 100.0,
        "phase_started_at": 100.0,
    }


def test_mixed_fleet_deltas_preserve_both_app_directions() -> None:
    """Mixed result must not collapse app writes into read goodput."""
    fleet = _load_module()
    base = {
        "acc": {"acc1": {"timestamp": 100.0, "write": 0, "read": 0}},
        "nvme": {"timestamp": 100.0, "read": 0, "write": 0},
        "app": {
            "mmgi0": {
                "timestamp": 100.0,
                "load_success_bytes": 1000,
                "store_success_bytes": 200,
            }
        },
    }
    after = {
        "acc": {"acc1": {"timestamp": 110.0, "write": 500, "read": 100}},
        "nvme": {"timestamp": 110.0, "read": 400, "write": 100},
        "app": {
            "mmgi0": {
                "timestamp": 110.0,
                "load_success_bytes": 1400,
                "store_success_bytes": 300,
            }
        },
    }

    totals = fleet.counter_deltas(base, after)

    assert totals["app_load_bytes"] == 400
    assert totals["app_store_bytes"] == 100
    assert totals["app_read_write_ratio"] == 4.0


def test_mixed_fleet_requires_both_target_directions() -> None:
    """A mixed cell cannot pass when its Store path is absent on the target."""
    fleet = _load_module()
    deltas = {
        "app_load_gbps_decimal": 500.0,
        "app_store_gbps_decimal": 100.0,
        "nvme_read_gbps_decimal": 500.0,
        "nvme_write_gbps_decimal": 0.0,
        "rdma_write_gbps_decimal": 503.0,
        "rdma_read_gbps_decimal": 101.0,
    }

    with pytest.raises(ValueError, match="NVMe writes"):
        fleet.validate_mixed_counter_agreement(deltas)

    deltas["nvme_write_gbps_decimal"] = 100.0
    fleet.validate_mixed_counter_agreement(deltas)


def test_mixed_host_result_requires_full_success_and_duration() -> None:
    """Both directions must complete the agreed measured window."""
    fleet = _load_module()
    result = {
        "metrics": {
            "config": {
                "duration_sec": 300.0,
                "read_write_ratio_requested": "5:1",
            },
            "op_0": {
                "operation": "Load",
                "total_success": 10,
                "total_keys": 10,
                "total_success_bytes": 500,
                "window_sec": 300.1,
                "phase_start_time_seconds": 100,
                "phase_end_time_seconds": 400.1,
            },
            "op_1": {
                "operation": "Store",
                "total_success": 2,
                "total_keys": 2,
                "total_success_bytes": 100,
                "window_sec": 300.1,
                "phase_start_time_seconds": 100,
                "phase_end_time_seconds": 400.1,
            },
            "mixed": {"read_write_ratio_achieved": 5.0},
        }
    }

    summary = fleet.host_result_summary(
        result, "/root/mixed.json", mode="mixed", duration=300
    )
    assert summary["operations"]["Load"]["total_success_bytes"] == 500
    assert summary["operations"]["Store"]["total_success_bytes"] == 100

    result["metrics"]["op_1"]["window_sec"] = 240
    with pytest.raises(ValueError, match="measured window"):
        fleet.host_result_summary(
            result, "/root/mixed.json", mode="mixed", duration=300
        )


def test_fleet_rejects_incomplete_host_run() -> None:
    """A partial host load cannot be linked as a valid fleet benchmark."""
    fleet = _load_module()
    result = {
        "metrics": {
            "op_0": {
                "operation": "Load",
                "total_success": 9,
                "total_keys": 10,
            }
        }
    }

    with pytest.raises(ValueError, match="requests did not all succeed"):
        fleet.host_result_summary(result, "/root/cell-if2-mmgi0.json")


def test_fleet_links_explicit_host_filename_scheme(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An overnight cell can link host files with IF before repetition."""
    fleet = _load_module()
    seen: list[str] = []
    result = {
        "metrics": {
            "op_0": {
                "operation": "Load",
                "total_success": 10,
                "total_keys": 10,
            }
        }
    }

    def read_host(host: str, command: str) -> str:
        seen.append(command)
        return json.dumps(result)

    monkeypatch.setattr(fleet, "_ssh", read_host)
    links = fleet.result_links(
        "goal4-overnight-20260925-0030-A-minimax-qp128",
        2,
        "/root/goal4-overnight-20260925-0030-A-minimax-qp128-if2-rep1-{host}.json",
    )

    assert len(links) == 4
    assert (
        "/root/goal4-overnight-20260925-0030-A-minimax-qp128-if2-rep1-mmgi0.json"
        in seen[0]
    )


def test_selected_lanes_read_only_their_accs_and_target_disks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one/two-lane result must exclude inactive target IPU counters."""
    fleet = _load_module()
    acc_text = "acc_telemetry_exporter_complete 1\n"
    for index in range(1, 5):
        for field in ("bytes_from_ulp_rc", "bytes_to_ulp"):
            acc_text += (
                f'acc_tele_field{{acc="acc{index}",section="global",'
                f'field="{field}"}} {index * 100}\n'
            )
        acc_text += (
            f'acc_telemetry_read_timestamp_seconds{{acc="acc{index}",'
            'source="grpc_exporter"} 100\n'
        )
    seen: list[str] = []

    def read_target(_host: str, command: str) -> str:
        seen.append(command)
        if "9188" in command:
            return acc_text
        return "\n".join(f"/dev/nvme{i}n1" for i in range(1, 9))

    monkeypatch.setattr(fleet, "_ssh", read_target)

    acc = fleet.read_acc(("mmgi0", "mmgi2"))
    disks = fleet.exported_devices(("mmgi0", "mmgi2"))

    assert set(acc) == {"acc2", "acc3"}
    assert len(disks) == 8
    assert "for port in 1 3" in seen[1]


def test_selected_lane_links_and_rejects_short_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only selected host JSONs count and a partial phase is invalid."""
    fleet = _load_module()
    result = {
        "metrics": {
            "op_0": {
                "operation": "Load",
                "total_success": 10,
                "total_keys": 10,
                "window_sec": 60.1,
                "phase_start_time_seconds": 100,
                "phase_end_time_seconds": 160.1,
            }
        }
    }
    seen: list[str] = []

    def read_host(host: str, command: str) -> str:
        seen.append(host)
        return json.dumps(result)

    monkeypatch.setattr(fleet, "_ssh", read_host)

    links = fleet.result_links(
        "goal5-test",
        4,
        "/root/result-{host}.json",
        duration=60,
        hosts=("mmgi1", "mmgi3"),
    )
    assert set(links) == {"mmgi1", "mmgi3"}
    assert seen == ["mmgi1", "mmgi3"]

    result["metrics"]["op_0"]["window_sec"] = 49
    with pytest.raises(ValueError, match="measured window"):
        fleet.host_result_summary(result, "/root/result-mmgi1.json", duration=60)


def test_load_result_rejects_wrong_acc_attribution() -> None:
    """An idle selected ACC cannot silently make a fleet result valid."""
    fleet = _load_module()
    deltas = {
        "app_goodput_gbps_decimal": 382.5,
        "nvme_read_gbps_decimal": 382.49,
        "rdma_write_gbps_decimal": 0.0,
    }

    with pytest.raises(ValueError, match="selected ACC RDMA WR"):
        fleet.validate_load_counter_agreement(deltas)

    deltas["rdma_write_gbps_decimal"] = 384.0
    fleet.validate_load_counter_agreement(deltas)
