# SPDX-License-Identifier: Apache-2.0
"""Tests for the local multi-process fs_native aggregate reporter."""

from __future__ import annotations

# Standard
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

# Third Party
import pytest


SCRIPT = Path(__file__).parents[2] / "scripts/ipu-poc/multi_initiator_report.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("multi_initiator_report", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reporter = _load_module()


def _write_worker(
    run_dir: Path, worker_id: int, start: float, success: int = 100
) -> None:
    """Write one successful, sustained worker artifact set."""
    metrics = {
        "metrics": {
            "config": {"mode": "sustained"},
            "op_0": {
                "operation": "Load",
                "window_sec": 10.0,
                "drain_tail_sec": 0.1,
                "total_keys": success,
                "total_success": success,
            },
        }
    }
    (run_dir / f"initiator-{worker_id}.json").write_text(json.dumps(metrics))
    (run_dir / f"initiator-{worker_id}.status").write_text("0\n")
    (run_dir / f"initiator-{worker_id}.log").write_text(
        f"{start:.3f} [Load] Sustained window for 10.0s at 4 in flight...\n"
    )


def _write_poll(path: Path) -> None:
    """Write a 20 ops/s counter trace over the workers' overlapping window."""
    path.write_text(
        "".join(f"{100 + second:.3f} {second * 20}\n" for second in range(12))
    )


def test_accepts_aligned_workers_with_matching_counter_rate(tmp_path: Path) -> None:
    _write_worker(tmp_path, 0, 100.0)
    _write_worker(tmp_path, 1, 100.1)
    poll = tmp_path / "rdma-poll.txt"
    _write_poll(poll)

    report = reporter.build_report(
        run_dir=tmp_path,
        expected_initiators=2,
        data_size_kb=1,
        poll_path=poll,
        trim_sec=1.0,
        max_start_skew_sec=1.0,
        counter_segment_bytes=1024.0,
        counter_tolerance=0.05,
        counters={"InRdmaWrites": 220},
        corpus_before=1000,
        corpus_after=1000,
    )

    assert report["accepted"]
    assert report["window_start_skew_sec"] == pytest.approx(0.1)
    assert report["aggregate_success_gbps"] > 0
    assert report["counter_app_rate_ratio"] == 1.0


def test_rejects_unaligned_workers_without_relabeling_them_as_aggregate(
    tmp_path: Path,
) -> None:
    _write_worker(tmp_path, 0, 100.0)
    _write_worker(tmp_path, 1, 103.0)
    poll = tmp_path / "rdma-poll.txt"
    _write_poll(poll)

    report = reporter.build_report(
        run_dir=tmp_path,
        expected_initiators=2,
        data_size_kb=1,
        poll_path=poll,
        trim_sec=1.0,
        max_start_skew_sec=1.0,
        counter_segment_bytes=1024.0,
        counter_tolerance=0.05,
        counters={"InRdmaWrites": 220},
        corpus_before=1000,
        corpus_after=1000,
    )

    assert not report["accepted"]
    assert "window-start skew" in " ".join(report["acceptance_failures"])
