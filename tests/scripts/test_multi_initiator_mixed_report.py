# SPDX-License-Identifier: Apache-2.0
"""Tests for the local multi-process mixed aggregate reporter."""

from __future__ import annotations

# Standard
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

# Third Party
import pytest


SCRIPT = Path(__file__).parents[2] / "scripts/ipu-poc/multi_initiator_mixed_report.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "multi_initiator_mixed_report", SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reporter = _load_module()


def _zero_error_counters() -> dict[str, int]:
    """Return the explicit zero-valued fabric error deltas a run requires."""
    return {counter: 0 for counter in reporter._ERROR_COUNTERS}


def _write_worker(
    run_dir: Path,
    worker_id: int,
    start: float,
    ratio: str = "5:1",
    read_keys: int = 100,
    write_keys: int = 20,
) -> None:
    payload_bytes = 52428 * 1024
    metrics = {
        "metrics": {
            "config": {
                "mode": "sustained",
                "read_write_ratio_requested": ratio,
                "write_key_prefix": f"write-{worker_id}",
            },
            "op_0": {
                "operation": "Load",
                "window_sec": 10.0,
                "total_keys": read_keys,
                "total_success": read_keys,
            },
            "op_1": {
                "operation": "Store",
                "window_sec": 10.0,
                "total_keys": write_keys,
                "total_success": write_keys,
            },
            "mixed": {
                "read_success_bytes": read_keys * payload_bytes,
                "write_success_bytes": write_keys * payload_bytes,
                "read_write_ratio_achieved": read_keys / write_keys,
            },
        }
    }
    (run_dir / f"initiator-{worker_id}.json").write_text(json.dumps(metrics))
    (run_dir / f"initiator-{worker_id}.status").write_text("0\n")
    (run_dir / f"initiator-{worker_id}.log").write_text(
        f"{start:.3f} [Mixed] Sustained window for 10.0s at 8 total in flight\n"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [("5:1", 5.0), ("9:1", 9.0), ("1:1", 1.0), ("10:2", 5.0)],
)
def test_parse_ratio_accepts_read_write_form(value: str, expected: float) -> None:
    assert reporter._parse_ratio(value) == expected


@pytest.mark.parametrize("value", ["5", "5:0", "0:1", "-5:1", "", "5:1:1", "a:b"])
def test_parse_ratio_rejects_malformed(value: str) -> None:
    with pytest.raises(ValueError):
        reporter._parse_ratio(value)


def test_accepts_matching_mixed_workers(tmp_path: Path) -> None:
    _write_worker(tmp_path, 0, 100.0)
    _write_worker(tmp_path, 1, 100.1)
    poll = tmp_path / "rdma-poll.txt"
    poll.write_text(
        "".join(
            f"{100 + second:.3f} {second * 20480} {second * 52428}\n"
            for second in range(12)
        )
    )

    report = reporter.build_report(
        run_dir=tmp_path,
        expected_initiators=2,
        data_size_kb=52428,
        poll_path=poll,
        trim_sec=1.0,
        max_start_skew_sec=1.0,
        requested_ratio="5:1",
        ratio_tolerance=0.01,
        counter_tolerance=0.05,
        counters=_zero_error_counters(),
        corpus_before=1000,
        corpus_after=1000,
    )

    assert report["accepted"]
    assert report["achieved_read_write_ratio"] == 5.0
    assert report["read_counter_app_rate_ratio"] == 1.0
    assert report["write_counter_app_rate_ratio"] == 1.0


def _poll_lines(read_step: int = 20480, write_step: int = 52428) -> str:
    return "".join(
        f"{100 + second:.3f} {second * read_step} {second * write_step}\n"
        for second in range(12)
    )


def test_accepts_nine_to_one_ratio(tmp_path: Path) -> None:
    """A 9:1 run is accepted against its own requested ratio, not 5:1."""
    _write_worker(tmp_path, 0, 100.0, ratio="9:1", read_keys=90, write_keys=10)
    _write_worker(tmp_path, 1, 100.1, ratio="9:1", read_keys=90, write_keys=10)
    poll = tmp_path / "rdma-poll.txt"
    poll.write_text(_poll_lines())

    report = reporter.build_report(
        run_dir=tmp_path,
        expected_initiators=2,
        data_size_kb=52428,
        poll_path=poll,
        trim_sec=1.0,
        max_start_skew_sec=1.0,
        requested_ratio="9:1",
        ratio_tolerance=0.01,
        counter_tolerance=0.05,
        counters=_zero_error_counters(),
        corpus_before=1000,
        corpus_after=1000,
    )

    assert report["accepted"]
    assert report["achieved_read_write_ratio"] == 9.0
    assert report["requested_read_write_ratio"] == "9:1"


