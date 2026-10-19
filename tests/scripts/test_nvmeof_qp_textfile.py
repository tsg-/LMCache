# SPDX-License-Identifier: Apache-2.0
"""Tests for the NVMe-oF configured-QP textfile collector."""

# Standard
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[2]
COLLECTOR = (
    ROOT
    / "docs/design/v1/platform/ipu-poc/instrumentation/host/bin"
    / "nvmeof_qp_textfile.sh"
)


def _write_controller(
    sysfs_root: Path, name: str, transport: str, queue_count: int, state: str
) -> None:
    """Create the kernel files needed for one synthetic NVMe controller."""
    controller = sysfs_root / name
    controller.mkdir(parents=True)
    (controller / "transport").write_text(f"{transport}\n")
    (controller / "queue_count").write_text(f"{queue_count}\n")
    (controller / "state").write_text(f"{state}\n")


def test_collector_exports_rdma_io_qps_without_admin_or_pcie(
    tmp_path: Path,
) -> None:
    """Only RDMA I/O queues contribute to the configured QP total."""
    sysfs_root = tmp_path / "nvme"
    _write_controller(sysfs_root, "nvme0", "pcie", 4, "live")
    _write_controller(sysfs_root, "nvme1", "rdma", 129, "live")
    _write_controller(sysfs_root, "nvme2", "rdma", 129, "live")
    output_dir = tmp_path / "textfile"
    output_dir.mkdir()

    result = subprocess.run(
        ["bash", str(COLLECTOR)],
        check=False,
        env=os.environ
        | {
            "OUT_DIR": str(output_dir),
            "SYS_CLASS_NVME": str(sysfs_root),
        },
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    rendered = (output_dir / "nvmeof_qp.prom").read_text()
    assert 'nvmeof_configured_io_qps{controller="nvme1",state="live"} 128' in rendered
    assert 'nvmeof_configured_io_qps{controller="nvme2",state="live"} 128' in rendered
    assert 'controller="nvme0"' not in rendered
    assert "nvmeof_configured_controller_count 2" in rendered
    assert "nvmeof_configured_io_qps_total 256" in rendered
