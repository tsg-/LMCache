# SPDX-License-Identifier: Apache-2.0
"""Check the QP fleet sweep's public safety gate."""

# Standard
import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/ipu-poc/run_qp_scale.sh"
INVENTORY = (
    Path(__file__).resolve().parents[2]
    / "scripts/ipu-poc/inventories/mmg-four-initiator.env"
)


def test_fleet_sweep_requires_apply() -> None:
    """Reject controller changes unless the operator explicitly enables them."""
    result = subprocess.run(
        ["bash", str(SCRIPT), "sweep-if-fleet", str(INVENTORY)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "APPLY=1" in result.stderr


def test_fleet_mode_is_documented() -> None:
    """Show the fleet mode and its read-only corpus policy in help."""
    result = subprocess.run(
        ["bash", str(SCRIPT), "help"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "sweep-if-fleet" in result.stdout
    assert "existing read-only MiniMax corpus" in result.stdout


def test_fleet_rejects_qps_above_online_cpu_cap(tmp_path: Path) -> None:
    """An impossible queue request fails before controller reconfiguration."""
    fake_ssh = tmp_path / "ssh"
    fake_ssh.write_text("#!/bin/sh\nprintf '128\\n'\n")
    fake_ssh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "APPLY": "1",
        "RUN_ID": "test-cap",
        "FLEET_QP_COUNTS": "512",
    }

    result = subprocess.run(
        ["bash", str(SCRIPT), "sweep-if-fleet", str(INVENTORY)],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 2
    assert "limits -i 512 to 128 online CPUs" in result.stderr


def test_reconnect_fleet_requires_apply() -> None:
    """A detached controller cannot reconnect storage without APPLY=1."""
    result = subprocess.run(
        ["bash", str(SCRIPT), "reconnect-fleet", "1", str(INVENTORY)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "APPLY=1" in result.stderr
