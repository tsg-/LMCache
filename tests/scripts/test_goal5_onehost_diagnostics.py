# SPDX-License-Identifier: Apache-2.0
"""Read-only controls and evidence rejection for one-host diagnostics."""

# Standard
from pathlib import Path
from types import ModuleType
import importlib.util
import sys

# Third Party
import pytest


def driver() -> ModuleType:
    """Import the diagnostic driver without contacting the lab."""
    path = Path(__file__).parents[2] / "scripts/ipu-poc/goal5_onehost_diagnostics.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("goal5_onehost_diagnostics", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config() -> dict:
    """Return the command inputs needed for a retained read corpus."""
    return {
        "run_id": "fresh-diagnostic",
        "corpus_run_id": "retained-baseline",
        "fio_corpus_run_id": "retained-fio",
        "geometry": {
            "profile_sha256": "abcdef123456",
            "objects_per_submit": 56,
            "task_size_bytes": 56 * 65536,
        },
        "hosts": {"mmgi1": {"numa": 1, "python": "/bench/python"}},
        "backend": "xfs",
        "profile": "mixtral.yaml",
        "dataset_submits": 256,
        "load_workers": 32,
        "duration_sec": 60,
        "warmup_sec": 90,
    }


def test_controls_reuse_only_retained_read_files() -> None:
    """Diagnostic identities must not redirect reads to an unseeded universe."""
    module = driver()
    cfg = config()
    argv = module.commands(cfg, {"kind": "fio", "depth": 1})[0]
    assert "--iodepth=1" in argv and "--numjobs=32" in argv
    assert "--readonly" in argv and "--rw=randread" in argv
    assert "--directory=/mnt/lmcache/mmgi1/fio/retained-fio" in argv
    assert "--allow_file_create=0" in argv
    commands = module.commands(
        cfg, {"kind": "lmcache", "workers": 32, "in_flight": 8, "processes": 4}
    )
    assert len(commands) == 4
    ports = [argv[argv.index("--serve-metrics") + 1] for argv in commands]
    assert len(set(ports)) == 4
    for argv in commands:
        assert argv[argv.index("--only") + 1] == "load"
        assert argv[argv.index("--key-prefix") + 1] == "retained-baseline-abcdef123456"
        assert "--write-key-prefix" not in argv


def test_successful_exit_cannot_accept_partial_or_short_load() -> None:
    """Reject bad evidence even when the process itself exited successfully."""
    module = driver()
    cfg = config()
    cell = {"kind": "lmcache", "workers": 32, "in_flight": 32, "processes": 1}
    argv = module.commands(cfg, cell)[0]
    row = {
        "exit_code": 0,
        "command": argv,
        "result": {
            "metrics": {
                "config": {
                    "l2_adapter_json": argv[argv.index("--l2-adapter") + 1],
                    "in_flight": 32,
                    "warmup_sec": 90,
                    "duration_sec": 60,
                },
                "geometry": cfg["geometry"],
                "op_0": {
                    "operation": "Load",
                    "submits": 10,
                    "total_keys": 560,
                    "total_success": 560,
                    "total_success_bytes": 10 * 56 * 65536,
                    "window_sec": 60,
                    "phase_start_time_seconds": 100,
                    "phase_end_time_seconds": 160,
                    "submit_latency_count": 10,
                },
            }
        },
    }
    assert module.validate_load(row, cfg, argv)["successful_bytes"] == 10 * 56 * 65536
    op = row["result"]["metrics"]["op_0"]
    op["total_success"] = 559
    with pytest.raises(ValueError, match="Load evidence"):
        module.validate_load(row, cfg, argv)
    op["total_success"] = 560
    op["phase_end_time_seconds"] = 159
    with pytest.raises(ValueError, match="Load evidence"):
        module.validate_load(row, cfg, argv)


def test_sample_interval_rejects_inadequate_coverage() -> None:
    """CPU/depth results require actual samples covering the measured phase."""
    module = driver()
    with pytest.raises(ValueError, match="coverage"):
        module.sample_interval([{"time": 101}, {"time": 159}], 100, 160)
    values = [{"time": 100 + i / 10} for i in range(601)]
    assert len(module.sample_interval(values, 100, 160)) == 601
