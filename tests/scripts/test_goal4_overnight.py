# SPDX-License-Identifier: Apache-2.0
"""Contract checks for the detached Goal 4 controller."""

from __future__ import annotations

# Standard
import importlib.util
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import ModuleType

# Third Party
import pytest


SCRIPT = Path(__file__).parents[2] / "scripts/ipu-poc/goal4_overnight.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("goal4_overnight", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_host_result_requires_successful_measured_load() -> None:
    """Only a complete Load with a real measured interval can pass."""
    goal4 = _load_module()
    row = {
        "metrics": {
            "op_0": {
                "operation": "Load",
                "total_success": 10,
                "total_keys": 10,
                "phase_start_time_seconds": 100,
                "phase_end_time_seconds": 160,
            }
        }
    }

    assert goal4._host_result_ok(row) == (True, "")
    row["metrics"]["op_0"]["total_success"] = 9
    assert goal4._host_result_ok(row)[0] is False
    row["metrics"]["op_0"]["total_success"] = 10
    row["metrics"]["op_0"]["phase_end_time_seconds"] = 0
    assert goal4._host_result_ok(row)[0] is False


def test_cells_cover_full_curve_rep_first() -> None:
    """Each repetition must cover every IF before the next repetition."""
    goal4 = _load_module()
    cells = goal4._cells("A", "minimax", 128, (1, 2, 4))

    assert cells == [
        ("A", "minimax", 128, 1, 1),
        ("A", "minimax", 128, 2, 1),
        ("A", "minimax", 128, 4, 1),
        ("A", "minimax", 128, 1, 2),
        ("A", "minimax", 128, 2, 2),
        ("A", "minimax", 128, 4, 2),
        ("A", "minimax", 128, 1, 3),
        ("A", "minimax", 128, 2, 3),
        ("A", "minimax", 128, 4, 3),
    ]


def test_remote_calls_use_strict_noninteractive_ssh() -> None:
    """The mmgi0 controller must not rely on a laptop SSH session."""
    goal4 = _load_module()
    remote = goal4._ssh_command("mmgi2", ["sudo", "-n", "true"])

    assert remote[:2] == ["ssh", "-o"]
    assert "BatchMode=yes" in remote
    assert "StrictHostKeyChecking=yes" in remote
    assert remote[-2:] == ["mmgi2", "sudo -n true"]


def test_qp_verification_rejects_a_silent_queue_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A controller returning fewer queues than requested must stop the run."""
    goal4 = _load_module()
    config = {
        "run_id": "20260925-0030",
        "results_dir": str(tmp_path),
        "review_files": [],
    }
    runner = goal4.Overnight(config)
    rows = {
        host: [
            {"controller": f"nvme{i}", "queue_count": 129, "state": "live"}
            for i in range(1, 5)
        ]
        for host in goal4.HOSTS
    }
    rows["mmgi2"][2]["queue_count"] = 33
    monkeypatch.setattr(runner, "_controllers", lambda: rows)

    with pytest.raises(RuntimeError, match="mmgi2 QP=128 verification failed"):
        runner.verify_qps(128)


def test_broken_md0_stops_before_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The controller must leave an unhealthy mounted RAID0 for a human."""
    goal4 = _load_module()
    runner = goal4.Overnight(
        {
            "run_id": "20260925-0030",
            "results_dir": str(tmp_path),
            "review_files": [],
        }
    )
    monkeypatch.setattr(
        goal4,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, "", ""),
    )

    with pytest.raises(goal4.StorageRecoveryRequired, match="md0 or mount"):
        runner._storage_healthy()


def test_resume_accepts_valid_remote_results_without_local_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart skips a complete cell even if mmgi0 had not copied its JSONs."""
    goal4 = _load_module()
    runner = goal4.Overnight(
        {
            "run_id": "20260925-0030",
            "results_dir": str(tmp_path),
            "review_files": [],
        }
    )
    seen: list[str] = []

    def fetched(
        cell: tuple[str, str, int, int, int], host: str
    ) -> tuple[bool, str, float]:
        seen.append(host)
        return True, "", 100.0

    monkeypatch.setattr(runner, "_fetch_host_result", fetched)

    assert runner._completed(("A", "minimax", 128, 2, 1))
    assert seen == list(goal4.HOSTS)


def test_code_provenance_rejects_an_unreviewed_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An uncommitted bench change must match the reviewed content hash."""
    goal4 = _load_module()
    runner = goal4.Overnight(
        {
            "run_id": "20260925-0030",
            "results_dir": str(tmp_path),
            "review_files": [],
            "expected_git_sha": "commit",
            "expected_code_sha256": {"command.py": "reviewed"},
        }
    )
    row = {
        "git_sha": "commit",
        "git_status": " M command.py",
        "file_sha256": {"command.py": "different"},
    }
    monkeypatch.setattr(
        goal4,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, json.dumps(row), ""),
    )

    with pytest.raises(RuntimeError, match="command.py differs"):
        runner._code_provenance()
    assert len(runner.status["code_provenance"]) == 4


def test_canary_rdma_timeout_is_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow telemetry check must not stop the measured load curve."""
    goal4 = _load_module()
    runner = goal4.Overnight(
        {
            "run_id": "20260925-0915",
            "results_dir": str(tmp_path),
            "review_files": [],
            "rdma_check_script": "check_rdma.py",
            "dashboard_file": "dashboard.json",
        }
    )

    def timed_out(
        host: str, argv: list[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        assert host == "mmgi0"
        assert timeout >= 120
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(goal4, "_run", timed_out)
    runner._canary_rdma_advisory(("canary", "minimax", 128, 2, 1))

    assert runner.status["canary_rdma"]["classification"] == "unverified"
    assert "timeout" in runner.status["canary_rdma"]["error"].lower()


def test_fleet_result_fetch_timeout_does_not_fail_cell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing advisory fleet JSON must leave a successful cell successful."""
    goal4 = _load_module()
    runner = goal4.Overnight(
        {
            "run_id": "20260925-0915",
            "results_dir": str(tmp_path),
            "review_files": [],
        }
    )
    cell = ("canary", "minimax", 128, 2, 1)
    monkeypatch.setattr(runner, "_completed", lambda _: False)
    monkeypatch.setattr(runner, "_storage_healthy", lambda: None)
    monkeypatch.setattr(
        runner,
        "_coordinator",
        lambda _: subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(
        runner,
        "_host_command",
        lambda _host, _cell: subprocess.CompletedProcess([], 0, "", ""),
    )
    monkeypatch.setattr(
        runner, "_fetch_host_result", lambda _cell, _host: (True, "", 100.0)
    )

    def timed_out(
        host: str, argv: list[str], *, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        assert host == "mmgi1"
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(goal4, "_run", timed_out)

    assert runner.run_cell(cell)
    assert runner.status["cells"][runner._cell_id(cell)]["fleet_result"] == "missing"
    assert runner.status["failures"] == []


def test_daytime_start_stops_at_next_morning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Starting after 07:00 must still allow cells until the next 07:00."""
    goal4 = _load_module()
    current = datetime(2026, 9, 25, 9, 0, tzinfo=timezone(timedelta(hours=-7)))

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz: timezone | None = None) -> datetime:
            return current

    monkeypatch.setattr(goal4, "datetime", FixedDateTime)
    runner = goal4.Overnight(
        {
            "run_id": "20260925-0915",
            "results_dir": str(tmp_path),
            "review_files": [],
        }
    )

    assert runner.status["cutoff_time"] == "2026-09-26T07:00:00-07:00"
    assert not runner._stop_requested()
    current += timedelta(hours=22)
    assert runner._stop_requested()
