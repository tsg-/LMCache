# SPDX-License-Identifier: Apache-2.0
"""Tests for MMG monitoring-stack collector checks."""

# Standard
from pathlib import Path


ROOT = Path(__file__).parents[2]
UP_SCRIPT = ROOT / "docs/design/v1/platform/ipu-poc/instrumentation/up.sh"


def test_up_checks_deployed_qp_and_acc_collectors() -> None:
    """Freshness checks match the collector inventory on each initiator."""
    contents = UP_SCRIPT.read_text()

    for host in ("mmgi0", "mmgi1", "mmgi2", "mmgi3"):
        assert f"{host}:nvmeof_qp.prom:90" in contents
    for host in ("mmgi0", "mmgi1", "mmgi3"):
        assert f"{host}:acc_transport.prom:30" in contents
        assert f"{host}:acc_stats.prom:90" in contents
    assert "mmgi2:acc_transport.prom:30" not in contents
    assert "mmgi2:acc_stats.prom:90" not in contents
