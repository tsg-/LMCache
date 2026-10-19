# SPDX-License-Identifier: Apache-2.0
"""Tests for the non-disruptive Goal 5 lane matrix."""

from __future__ import annotations

# Standard
import importlib.util
from pathlib import Path
from types import ModuleType


SCRIPT = Path(__file__).parents[2] / "scripts/ipu-poc/goal5_lane_sweep.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("goal5_lane_sweep", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_lane_matrix_covers_single_pairs_and_four_lanes() -> None:
    """Each declared group gets IF2 and IF4 with three repetitions."""
    sweep = _load_module()
    cells = sweep.cells()

    assert len(cells) == 42
    assert len(set(cells)) == 42
    for group in sweep.GROUPS:
        assert {
            (in_flight, repetition)
            for hosts, in_flight, repetition in cells
            if hosts == group
        } == {
            (in_flight, repetition) for in_flight in (2, 4) for repetition in (1, 2, 3)
        }


def test_if8_only_extends_groups_with_more_than_five_percent_gain() -> None:
    """A three-rep median, rather than one fast run, triggers IF8."""
    sweep = _load_module()
    results = {}
    for group in sweep.GROUPS:
        for in_flight in (2, 4):
            for repetition in (1, 2, 3):
                goodput = 100 if in_flight == 2 else 104
                if group == sweep.GROUPS[0] and in_flight == 4:
                    goodput = (100, 110, 110)[repetition - 1]
                results[sweep._cell_name(group, in_flight, repetition)] = {
                    "counter_deltas": {"app_goodput_gbps_decimal": goodput}
                }

    assert sweep._extension_cells(results) == [
        (sweep.GROUPS[0], 8, repetition) for repetition in (1, 2, 3)
    ]
