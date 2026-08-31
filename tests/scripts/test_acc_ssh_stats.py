# SPDX-License-Identifier: Apache-2.0
"""Behavior tests for the MMG ACC SSH textfile collector."""

# Standard
import importlib.util
from pathlib import Path


ROOT = Path(__file__).parents[2]
COLLECTOR = ROOT / "scripts/ipu-poc/acc_ssh_stats.py"


def _load_collector():
    """Load the standalone collector script as a module."""
    spec = importlib.util.spec_from_file_location("acc_ssh_stats", COLLECTOR)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_core_textfile_does_not_publish_transport_counters(tmp_path: Path) -> None:
    """Core sampling cannot duplicate the transport counter series."""
    collector = _load_collector()
    output = tmp_path / "acc_stats.prom"

    collector.write_core_prom_textfile(output, [("acc1", {"cpu": 50.0})])

    text = output.read_text()
    assert 'acc_cpu_busy_percent{acc="acc1",core="cpu"} 50.00' in text
    assert "acc_tele_field" not in text


def test_transport_textfile_does_not_publish_core_busy_gauges(tmp_path: Path) -> None:
    """Fast transport polling cannot overwrite the 30-second core gauge."""
    collector = _load_collector()
    output = tmp_path / "acc_transport.prom"

    collector.write_transport_prom_textfile(
        output,
        [("acc1", [("global", "bytes_from_ulp_rc", 1234)])],
    )

    text = output.read_text()
    assert (
        'acc_tele_field{acc="acc1",section="global",field="bytes_from_ulp_rc"} 1234'
    ) in text
    assert "acc_cpu_busy_percent" not in text
