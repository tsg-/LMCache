# SPDX-License-Identifier: Apache-2.0
"""Tests for the MMG two-initiator FIO geometry sweep."""

# Standard
from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/ipu-poc/run_mmg_geometry_sweep.sh"


def test_sweep_is_self_contained_and_has_no_internal_fix_reference() -> None:
    """The checked-in runner must not depend on local results or internal fixes."""
    contents = SCRIPT.read_text(encoding="utf-8")

    assert "write_job" in contents
    assert "fio-job-templates" not in contents
    assert "results/mmg-nvmeof-rdma-sweep" not in contents
    assert "irdma_clean_cqes" not in contents
    assert "Jijun" not in contents
    assert "mmgi0" not in contents
    assert "mmgi1" not in contents
    assert "/dev/nvme" not in contents
    assert 'ssh "$TARGET_HOST"' in contents
    assert 'for initiator in "${INITIATOR_HOSTS[@]}"' in contents


def test_sweep_loads_the_shared_host_inventory(tmp_path: Path) -> None:
    """The runner takes target and initiator host names from its inventory."""
    inventory = tmp_path / "inventory.env"
    inventory.write_text(
        "INITIATOR_HOSTS=(initiator-a initiator-b)\nTARGET_HOST=target-a\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                'source "$1"; INVENTORY="$2"; FIO_DEVICE_LIST=(/dev/fio-test-a); '
                'validate_inputs; printf "%s|%s\\n" "${INITIATOR_HOSTS[*]}" '
                '"$TARGET_HOST"'
            ),
            "bash",
            str(SCRIPT),
            str(inventory),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "initiator-a initiator-b|target-a\n"


def test_sweep_renders_original_mixed_job_shape(tmp_path: Path) -> None:
    """The generated FIO file retains the intended per-device job layout."""
    job_file = tmp_path / "mixed.fio"
    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                'source "$1"; FIO_DEVICE_LIST=(/dev/fio-test-a /dev/fio-test-b); '
                'write_job "$2" mixed 2 8'
            ),
            "bash",
            str(SCRIPT),
            str(job_file),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    contents = job_file.read_text(encoding="utf-8")
    assert "rw=rw" in contents
    assert "rwmixread=83" in contents
    assert "iodepth=8" in contents
    assert "[fio-test-a-j0]" in contents
    assert "[fio-test-a-j1]" in contents
    assert "[fio-test-b-j0]" in contents
    assert "[fio-test-b-j1]" in contents
    assert "offset=0" in contents
    assert "offset=137438953472" in contents


def test_write_workload_requires_explicit_destructive_opt_in() -> None:
    """A write sweep stops before any remote command without the opt-in."""
    result = subprocess.run(
        [
            "bash",
            "-c",
            (
                'source "$1"; FIO_DEVICE_LIST=(/dev/fio-test-a); '
                "WORKLOADS=(write); validate_inputs"
            ),
            "bash",
            str(SCRIPT),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    assert "ALLOW_DESTRUCTIVE_WRITE=1" in result.stderr
