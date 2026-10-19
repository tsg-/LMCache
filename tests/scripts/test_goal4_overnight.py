# SPDX-License-Identifier: Apache-2.0
"""Contract checks for the detached Goal 4 controller."""

# Future
from __future__ import annotations

# Standard
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from types import ModuleType
from typing import Any
import importlib.util
import json
import subprocess
import sys

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
            "expected_code_sha256": {
                "lmcache/cli/commands/bench/l2_adapter_bench/command.py": "reviewed",
                "lmcache/cli/commands/bench/l2_adapter_bench/runner.py": "reviewed",
                "lmcache/cli/commands/bench/l2_adapter_bench/metrics.py": "reviewed",
                "scripts/ipu-poc/run_model_geometry.sh": "reviewed",
            },
        }
    )
    row = {
        "git_sha": "commit",
        "git_status": " M command.py",
        "file_sha256": {
            name: "different" for name in runner.config["expected_code_sha256"]
        },
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
        def now(cls, tz: tzinfo | None = None) -> FixedDateTime:
            return cls.fromtimestamp(
                current.timestamp(), current.tzinfo if tz is None else tz
            )

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


def test_resume_rejects_changed_configuration(tmp_path: Path) -> None:
    """Changing a corpus under the same run ID must preserve and reject old state."""
    goal4 = _load_module()
    config = {
        "run_id": "20261018-2010",
        "results_dir": str(tmp_path),
        "review_files": [],
        "models": {"minimax": {"corpus": "original"}},
    }
    runner = goal4.Overnight(config)
    status_path = tmp_path / "goal4-overnight-20261018-2010.status.json"
    assert goal4.Overnight(config).status["run_id"] == runner.status["run_id"]
    original = status_path.read_bytes()

    with pytest.raises(RuntimeError, match="configuration"):
        goal4.Overnight({**config, "models": {"minimax": {"corpus": "different"}}})
    assert json.loads(status_path.read_bytes()) == json.loads(original)


def test_resume_rejects_state_without_configuration(tmp_path: Path) -> None:
    """Legacy status lacking a configuration cannot establish resume identity."""
    goal4 = _load_module()
    path = tmp_path / "goal4-overnight-20261018-2010.status.json"
    path.write_text('{"run_id": "20261018-2010"}')
    with pytest.raises(RuntimeError, match="configuration"):
        goal4.Overnight(
            {
                "run_id": "20261018-2010",
                "results_dir": str(tmp_path),
                "review_files": [],
            }
        )
    assert path.read_text() == '{"run_id": "20261018-2010"}'


@pytest.mark.parametrize("mismatch", [None, "in_flight", "profile", "corpus"])
@pytest.mark.parametrize("successful", [True, False])
def test_resume_checks_cell_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mismatch: str | None,
    successful: bool,
) -> None:
    """Resume accepts matching evidence and preserves mismatched cell results."""
    goal4 = _load_module()
    profile = "scripts/ipu-poc/models/minimax.yaml"
    runner = goal4.Overnight(
        {
            "run_id": "20261018-2010",
            "results_dir": str(tmp_path),
            "review_files": [],
            "models": {"minimax": {"corpus": "corpus", "profile": profile}},
            "expected_code_sha256": {profile: "profile-hash"},
        }
    )
    adapter: dict[str, Any] = {
        "type": "fs_native",
        "base_path": "/mnt/lmcache/bench-l2/corpus/hostname",
        "num_workers": 32,
        "use_odirect": True,
    }
    result: dict[str, Any] = {
        "metrics": {
            "config": {
                "in_flight": 2,
                "mode": "sustained",
                "duration_sec": 60,
                "warmup_sec": 90,
                "l2_adapter_json": json.dumps(adapter),
            },
            "geometry": {"profile_sha256": "profile-hash"},
            "op_0": {
                "operation": "Load",
                "total_success": 10 if successful else 9,
                "total_keys": 10,
                "phase_start_time_seconds": 100,
                "phase_end_time_seconds": 160,
            },
        }
    }
    if mismatch == "in_flight":
        result["metrics"]["config"]["in_flight"] = 256
    elif mismatch == "profile":
        result["metrics"]["geometry"]["profile_sha256"] = "other-hash"
    elif mismatch == "corpus":
        adapter["base_path"] = "/mnt/lmcache/bench-l2/other/hostname"
        result["metrics"]["config"]["l2_adapter_json"] = json.dumps(adapter)
    for host in goal4.HOSTS:
        path = tmp_path / (
            f"goal4-overnight-20261018-2010-A-minimax-qp128-if2-rep1-{host}.json"
        )
        path.write_text(json.dumps(result))

    def fetch(
        host: str, argv: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "cat"
        return subprocess.CompletedProcess(argv, 0, Path(argv[1]).read_text(), "")

    monkeypatch.setattr(goal4, "_run", fetch)
    if mismatch is None:
        assert runner._completed(("A", "minimax", 128, 2, 1)) is successful
    else:
        with pytest.raises(
            RuntimeError, match="refusing to replace mismatched evidence"
        ):
            runner.run_cell(("A", "minimax", 128, 2, 1))
    for host in goal4.HOSTS:
        path = tmp_path / (
            f"goal4-overnight-20261018-2010-A-minimax-qp128-if2-rep1-{host}.json"
        )
        assert json.loads(path.read_text()) == result


def test_provenance_requires_runner_and_metrics_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An old hash contract must not approve unverified benchmark dependencies."""
    goal4 = _load_module()
    hashes = {
        "lmcache/cli/commands/bench/l2_adapter_bench/command.py": "reviewed",
        "scripts/ipu-poc/run_model_geometry.sh": "reviewed",
    }
    runner = goal4.Overnight(
        {
            "run_id": "20261018-2010",
            "results_dir": str(tmp_path),
            "review_files": [],
            "expected_git_sha": "commit",
            "expected_code_sha256": hashes,
        }
    )
    row = {
        "git_sha": "commit",
        "git_status": " M lmcache/cli/commands/bench/l2_adapter_bench/runner.py",
        "file_sha256": hashes,
    }
    monkeypatch.setattr(
        goal4,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 0, json.dumps(row), ""),
    )
    with pytest.raises(RuntimeError, match="provenance"):
        runner._code_provenance()


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("stop", "stopped"),
        ("cutoff", "stopped"),
        ("failed_cells", "two_consecutive_failed_cells"),
        ("skipped_block", "incomplete"),
        ("complete", "complete"),
    ],
)
def test_incomplete_matrix_never_reports_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    expected: str,
) -> None:
    """Stopping or skipping a block must remain visible after storage restoration."""
    goal4 = _load_module()
    runner = goal4.Overnight(
        {
            "run_id": "20261018-2010",
            "results_dir": str(tmp_path),
            "review_files": [],
        }
    )
    for method in (
        "wait_for_prior",
        "_no_io",
        "_storage_healthy",
        "_code_provenance",
        "verify_qps",
        "_corpus",
        "_canary_rdma_advisory",
        "_restore",
    ):
        monkeypatch.setattr(runner, method, lambda *args: None)
    monkeypatch.setattr(runner, "_completed", lambda cell: cell[0] == "canary")
    if reason in ("stop", "cutoff"):

        def stop_after_canary(*args: object) -> None:
            if reason == "stop":
                (tmp_path / "goal4-overnight-20261018-2010.STOP").touch()
            else:
                runner.status["cutoff_time"] = "2000-01-01T07:00:00-07:00"

        monkeypatch.setattr(runner, "_canary_rdma_advisory", stop_after_canary)
    elif reason == "failed_cells":
        monkeypatch.setattr(runner, "run_cell", lambda _: False)
    elif reason == "skipped_block":
        monkeypatch.setattr(goal4, "BLOCKS", (("C", "minimax", 1, (2,)),))

        def fail_reconnect(qps: int) -> None:
            raise RuntimeError("reconnect unavailable")

        monkeypatch.setattr(runner, "reconnect", fail_reconnect)
    else:
        monkeypatch.setattr(runner, "_completed", lambda _: True)
    runner.execute()
    assert runner.status["state"] == expected
