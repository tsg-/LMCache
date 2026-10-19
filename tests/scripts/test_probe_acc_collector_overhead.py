# SPDX-License-Identifier: Apache-2.0
"""Tests for raw ACC /proc/stat busy arithmetic."""

from __future__ import annotations

# Standard
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


SCRIPT = Path(__file__).parents[2] / "scripts/ipu-poc/probe_acc_collector_overhead.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "probe_acc_collector_overhead", SCRIPT
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_raw_proc_stat_counts_busy_core_equivalents() -> None:
    """The probe counts iowait as idle and does not double-count guest time."""
    probe = _load_module()
    before = {"cpu0": (10, 0, 0, 90, 0, 0, 0, 0)}
    after = {"cpu0": (30, 0, 0, 150, 20, 0, 0, 0)}

    result = probe.busy_from_snapshots(before, after)

    assert result["cores_equivalent"] == 0.2
    assert result["busy_percent"] == 20
    assert result["cores"] == 1
