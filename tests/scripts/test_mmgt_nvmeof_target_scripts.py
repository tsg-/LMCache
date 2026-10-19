# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the MMGT 4x4 NVMe-oF target helpers."""

# Standard
import os
from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[2]
KERNEL_TARGET_SCRIPT = (
    REPO_ROOT / "scripts/ipu-poc/setup_mmgt_nvmeof_4x4_target.sh"
)
SPDK_TARGET_SCRIPT = (
    REPO_ROOT / "scripts/ipu-poc/setup_mmgt_spdk_nvmf_4x4_target.sh"
)
SERIAL_PCI_MAP_SCRIPT = (
    REPO_ROOT / "scripts/ipu-poc/create_mmgt_solidigm_serial_pci_map.sh"
)


def test_kernel_target_uses_live_ipu4_serial() -> None:
    """Keep the IPU4 namespace serial aligned with the live target inventory."""
    script = KERNEL_TARGET_SCRIPT.read_text()

    assert "PHCP438000041P9AGN" in script
    assert "PHCP438000041P1P9AGN" not in script


def test_spdk_target_refuses_to_start_without_serial_pci_map(
    tmp_path: Path,
) -> None:
    """Do not start SPDK before a reviewed serial-to-PCI map exists."""
    invocation_log = tmp_path / "target-invoked"
    fake_target = tmp_path / "fake-nvmf-tgt"
    fake_target.write_text(
        "#!/usr/bin/env bash\n"
        f"touch {invocation_log}\n"
    )
    fake_target.chmod(0o755)

    result = subprocess.run(
        ["bash", str(SPDK_TARGET_SCRIPT), "up"],
        check=False,
        capture_output=True,
        env={
            **os.environ,
            "SPDK_TGT": str(fake_target),
            "SPDK_RPC": str(fake_target),
            "SPDK_MAP": str(tmp_path / "missing-map"),
        },
        text=True,
    )

    assert result.returncode != 0
    assert "serial-to-PCI map" in result.stderr
    assert not invocation_log.exists()


def test_serial_pci_map_generator_has_exact_solidigm_allowlist() -> None:
    """Generate maps only for the 16 approved Solidigm namespaces."""
    script = SERIAL_PCI_MAP_SCRIPT.read_text()

    assert script.count("PHCP") == 16
    assert "PHCP438000041P9AGN" in script
    assert "PHCP438000041P1P9AGN" not in script
    assert 'dirname "$(dirname "$(readlink -f' in script


def test_spdk_preflight_reports_serial_pci_map_verification() -> None:
    """Make map cardinality and uniqueness checks visible to the operator."""
    script = SPDK_TARGET_SCRIPT.read_text()

    assert (
        "validated serial-to-PCI map: 16 rows, 16 unique serials, "
        "16 unique PCI BDFs"
    ) in script


def test_spdk_preflight_requires_enabled_and_isolated_iommu_groups() -> None:
    """Require the kernel IOMMU setting and vfio ownership of each full group."""
    script = SPDK_TARGET_SCRIPT.read_text()

    assert "intel_iommu=on" in script
    assert "/iommu_group" in script
    assert "IOMMU group" in script


def test_spdk_preflight_excludes_os_home_and_var_storage() -> None:
    """Reject a map that includes storage backing protected host mounts."""
    script = SPDK_TARGET_SCRIPT.read_text()

    assert "assert_os_storage_excluded" in script
    assert "for mountpoint in / /home /var" in script
    assert (
        "validated serial-to-PCI map excludes storage backing /, /home, and /var"
    ) in script
    assert script.index("assert_map") < script.index("assert_os_storage_excluded")


def test_spdk_preflight_checks_runtime_hugepages_and_rdma_endpoints() -> None:
    """Require capacity, hugepages, and an active RDMA path per target IP."""
    script = SPDK_TARGET_SCRIPT.read_text()

    assert "SPDK_RUNTIME_MIN_KB" in script
    assert "SPDK_RUNTIME_MIN_INODES" in script
    assert "SPDK_HUGEPAGES_MIN" in script
    assert "mountpoint -q /dev/hugepages" in script
    assert "rdma link show" in script
    assert "validated active RDMA endpoint" in script


def test_target_startup_paths_are_mutually_exclusive() -> None:
    """Keep kernel nvmet and SPDK NVMf from owning the same endpoints."""
    kernel_script = KERNEL_TARGET_SCRIPT.read_text()
    spdk_script = SPDK_TARGET_SCRIPT.read_text()

    assert "assert_no_spdk_target" in kernel_script
    assert "nvmf_tgt" in kernel_script
    assert "assert_no_spdk_target\n    preflight" in kernel_script
    assert "systemctl is-active --quiet nvmet.service" in spdk_script
    assert "systemctl is-enabled --quiet nvmet.service" in spdk_script
    assert "kernel nvmet ports exist" in spdk_script
