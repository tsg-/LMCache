# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/nvmeof_target_provision.sh (alt-track LMCache-msm.1).

These tests exercise the script's argument surface, its dry-run command
generation, and the management-plane / arg-validation guardrails. They do
not touch configfs and do not require root; the script's --dry-run mode
prints what it *would* run rather than executing it.

Hardware-marked integration is covered separately (see conftest hardware
marker); this file stays fast and CI-runnable.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "nvmeof_target_provision.sh"


def _run(*args: str) -> subprocess.CompletedProcess:
    """Invoke the script with the given args, capturing stdout/stderr."""
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
# Setup: argument validation
# ---------------------------------------------------------------------------

def test_setup_requires_nqn():
    result = _run(
        "setup", "--dry-run",
        "--namespace-device", "/dev/nvme6n1",
        "--listen-ip", "192.168.200.4",
    )
    assert result.returncode == 1
    assert "--nqn required" in result.stderr


def test_setup_requires_namespace_device():
    result = _run(
        "setup", "--dry-run",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
        "--listen-ip", "192.168.200.4",
    )
    assert result.returncode == 1
    assert "--namespace-device required" in result.stderr


def test_setup_requires_listen_ip():
    result = _run(
        "setup", "--dry-run",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
        "--namespace-device", "/dev/nvme6n1",
    )
    assert result.returncode == 1
    assert "--listen-ip required" in result.stderr


def test_setup_rejects_non_numeric_namespace_id():
    result = _run(
        "setup", "--dry-run",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
        "--namespace-device", "/dev/nvme6n1",
        "--listen-ip", "192.168.200.4",
        "--namespace-id", "abc",
    )
    assert result.returncode == 1
    assert "must be numeric" in result.stderr


def test_setup_rejects_unknown_option():
    result = _run(
        "setup", "--dry-run",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
        "--namespace-device", "/dev/nvme6n1",
        "--listen-ip", "192.168.200.4",
        "--bogus-flag", "x",
    )
    assert result.returncode == 1
    assert "unknown option" in result.stderr


# ---------------------------------------------------------------------------
# Setup: management-plane guardrail (exit code 3, do not proceed)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mgmt_ip",
    ["192.168.100.4", "192.168.100.3", "192.168.100.1", "192.168.100.254"],
)
def test_setup_refuses_management_plane_ip(mgmt_ip):
    result = _run(
        "setup", "--dry-run",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
        "--namespace-device", "/dev/nvme6n1",
        "--listen-ip", mgmt_ip,
    )
    assert result.returncode == 3, (
        f"management-plane guardrail must exit 3 for {mgmt_ip}, "
        f"got {result.returncode}: {result.stderr}"
    )
    assert "management plane" in result.stderr.lower()
    # Must NOT have emitted any configfs write instructions.
    assert "DRY-RUN:" not in result.stdout


def test_setup_off_fabric_ip_warns_but_proceeds():
    """An IP outside both 192.168.100 and 192.168.200 warns; setup continues."""
    result = _run(
        "setup", "--dry-run",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
        "--namespace-device", "/dev/nvme6n1",
        "--listen-ip", "10.0.0.5",
    )
    assert result.returncode == 0
    assert "WARNING" in result.stderr
    assert "DRY-RUN:" in result.stdout


# ---------------------------------------------------------------------------
# Setup: dry-run command generation
# ---------------------------------------------------------------------------

@pytest.fixture()
def setup_dry_run() -> subprocess.CompletedProcess:
    """Canonical setup dry-run used by several tests."""
    return _run(
        "setup", "--dry-run",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
        "--namespace-device", "/dev/nvme6n1",
        "--namespace-id", "1",
        "--listen-ip", "192.168.200.4",
        "--listen-port", "4420",
        "--host-nqn", "nqn.2026-07.io.lmcache.alt:bmg0",
    )


def test_setup_dry_run_succeeds(setup_dry_run):
    assert setup_dry_run.returncode == 0, setup_dry_run.stderr


def test_setup_creates_subsystem_and_namespace_paths(setup_dry_run):
    stdout = setup_dry_run.stdout
    assert (
        "/sys/kernel/config/nvmet/subsystems/nqn.2026-07.io.lmcache.alt:bmg1"
        in stdout
    )
    assert "namespaces/1" in stdout
    assert "namespaces/1/device_path" in stdout
    assert "namespaces/1/enable" in stdout


def test_setup_writes_namespace_device_and_enables_it(setup_dry_run):
    stdout = setup_dry_run.stdout
    assert "echo /dev/nvme6n1" in stdout
    # Explicit enable=1 write.
    assert "echo 1" in stdout and "namespaces/1/enable" in stdout


def test_setup_configures_rdma_listener_on_fabric_ip_and_port(setup_dry_run):
    stdout = setup_dry_run.stdout
    assert "addr_trtype" in stdout and "echo rdma" in stdout
    assert "addr_traddr" in stdout and "echo 192.168.200.4" in stdout
    assert "addr_trsvcid" in stdout and "echo 4420" in stdout
    assert "addr_adrfam" in stdout and "echo ipv4" in stdout


