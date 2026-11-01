# SPDX-License-Identifier: Apache-2.0
"""Successful-payload and file isolation contracts for the six-host FIO control."""

# Standard
from pathlib import Path
from types import ModuleType
import importlib.util
import sys

# Third Party
import pytest


def driver() -> ModuleType:
    """Import the lab driver without accessing the lab."""
    path = Path(__file__).parents[2] / "scripts/ipu-poc/goal5_fio.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("goal5_fio", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_jobs_use_private_files_and_explicit_fanout() -> None:
    module = driver()
    cfg = {"run_id": "goal5-test", "hosts": {"mmgi5": {"numa": 1}}}
    args = module.fio_args(cfg, "mmgi5", "64k", "mixed")
    assert "--numjobs=32" in args
    assert "--iodepth=32" in args
    assert "--direct=1" in args
    assert "--rwmixread=83" in args
    assert "--directory=/mnt/lmcache/mmgi5/fio/goal5-test" in args
    assert "--size=16G" in args
    assert not any("/dev/" in arg for arg in args)
    assert "--readonly" in module.fio_args(cfg, "mmgi5", "256k", "read")


def test_bandwidth_uses_successful_bytes_and_actual_runtime() -> None:
    module = driver()
    op = {
        "io_bytes": 200_000_000_000,
        "runtime": 1000,
        "clat_ns": {"percentile": {"50.000000": 1000, "99.000000": 9000}},
    }
    result = module.summarize_fio({"jobs": [{"error": 0, "read": op, "write": op}]})
    assert result["payload_gbps_decimal"] == 3200
    assert result["read"]["p99_io_us"] == 9
    assert result["read_write_ratio_achieved"] == 1
    with pytest.raises(ValueError, match="Failed"):
        module.summarize_fio({"jobs": [{"error": 5, "read": op, "write": op}]})


def test_fio_rejects_short_window_or_wrong_operation_mix() -> None:
    """A successful process cannot substitute a short window or a write for reads."""
    module = driver()
    op = {"io_bytes": 1_000_000, "runtime": 1000}
    with pytest.raises(ValueError, match="window"):
        module.summarize_fio({"jobs": [{"error": 0, "read": op, "write": op}]}, "mixed")
    op["runtime"] = 60_000
    with pytest.raises(ValueError, match="read-only"):
        module.summarize_fio({"jobs": [{"error": 0, "read": op, "write": op}]}, "read")
    with pytest.raises(ValueError, match="ratio"):
        module.summarize_fio({"jobs": [{"error": 0, "read": op, "write": op}]}, "mixed")
