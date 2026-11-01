# SPDX-License-Identifier: Apache-2.0
"""Check that telemetry cannot hang on removed devices or delayed PCM reads."""

# Standard
from pathlib import Path
import os
import shutil
import subprocess
import sys

# Third Party
import pytest

BIN = (
    Path(__file__).parents[2]
    / "docs/design/v1/platform/ipu-poc/instrumentation/host/bin"
)


@pytest.fixture
def collector_env(tmp_path: Path) -> dict[str, str]:
    """Provide local namespaces and a controlled external NVMe CLI."""
    commands = tmp_path / "commands"
    commands.mkdir()
    output = tmp_path / "output"
    output.mkdir()
    sysfs = tmp_path / "sys"
    for controller, transport in [("nvme0", "pcie"), ("nvme1", "rdma")]:
        directory = sysfs / "class/nvme" / controller
        directory.mkdir(parents=True)
        (directory / "transport").write_text(transport)
        namespace = sysfs / "class/block" / f"{controller}n1" / "device"
        namespace.mkdir(parents=True)
        (namespace / "subsysnqn").write_text(f"nqn.test.{controller}")
    cli = commands / "nvme"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys, time\n"
        "with open(os.environ['CLI_LOG'], 'a') as log:\n"
        " log.write(' '.join(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'list':\n"
        " print(json.dumps({'Devices': [{'DevicePath': '/dev/nvme0n1'},\n"
        "                               {'DevicePath': '/dev/nvme1n1'}]}))\n"
        "elif sys.argv[1] == 'smart-log':\n"
        " if os.environ.get('CLI_STALL'): time.sleep(30)\n"
        " print(json.dumps({'temperature': 310, 'critical_warning': 0}))\n"
        "else: sys.exit(2)\n"
    )
    cli.chmod(0o755)
    # Production uses GNU timeout; use its real behavior on either platform.
    timeout = shutil.which("timeout") or shutil.which("gtimeout")
    if timeout:
        (commands / "timeout").symlink_to(timeout)
    else:
        bounded = commands / "timeout"
        bounded.write_text(
            f"#!{sys.executable}\n"
            "import subprocess, sys\n"
            "try:\n"
            " result = subprocess.run(sys.argv[3:], timeout=float(sys.argv[2]))\n"
            " sys.exit(result.returncode)\n"
            "except subprocess.TimeoutExpired: sys.exit(124)\n"
        )
        bounded.chmod(0o755)
    return {
        **os.environ,
        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
        "OUT_DIR": str(output),
        "SYSFS_ROOT": str(sysfs),
        "CLI_LOG": str(tmp_path / "nvme-calls"),
        "NVME_SMART_TIMEOUT_SECONDS": "1",
    }


def test_nvme_collector_never_issues_admin_io_to_imports(
    collector_env: dict[str, str],
) -> None:
    """Remote namespaces keep their role, without SMART admin commands."""
    result = subprocess.run(
        ["bash", str(BIN / "nvme_stats_textfile.sh")],
        env=collector_env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    metrics = (Path(collector_env["OUT_DIR"]) / "nvme_stats.prom").read_text()
    assert 'nvme_smart_field{ns="nvme0n1",field="temperature"} 310' in metrics
    assert 'nvme_namespace_role{ns="nvme1n1",role="imported",' in metrics
    assert 'nvme_smart_available{ns="nvme1n1"} 0' in metrics
    calls = Path(collector_env["CLI_LOG"]).read_text()
    assert "nvme1n1" not in calls
    assert "list" not in calls


def test_nvme_timeout_publishes_failure_and_namespace_roles(
    collector_env: dict[str, str],
) -> None:
    """A stuck local SMART read must not freeze the published snapshot."""
    collector_env["CLI_STALL"] = "1"
    result = subprocess.run(
        ["bash", str(BIN / "nvme_stats_textfile.sh")],
        env=collector_env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    metrics = (Path(collector_env["OUT_DIR"]) / "nvme_stats.prom").read_text()
    assert "nvme_smart_collector_success 0" in metrics
    assert 'nvme_smart_available{ns="nvme0n1"} 0' in metrics
    assert 'nvme_namespace_role{ns="nvme1n1",role="imported",' in metrics
    assert "nvme_smart_field{" not in metrics


@pytest.mark.parametrize("stall", [False, True])
def test_pcm_bounds_and_sample_interval(
    collector_env: dict[str, str], tmp_path: Path, stall: bool
) -> None:
    """Slow PCM reads publish failure, while valid samples carry time bounds."""
    pcm = tmp_path / "pcm"
    pcm.write_text(
        f"#!{sys.executable}\n"
        "import sys, time\n"
        + ("time.sleep(30)\n" if stall else "")
        + "path = next(a[5:] for a in sys.argv if a.startswith('-csv='))\n"
        "with open(path, 'w') as output:\n"
        " output.write('header\\n0,0,0,0,0,0,0,0,120,80(Total)\\n'\n"
        "              '0,0,0,0,0,0,0,0,30,20(Miss)\\n'\n"
        "              '0,0,0,0,0,0,0,0,90,60(Hit)\\n')\n"
    )
    pcm.chmod(0o755)
    collector_env.update(PCM_PCIE_BIN=str(pcm), PCM_PCIE_TIMEOUT_SECONDS="1")
    result = subprocess.run(
        ["bash", str(BIN / "pcm_pcie_textfile.sh")],
        env=collector_env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    metrics = (Path(collector_env["OUT_DIR"]) / "pcm_pcie.prom").read_text()
    assert result.returncode == (1 if stall else 0), result.stderr
    assert f"pcm_pcie_collector_success {0 if stall else 1}" in metrics
    assert "pcm_pcie_collector_start_timestamp_seconds " in metrics
    assert "pcm_pcie_collector_duration_seconds " in metrics
    if stall:
        assert "pcm_pcie_bandwidth_bytes_per_second{" not in metrics
    else:
        assert 'direction="read",classification="hit"} 90' in metrics


@pytest.mark.parametrize("unit", ["pcm-pcie.service", "pcm-memory.service"])
def test_pcm_units_outrank_rdma_completion_threads(unit: str) -> None:
    """PCM visits every CPU, so it must not queue behind FIFO-50 irdma CEQ threads."""
    settings = dict(
        line.split("=", 1)
        for line in (BIN.parent / "systemd" / unit).read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    assert settings.get("CPUSchedulingPolicy") == "fifo"
    assert int(settings.get("CPUSchedulingPriority", "0")) > 50


@pytest.mark.parametrize("present", [False, True])
def test_rdma_collector_reports_missing_counters(
    collector_env: dict[str, str], present: bool
) -> None:
    """Absent hardware counters are an explicit collection failure."""
    if present:
        port = Path(collector_env["SYSFS_ROOT"]) / "class/infiniband/rdma0/ports/1"
        counters = port / "hw_counters"
        counters.mkdir(parents=True)
        (counters / "InRdmaWrites").write_text("42\n")
        (port / "state").write_text("4: ACTIVE\n")
    result = subprocess.run(
        ["bash", str(BIN / "rdma_hwcounters_textfile.sh")],
        env=collector_env,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    metrics = (Path(collector_env["OUT_DIR"]) / "rdma_hwcounters.prom").read_text()
    assert f"rdma_hw_collector_success {1 if present else 0}" in metrics
    if present:
        assert 'counter="InRdmaWrites"} 42' in metrics
        assert 'rdma_port_up{device="rdma0",port="1"} 1' in metrics
    else:
        assert "rdma_hw_counter{" not in metrics
