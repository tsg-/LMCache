# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/nvmeof_initiator_attach.sh (alt-track LMCache-msm.1).

Argument surface, dry-run command generation, and the management-plane
guardrail are covered here without invoking real `nvme` CLI or hardware.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "nvmeof_initiator_attach.sh"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Usage / dispatch
# ---------------------------------------------------------------------------

def test_no_subcommand_prints_usage_and_exits_1():
    result = _run()
    assert result.returncode == 1
    assert "usage" in result.stderr.lower()


def test_unknown_subcommand_exits_1():
    result = _run("frobnicate")
    assert result.returncode == 1
    assert "unknown subcommand" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Connect: argument validation
# ---------------------------------------------------------------------------

def test_connect_requires_target_ip():
    result = _run(
        "connect", "--dry-run",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
    )
    assert result.returncode == 1
    assert "--target-ip required" in result.stderr


def test_connect_requires_nqn():
    result = _run(
        "connect", "--dry-run",
        "--target-ip", "192.168.200.4",
    )
    assert result.returncode == 1
    assert "--nqn required" in result.stderr


def test_connect_rejects_unknown_option():
    result = _run(
        "connect", "--dry-run",
        "--target-ip", "192.168.200.4",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
        "--wat", "1",
    )
    assert result.returncode == 1
    assert "unknown option" in result.stderr


# ---------------------------------------------------------------------------
# Connect: management-plane guardrail
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mgmt_ip",
    ["192.168.100.3", "192.168.100.4", "192.168.100.254"],
)
def test_connect_refuses_management_plane_ip(mgmt_ip):
    result = _run(
        "connect", "--dry-run",
        "--target-ip", mgmt_ip,
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
    )
    assert result.returncode == 3
    lowered = result.stderr.lower()
    assert "management-plane" in lowered or "management plane" in lowered
    # Must not have emitted a nvme connect command.
    assert "nvme connect" not in result.stdout


def test_connect_off_fabric_ip_warns_but_proceeds():
    result = _run(
        "connect", "--dry-run",
        "--target-ip", "10.0.0.5",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
    )
    assert result.returncode == 0
    assert "WARNING" in result.stderr


# ---------------------------------------------------------------------------
# Connect: dry-run command generation
# ---------------------------------------------------------------------------

@pytest.fixture()
def connect_dry_run() -> subprocess.CompletedProcess:
    return _run(
        "connect", "--dry-run",
        "--target-ip", "192.168.200.4",
        "--target-port", "4420",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
        "--host-nqn", "nqn.2026-07.io.lmcache.alt:bmg0",
        "--ctrl-loss-tmo", "60",
        "--reconnect-delay", "5",
    )


def test_connect_dry_run_succeeds(connect_dry_run):
    assert connect_dry_run.returncode == 0, connect_dry_run.stderr


def test_connect_uses_rdma_transport(connect_dry_run):
    stdout = connect_dry_run.stdout
    assert "nvme connect" in stdout
    assert "--transport rdma" in stdout


def test_connect_passes_target_ip_port_and_nqn(connect_dry_run):
    stdout = connect_dry_run.stdout
    assert "--traddr 192.168.200.4" in stdout
    assert "--trsvcid 4420" in stdout
    assert "--nqn nqn.2026-07.io.lmcache.alt:bmg1" in stdout


def test_connect_passes_host_nqn(connect_dry_run):
    assert "--hostnqn nqn.2026-07.io.lmcache.alt:bmg0" in connect_dry_run.stdout


def test_connect_passes_reconnect_tuning(connect_dry_run):
    stdout = connect_dry_run.stdout
    assert "--ctrl-loss-tmo 60" in stdout
    assert "--reconnect-delay 5" in stdout


def test_connect_defaults_target_port_to_4420():
    result = _run(
        "connect", "--dry-run",
        "--target-ip", "192.168.200.4",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
    )
    assert result.returncode == 0
    assert "--trsvcid 4420" in result.stdout


def test_connect_defaults_reconnect_tuning():
    result = _run(
        "connect", "--dry-run",
        "--target-ip", "192.168.200.4",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
    )
    assert result.returncode == 0
    assert "--ctrl-loss-tmo 30" in result.stdout
    assert "--reconnect-delay 2" in result.stdout


def test_connect_prints_stable_by_id_path_on_stdout(connect_dry_run):
    """The last stdout line is the stable /dev/disk/by-id path; callers pipe it."""
    lines = [ln for ln in connect_dry_run.stdout.splitlines() if ln.strip()]
    assert lines, "expected stdout content"
    assert lines[-1].startswith("/dev/disk/by-id/")


def test_connect_without_host_nqn_omits_hostnqn_flag():
    result = _run(
        "connect", "--dry-run",
        "--target-ip", "192.168.200.4",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
    )
    assert result.returncode == 0
    assert "--hostnqn" not in result.stdout


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------

def test_disconnect_requires_nqn():
    result = _run("disconnect", "--dry-run")
    assert result.returncode == 1
    assert "--nqn required" in result.stderr


def test_disconnect_dry_run_emits_nvme_disconnect():
    result = _run(
        "disconnect", "--dry-run",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
    )
    assert result.returncode == 0
    assert "nvme disconnect" in result.stdout
    assert "nqn.2026-07.io.lmcache.alt:bmg1" in result.stdout


# ---------------------------------------------------------------------------
# Path
# ---------------------------------------------------------------------------

def test_path_requires_nqn():
    result = _run("path")
    assert result.returncode == 1
    assert "--nqn required" in result.stderr


def test_path_dry_run_prints_by_id_stub():
    result = _run(
        "path", "--dry-run",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
    )
    assert result.returncode == 0
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    assert lines[-1].startswith("/dev/disk/by-id/")
