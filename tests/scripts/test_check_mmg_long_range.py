# SPDX-License-Identifier: Apache-2.0
"""Acceptance gate for the long MMGT throughput view."""

# Standard
import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).parents[2] / "scripts/ipu-poc/check_mmg_long_range.py"
SPEC = importlib.util.spec_from_file_location("check_mmg_long_range", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_complete_nonnegative_range_passes() -> None:
    """A rate at every four-second evaluation fills the intended range."""
    result = MODULE.assess_points(
        [[100.0, "0"], [104.0, "10"], [108.0, "3"]], 100, 108, 4
    )
    assert result["valid"] is True
    assert result["missing_points"] == 0


def test_gap_or_negative_rate_fails() -> None:
    """A line gap or reset artifact cannot pass by averaging available points."""
    gap = MODULE.assess_points([[100.0, "0"], [108.0, "3"]], 100, 108, 4)
    negative = MODULE.assess_points(
        [[100.0, "0"], [104.0, "-2"], [108.0, "3"]], 100, 108, 4
    )
    assert gap["valid"] is False
    assert gap["missing_points"] == 1
    assert negative["valid"] is False
    assert negative["negative_points"] == 1


def test_range_end_between_steps_does_not_require_an_extra_point() -> None:
    """Prometheus stops at the last evaluation at or before the range end."""
    points = [[100 + index * 16, "1"] for index in range(8)]
    result = MODULE.assess_points(points, 100, 220, 16)
    assert result["valid"] is True
    assert result["expected_points"] == 8