def test_read_fraction_is_the_fio_comparable_unit(tmp_path: Path) -> None:
    """The reported read fraction matches fio's rwmixread for both ratios."""
    _write_worker(tmp_path, 0, 100.0, ratio="9:1", read_keys=90, write_keys=10)
    _write_worker(tmp_path, 1, 100.1, ratio="9:1", read_keys=90, write_keys=10)
    poll = tmp_path / "rdma-poll.txt"
    poll.write_text(_poll_lines())

    report = reporter.build_report(
        run_dir=tmp_path,
        expected_initiators=2,
        data_size_kb=52428,
        poll_path=poll,
        trim_sec=1.0,
        max_start_skew_sec=1.0,
        requested_ratio="9:1",
        ratio_tolerance=0.01,
        counter_tolerance=0.05,
        counters=_zero_error_counters(),
        corpus_before=1000,
        corpus_after=1000,
    )

    # rwmixread=90 in fio terms; 9/(9+1).
    assert report["achieved_read_fraction"] == 0.9
    assert report["requested_read_fraction"] == 0.9


def test_rejects_worker_ratio_not_matching_requested(tmp_path: Path) -> None:
    """A 5:1 worker in a run declared as 9:1 is a configuration error."""
    _write_worker(tmp_path, 0, 100.0, ratio="9:1", read_keys=90, write_keys=10)
    _write_worker(tmp_path, 1, 100.1, ratio="5:1", read_keys=90, write_keys=10)
    poll = tmp_path / "rdma-poll.txt"
    poll.write_text(_poll_lines())

    report = reporter.build_report(
        run_dir=tmp_path,
        expected_initiators=2,
        data_size_kb=52428,
        poll_path=poll,
        trim_sec=1.0,
        max_start_skew_sec=1.0,
        requested_ratio="9:1",
        ratio_tolerance=0.01,
        counter_tolerance=0.05,
        counters=_zero_error_counters(),
        corpus_before=1000,
        corpus_after=1000,
    )

    assert not report["accepted"]
    assert "requested ratio is 5:1, expected 9:1" in " ".join(
        report["acceptance_failures"]
    )


def test_rejects_ratio_drift(tmp_path: Path) -> None:
    _write_worker(tmp_path, 0, 100.0)
    _write_worker(tmp_path, 1, 100.1)
    data = json.loads((tmp_path / "initiator-1.json").read_text())
    data["metrics"]["mixed"]["write_success_bytes"] = 25 * 1024
    (tmp_path / "initiator-1.json").write_text(json.dumps(data))
    poll = tmp_path / "rdma-poll.txt"
    poll.write_text(
        "".join(
            f"{100 + second:.3f} {second * 20480} {second * 52428}\n"
            for second in range(12)
        )
    )

    report = reporter.build_report(
        run_dir=tmp_path,
        expected_initiators=2,
        data_size_kb=52428,
        poll_path=poll,
        trim_sec=1.0,
        max_start_skew_sec=1.0,
        requested_ratio="5:1",
        ratio_tolerance=0.01,
        counter_tolerance=0.05,
        counters=_zero_error_counters(),
        corpus_before=1000,
        corpus_after=1000,
    )

    assert not report["accepted"]
    assert "achieved ratio" in " ".join(report["acceptance_failures"])


def test_rejects_missing_error_counter_delta(tmp_path: Path) -> None:
    _write_worker(tmp_path, 0, 100.0)
    _write_worker(tmp_path, 1, 100.1)
    poll = tmp_path / "rdma-poll.txt"
    poll.write_text(
        "".join(
            f"{100 + second:.3f} {second * 20480} {second * 52428}\n"
            for second in range(12)
        )
    )
    counters = _zero_error_counters()
    counters.pop("RTO")

    report = reporter.build_report(
        run_dir=tmp_path,
        expected_initiators=2,
        data_size_kb=52428,
        poll_path=poll,
        trim_sec=1.0,
        max_start_skew_sec=1.0,
        requested_ratio="5:1",
        ratio_tolerance=0.01,
        counter_tolerance=0.05,
        counters=counters,
        corpus_before=1000,
        corpus_after=1000,
    )

    assert not report["accepted"]
    assert "missing error-counter delta: RTO" in report["acceptance_failures"]


def test_counter_rate_mismatch_is_reported_but_not_accepted_as_a_gate(
    tmp_path: Path,
) -> None:
    _write_worker(tmp_path, 0, 100.0)
    _write_worker(tmp_path, 1, 100.1)
    poll = tmp_path / "rdma-poll.txt"
    poll.write_text(
        "".join(
            f"{100 + second:.3f} {second * 10240} {second * 52428}\n"
            for second in range(12)
        )
    )

    report = reporter.build_report(
        run_dir=tmp_path,
        expected_initiators=2,
        data_size_kb=52428,
        poll_path=poll,
        trim_sec=1.0,
        max_start_skew_sec=1.0,
        requested_ratio="5:1",
        ratio_tolerance=0.01,
        counter_tolerance=0.05,
        counters=_zero_error_counters(),
        corpus_before=1000,
        corpus_after=1000,
    )

    assert report["accepted"]
    assert report["counter_correlation"]["acceptance_role"] == (
        "non-gating corroboration"
    )
    assert report["counter_correlation"]["directions"]["read"]["status"] == (
        "outside_tolerance"
    )