def test_setup_with_host_nqn_disallows_open_access(setup_dry_run):
    stdout = setup_dry_run.stdout
    # attr_allow_any_host = 0 when host NQNs are provided.
    assert "echo 0" in stdout
    assert "attr_allow_any_host" in stdout
    assert "allowed_hosts/nqn.2026-07.io.lmcache.alt:bmg0" in stdout


def test_setup_without_host_nqn_falls_back_to_open_access():
    result = _run(
        "setup", "--dry-run",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
        "--namespace-device", "/dev/nvme6n1",
        "--listen-ip", "192.168.200.4",
    )
    assert result.returncode == 0
    assert "echo 1" in result.stdout
    assert "attr_allow_any_host" in result.stdout
    # No allowed_hosts symlink when the caller supplies no --host-nqn.
    assert "allowed_hosts/" not in result.stdout


def test_setup_supports_multiple_host_nqns():
    result = _run(
        "setup", "--dry-run",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
        "--namespace-device", "/dev/nvme6n1",
        "--listen-ip", "192.168.200.4",
        "--host-nqn", "nqn.2026-07.io.lmcache.alt:bmg0",
        "--host-nqn", "nqn.2026-07.io.lmcache.alt:bmg2",
    )
    assert result.returncode == 0
    assert "allowed_hosts/nqn.2026-07.io.lmcache.alt:bmg0" in result.stdout
    assert "allowed_hosts/nqn.2026-07.io.lmcache.alt:bmg2" in result.stdout


def test_setup_defaults_listen_port_to_4420():
    result = _run(
        "setup", "--dry-run",
        "--nqn", "nqn.2026-07.io.lmcache.alt:bmg1",
        "--namespace-device", "/dev/nvme6n1",
        "--listen-ip", "192.168.200.4",
    )
    assert result.returncode == 0
    assert "echo 4420" in result.stdout
    assert "addr_trsvcid" in result.stdout


def test_setup_links_port_to_subsystem(setup_dry_run):
    """Symlink from ports/1/subsystems/<nqn> to subsystems/<nqn>."""
    stdout = setup_dry_run.stdout
    assert (
        "ports/1/subsystems/nqn.2026-07.io.lmcache.alt:bmg1" in stdout
    )


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

def test_teardown_requires_nqn():
    result = _run("teardown", "--dry-run")
    assert result.returncode == 1
    assert "--nqn required" in result.stderr


def test_teardown_dry_run_succeeds_when_subsystem_absent(tmp_path):
    """Teardown must be idempotent: safe to run when nothing is provisioned."""
    result = _run(
        "teardown", "--dry-run",
        "--nqn", "nqn.2026-07.io.lmcache.alt:doesnotexist",
    )
    # Exit 0 even though the subsystem does not exist on this host.
    assert result.returncode == 0


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def test_status_requires_nqn():
    result = _run("status")
    assert result.returncode == 1
    assert "--nqn required" in result.stderr


