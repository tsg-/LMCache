# SPDX-License-Identifier: Apache-2.0
"""Behavioral tests for the FIO capacity-sweep shell runner."""

# Standard
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts/ipu-poc/run_fio_capacity_sweep.sh"


def _write_fake_command(directory: Path, name: str, body: str) -> None:
    """Create one minimal command shim for the runner's dry-run preflight."""
    path = directory / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)


def test_remote_xfs_dry_run_uses_requested_work_directory(tmp_path: Path) -> None:
    """A safe XFS_DIR override reaches generated FIO jobs and run metadata."""
    mount = tmp_path / "mount"
    corpus = mount / "kvcache"
    requested_work_dir = mount / "fio-isolated"
    corpus.mkdir(parents=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_command(fake_bin, "id", "echo 0")
    _write_fake_command(fake_bin, "fio", 'test "$1" = "--version" && echo fio-test')
    _write_fake_command(fake_bin, "iostat", "exit 0")
    _write_fake_command(fake_bin, "pidstat", "exit 0")
    _write_fake_command(fake_bin, "mountpoint", "exit 0")
    _write_fake_command(fake_bin, "df", 'printf "Avail\\n500G\\n"')

    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "XFS_MOUNT": str(mount),
        "XFS_DIR": str(requested_work_dir),
        "OUT_BASE": str(tmp_path / "out"),
        "RUN_ID": "override",
        "BS_LIST": "256k",
        "QD_MAIN": "64",
        "QD_SECOND": "64",
        "REPS": "0",
        "MIX_BS": "256k",
        "MIX_RATIOS": "83",
    }
    result = subprocess.run(
        [str(SCRIPT), "--surface", "remote_xfs", "--mixed-only", "--dry-run"],
        capture_output=True,
        env=env,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    out_dir = tmp_path / "out" / "override"
    assert f"devices={requested_work_dir}" in (out_dir / "context.txt").read_text()
    job = out_dir / "jobs" / "mixed83_remote_xfs_bs256k_qd64_rep1.fio"
    assert f"directory={requested_work_dir}" in job.read_text()


def test_remote_xfs_rejects_requested_directory_inside_corpus(tmp_path: Path) -> None:
    """An override cannot direct mixed writes into the benchmark corpus."""
    mount = tmp_path / "mount"
    corpus = mount / "kvcache"
    corpus.mkdir(parents=True)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_fake_command(fake_bin, "id", "echo 0")
    _write_fake_command(fake_bin, "fio", 'test "$1" = "--version" && echo fio-test')
    _write_fake_command(fake_bin, "iostat", "exit 0")
    _write_fake_command(fake_bin, "pidstat", "exit 0")
    _write_fake_command(fake_bin, "mountpoint", "exit 0")
    _write_fake_command(fake_bin, "df", 'printf "Avail\\n500G\\n"')

    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "XFS_MOUNT": str(mount),
        "XFS_DIR": str(corpus / "fio-isolated"),
        "OUT_BASE": str(tmp_path / "out"),
        "RUN_ID": "overlap",
    }
    result = subprocess.run(
        [str(SCRIPT), "--surface", "remote_xfs", "--mixed-only", "--dry-run"],
        capture_output=True,
        env=env,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "sweep directory would overlap the bench l2 corpus" in result.stdout
