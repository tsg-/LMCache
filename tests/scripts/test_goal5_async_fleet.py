# SPDX-License-Identifier: Apache-2.0
"""Contracts for phase-aligned native filesystem fleet instrumentation."""

# Standard
from pathlib import Path
from types import ModuleType
from typing import Any
import importlib
import json
import sys

# Third Party
import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts/ipu-poc"))


def test_native_counts_require_drain_bytes_and_bound_depth() -> None:
    """Successful application evidence cannot excuse missing native completions."""
    # Third Party
    from goal5_async_fleet_probe import native_summary

    rows: list[dict[str, Any]] = [
        {
            "time": i / 10,
            "terminal": False,
            "stats": {
                "pending_reads": 4,
                "submitted_reads": 112,
                "completed_reads": 108,
                "max_worker_pending": 32,
                "max_total_pending": 56,
            },
        }
        for i in range(11)
    ]
    rows.append(
        {
            "time": 1.1,
            "terminal": True,
            "stats": {
                "pending_reads": 0,
                "submitted_reads": 112,
                "completed_reads": 112,
                "max_worker_pending": 32,
                "max_total_pending": 56,
            },
        }
    )
    result = native_summary(rows, 0, 1, 56, 32, 32)
    assert result["mean_sampled_pending_reads"] == 4
    assert result["terminal"]["completed_reads"] == 112
    rows[-1]["stats"]["completed_reads"] = 111
    with pytest.raises(ValueError, match="drain"):
        native_summary(rows, 0, 1, 56, 32, 32)
    rows[-1]["stats"]["completed_reads"] = 112
    with pytest.raises(ValueError, match="counts"):
        native_summary(rows, 0, 1, 113, 32, 32)
    rows[-1]["stats"]["max_worker_pending"] = 33
    with pytest.raises(ValueError, match="depth"):
        native_summary(rows, 0, 1, 56, 32, 32)


def test_attempt_recorder_owns_distinct_files_and_rejects_path_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated cells cannot overwrite counter evidence or escape their root."""
    backend: Any = ModuleType("lmcache_fs_uring")

    class Client:
        """Provide the public counter and close contract without lab I/O."""

        def __init__(self, **kwargs: Any) -> None:
            pass

        def close(self) -> None:
            """Finish the native client."""

        def io_stats(self) -> dict[str, int]:
            """Return drained public counters."""
            return {"pending_reads": 0, "submitted_reads": 0, "completed_reads": 0}

    backend.LMCacheFSUringClient = Client
    monkeypatch.setitem(sys.modules, "lmcache_fs_uring", backend)
    for name in ("goal5_async_stats", "goal5_async_fleet_stats"):
        sys.modules.pop(name, None)
    module = importlib.import_module("goal5_async_fleet_stats")
    for artifact in ("attempt-0001-load-if2-r1", "attempt-0002-load-if2-r1"):
        monkeypatch.setenv("GOAL5_ARTIFACT", artifact)
        client = module.FleetRecordingClient(stats_directory=str(tmp_path))
        client.close()
        rows = [
            json.loads(line)
            for line in (tmp_path / f"{artifact}-native.jsonl").read_text().splitlines()
        ]
        assert rows[-1]["terminal"]
    with pytest.raises(FileExistsError):
        module.FleetRecordingClient(stats_directory=str(tmp_path))
    monkeypatch.setenv("GOAL5_ARTIFACT", "../foreign")
    with pytest.raises(ValueError, match="Unsafe"):
        module.FleetRecordingClient(stats_directory=str(tmp_path))


def test_benchmark_limits_raise_only_the_current_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SSH defaults must not cap native reads at 1024 descriptors."""
    # Third Party
    import goal5_async_fleet_probe as module

    limits = {
        module.resource.RLIMIT_NOFILE: (1024, 524288),
        module.resource.RLIMIT_MEMLOCK: (8388608, 8388608),
    }
    monkeypatch.setattr(module.resource, "getrlimit", lambda k: limits[k])
    monkeypatch.setattr(
        module.resource, "setrlimit", lambda k, v: limits.update({k: v})
    )
    result = module.benchmark_limits()
    assert result["nofile"] == (350000, 524288)
    assert result["memlock"] == (
        module.resource.RLIM_INFINITY,
        module.resource.RLIM_INFINITY,
    )
    limits[module.resource.RLIMIT_NOFILE] = (1024, 1024)
    with pytest.raises(ValueError, match="descriptor"):
        module.benchmark_limits()