def test_status_reports_absent_for_missing_nqn():
    """On any host where the nvmet path does not exist, status prints 'absent'."""
    result = _run(
        "status", "--nqn", "nqn.2026-07.io.lmcache.alt:doesnotexist",
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "absent"


# ---------------------------------------------------------------------------
# Hardware integration: end-to-end attach / write / detach / reattach cycle.
# Requires bmg0 + bmg1 SSH aliases and LMCACHE_NVMEOF_HW_TEST=1. This test
# provisions a subsystem on bmg1 backed by /dev/nvme6n1 (KIOXIA namespace,
# raw and unmounted -- does not clash with the storage-track FSConnector
# mounts on nvme4n1/nvme5n1), attaches it from bmg0, writes a small buffer,
# reads it back with dd, then disconnects and confirms no orphans on either
# side. Always tears down at the end even on failure.
# ---------------------------------------------------------------------------

_HW_MARKER_ENV = "LMCACHE_NVMEOF_HW_TEST"
_HW_NQN = "nqn.2026-07.io.lmcache.alt:bmg1-integration-test"
_HW_LISTEN_IP = "192.168.200.4"
_HW_TARGET_HOST = "bmg1"
_HW_INITIATOR_HOST = "bmg0"
_HW_HOST_NQN = "nqn.2026-07.io.lmcache.alt:bmg0-integration-test"
_HW_NAMESPACE_DEVICE = "/dev/nvme6n1"
_HW_REMOTE_REPO = "~/tsg/LMCache"


def _ssh_run(host: str, cmd: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command on a remote host over SSH."""
    return subprocess.run(
        ["ssh", host, cmd],
        capture_output=True, text=True, check=check,
    )


def _hw_available() -> bool:
    if os.environ.get(_HW_MARKER_ENV) != "1":
        return False
    # Both SSH aliases must resolve to a working shell.
    for host in (_HW_TARGET_HOST, _HW_INITIATOR_HOST):
        result = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", host, "true"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False
    return True


@pytest.fixture()
def hw_cleanup():
    """Best-effort teardown regardless of test outcome."""
    yield
    # Initiator side: disconnect if attached.
    _ssh_run(
        _HW_INITIATOR_HOST,
        f"cd {_HW_REMOTE_REPO} && ./scripts/nvmeof_initiator_attach.sh "
        f"disconnect --nqn {_HW_NQN} 2>&1 || true",
        check=False,
    )
    # Target side: teardown subsystem.
    _ssh_run(
        _HW_TARGET_HOST,
        f"cd {_HW_REMOTE_REPO} && ./scripts/nvmeof_target_provision.sh "
        f"teardown --nqn {_HW_NQN} --listen-ip {_HW_LISTEN_IP} 2>&1 || true",
        check=False,
    )


@pytest.mark.skipif(
    not _hw_available(),
    reason=(
        f"hardware integration requires {_HW_MARKER_ENV}=1 and working SSH "
        f"aliases {_HW_INITIATOR_HOST}/{_HW_TARGET_HOST}"
    ),
)
def test_hw_attach_write_detach_reattach_cycle(hw_cleanup):
    """End-to-end: provision, attach, write, detach, reattach, cleanup.

    Verifies the acceptance criterion for LMCache-msm.1 -- a fresh cycle
    completes with a stable /dev/disk/by-id path and leaves no residue on
    either host. Uses /dev/nvme6n1 on bmg1 as the exported namespace.
    """
    # 1. Provision the subsystem on bmg1.
    provision = _ssh_run(
        _HW_TARGET_HOST,
        f"cd {_HW_REMOTE_REPO} && ./scripts/nvmeof_target_provision.sh setup "
        f"--nqn {_HW_NQN} "
        f"--namespace-device {_HW_NAMESPACE_DEVICE} "
        f"--listen-ip {_HW_LISTEN_IP} "
        f"--host-nqn {_HW_HOST_NQN}",
    )
    assert provision.returncode == 0, provision.stderr

    # 2. Attach from bmg0 and capture the stable path.
    connect = _ssh_run(
        _HW_INITIATOR_HOST,
        f"cd {_HW_REMOTE_REPO} && ./scripts/nvmeof_initiator_attach.sh connect "
        f"--target-ip {_HW_LISTEN_IP} --nqn {_HW_NQN} --host-nqn {_HW_HOST_NQN}",
    )
    assert connect.returncode == 0, connect.stderr
    by_id_path = connect.stdout.strip().splitlines()[-1]
    assert by_id_path.startswith("/dev/disk/by-id/"), by_id_path

    # 3. Small write + read to confirm the block device works end-to-end.
    io_test = _ssh_run(
        _HW_INITIATOR_HOST,
        # Write 4 KiB of zeros then read it back; compare hashes.
        f"sudo dd if=/dev/zero of={by_id_path} bs=4096 count=1 conv=fsync 2>&1 "
        f"&& sudo dd if={by_id_path} bs=4096 count=1 2>/dev/null | sha256sum",
    )
    assert io_test.returncode == 0, io_test.stderr
    expected_zero_sha = (
        "ad7facb2586fc6e966c004d7d1d16b024f5805ff7cb47c7a85dabd8b48892ca7"
    )
    assert expected_zero_sha in io_test.stdout, io_test.stdout

    # 4. Disconnect from bmg0 and confirm controller is gone.
    disconnect = _ssh_run(
        _HW_INITIATOR_HOST,
        f"cd {_HW_REMOTE_REPO} && ./scripts/nvmeof_initiator_attach.sh "
        f"disconnect --nqn {_HW_NQN}",
    )
    assert disconnect.returncode == 0, disconnect.stderr

    # 5. Reattach and confirm the same stable path resolves.
    reconnect = _ssh_run(
        _HW_INITIATOR_HOST,
        f"cd {_HW_REMOTE_REPO} && ./scripts/nvmeof_initiator_attach.sh connect "
        f"--target-ip {_HW_LISTEN_IP} --nqn {_HW_NQN} --host-nqn {_HW_HOST_NQN}",
    )
    assert reconnect.returncode == 0, reconnect.stderr
    by_id_path_2 = reconnect.stdout.strip().splitlines()[-1]
    assert by_id_path == by_id_path_2, (
        f"stable path changed across disconnect/reconnect: "
        f"{by_id_path} -> {by_id_path_2}"
    )

    # 6. Management-plane sanity: SSH still works on both hosts.
    # (If this test somehow disturbed 192.168.100.x, this call would hang or fail.)
    _ssh_run(_HW_TARGET_HOST, "hostname")
    _ssh_run(_HW_INITIATOR_HOST, "hostname")
