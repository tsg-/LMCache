# SPDX-License-Identifier: Apache-2.0
"""Reject incomplete async pilot evidence and retain public native counters."""

# Standard
from pathlib import Path
from types import ModuleType
from typing import Any
import importlib
import json
import sys
import time

# Third Party
import pytest


def test_recorder_retains_samples_and_drained_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Closing twice must preserve one counter stream and native close semantics."""
    backend: Any = ModuleType("lmcache_fs_uring")

    class Client:
        """Stand in for the public native counter/close contract."""

        def __init__(self, **kwargs: object) -> None:
            self.pending = 32

        def io_stats(self) -> dict[str, int]:
            """Return issued and consumed counts independently of adapter IF."""
            return {
                "submitted_reads": 56,
                "completed_reads": 56 - self.pending,
                "pending_reads": self.pending,
                "max_worker_pending": 32,
                "max_total_pending": 32,
            }

        def close(self) -> None:
            """Drain native work before reporting its terminal state."""
            self.pending = 0

    backend.LMCacheFSUringClient = Client
    monkeypatch.setitem(sys.modules, "lmcache_fs_uring", backend)
    monkeypatch.syspath_prepend(str(Path(__file__).parents[2] / "scripts/ipu-poc"))
    sys.modules.pop("goal5_async_stats", None)
    module = importlib.import_module("goal5_async_stats")
    output = tmp_path / "stats.jsonl"
    client = module.RecordingFSUringClient(stats_path=str(output))
    time.sleep(0.22)
    client.close()
    client.close()
    rows = [json.loads(line) for line in output.read_text().splitlines()]
    assert len(rows) >= 3
    assert rows[-1]["terminal"] and rows[-1]["stats"]["pending_reads"] == 0
    assert rows[-1]["stats"]["submitted_reads"] == rows[-1]["stats"]["completed_reads"]
    assert any(row["stats"]["pending_reads"] == 32 for row in rows[:-1])


def test_native_depth_evidence_requires_coverage_and_final_drain() -> None:
    """Configuration and a lifetime maximum alone cannot establish measured depth."""
    sys.path.insert(0, str(Path(__file__).parents[2] / "scripts/ipu-poc"))
    module = importlib.import_module("goal5_async_pilot")
    rows: list[dict[str, Any]] = [
        {
            "time": 100 + i / 10,
            "terminal": False,
            "stats": {
                "submitted_reads": 56 + i,
                "completed_reads": i,
                "pending_reads": 56,
                "max_worker_pending": 32,
                "max_total_pending": 56,
            },
        }
        for i in range(601)
    ]
    rows.append(
        {
            "time": 161,
            "terminal": True,
            "stats": {
                "submitted_reads": 656,
                "completed_reads": 656,
                "pending_reads": 0,
                "max_worker_pending": 32,
                "max_total_pending": 56,
            },
        }
    )
    assert module.native_summary(rows, 100, 160)["mean_sampled_pending_reads"] == 56
    rows[-1]["stats"]["pending_reads"] = 1
    with pytest.raises(ValueError, match="drain"):
        module.native_summary(rows, 100, 160)
    rows[-1]["stats"]["pending_reads"] = 0
    with pytest.raises(ValueError, match="coverage"):
        module.native_summary(rows[10:], 100, 160)


def test_pilot_uses_retained_read_keys_and_separate_native_stats() -> None:
    """A fresh result identity must not redirect the pilot to unprepared files."""
    sys.path.insert(0, str(Path(__file__).parents[2] / "scripts/ipu-poc"))
    module = importlib.import_module("goal5_async_pilot")
    cfg = {
        "run_id": "fresh-pilot",
        "corpus_run_id": "retained-corpus",
        "results_dir": "/fresh/results",
        "geometry": {"profile_sha256": "abcdef123456"},
        "hosts": {"mmgi1": {"numa": 1, "python": "/bench/python"}},
        "backend": "xfs",
        "profile": "mixtral.yaml",
        "dataset_submits": 256,
        "load_workers": 32,
        "duration_sec": 60,
        "warmup_sec": 90,
    }
    argv = module.pilot_command(cfg, "r1")
    adapter = json.loads(argv[argv.index("--l2-adapter") + 1])
    assert adapter["type"] == "native_plugin"
    assert adapter["adapter_params"]["base_path"].endswith("/retained-corpus")
    assert adapter["adapter_params"]["stats_path"] == "/fresh/results/r1-native.jsonl"
    assert argv[argv.index("--key-prefix") + 1] == "retained-corpus-abcdef123456"
    assert argv[argv.index("--only") + 1] == "load"
    assert "--write-key-prefix" not in argv
