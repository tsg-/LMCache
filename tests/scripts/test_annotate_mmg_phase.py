# SPDX-License-Identifier: Apache-2.0
"""Public result-to-annotation contract for MMG measured regions."""

# Standard
import importlib.util
from pathlib import Path

# Third Party
import pytest


SCRIPT = Path(__file__).parents[2] / "scripts/ipu-poc/annotate_mmg_phase.py"
SPEC = importlib.util.spec_from_file_location("annotate_mmg_phase", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_valid_fleet_result_makes_measured_region() -> None:
    """The annotation covers only the shared measured interval."""
    result = {
        "valid": True,
        "run_id": "cell-qps2-rep1",
        "in_flight": 2,
        "phase_starts": {
            "mmgi0": 10.1,
            "mmgi1": 10.25,
            "mmgi2": 10.0,
            "mmgi3": 10.2,
        },
        "host_results": {
            "mmgi0": {"phase_end_time_seconds": 70.1},
            "mmgi1": {"phase_end_time_seconds": 70.25},
            "mmgi2": {"phase_end_time_seconds": 70.0},
            "mmgi3": {"phase_end_time_seconds": 70.2},
        },
    }
    region = MODULE.annotation_payload(result)
    assert region["dashboardUID"] == "mmgt-storage-target"
    assert region["time"] == 10250
    assert region["timeEnd"] == 70000
    assert "cell-qps2-rep1-if2" in region["tags"]


def test_invalid_fleet_result_cannot_shade_dashboard() -> None:
    """A failed cell must not leave a plausible measured region."""
    with pytest.raises(ValueError, match="not valid"):
        MODULE.annotation_payload({"valid": False})
