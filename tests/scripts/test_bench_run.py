# SPDX-License-Identifier: Apache-2.0
"""Tests for the public bench_run.sh isolation-helper interface."""

# Standard
import subprocess
import time
from pathlib import Path

# Third Party
import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "bench_run.sh"


@pytest.mark.parametrize("path_count", [1, 2])
def test_cleanup_owner_preserves_then_removes_run_dirs(
    tmp_path: Path, path_count: int
) -> None:
    """The cleanup owner retains every path until it receives SIGTERM."""
    base_paths = [tmp_path / f"nvme{index}" for index in range(path_count)]
    process = subprocess.Popen(
        [
            "bash",
            str(SCRIPT),
            "--label",
            "run_under_test",
            "--paths",
            ",".join(str(path) for path in base_paths),
            "--cleanup",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        assert process.stdout is not None
        run_dirs = [Path(process.stdout.readline().strip()) for _ in base_paths]
        assert all(run_dir.is_dir() for run_dir in run_dirs)
        time.sleep(0.1)
        assert process.poll() is None
        assert all(run_dir.is_dir() for run_dir in run_dirs)

        process.terminate()
        _, stderr = process.communicate(timeout=10)

        assert process.returncode == 0
        assert all(not run_dir.exists() for run_dir in run_dirs)
        assert stderr.count("bench_run: cleaned up ") == path_count
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
