# SPDX-License-Identifier: Apache-2.0
"""Contract tests for the versioned MMG telemetry dashboard."""

# Standard
import json
from pathlib import Path


ROOT = Path(__file__).parents[2]
DASHBOARD = (
    ROOT
    / "docs/design/v1/platform/ipu-poc/instrumentation/dashboards"
    / "mmgt-storage-target.json"
)


def _expressions(panel: dict[str, object]) -> list[str]:
    """Return PromQL expressions from a panel tree."""
    expressions = [
        str(target["expr"])
        for target in panel.get("targets", [])  # type: ignore[union-attr]
        if "expr" in target
    ]
    for child in panel.get("panels", []):  # type: ignore[union-attr]
        expressions.extend(_expressions(child))
    return expressions


def test_mmg_dashboard_tracks_the_repo_owned_metric_contract() -> None:
    """The checked-in dashboard follows the MMG job and NUMA CPU collector."""
    dashboard = json.loads(DASHBOARD.read_text())
    expressions = "\n".join(_expressions({"panels": dashboard["panels"]}))

    assert dashboard["uid"] == "mmgt-storage-target"
    assert "lmcache_bench_mmg" in expressions
    assert "numa_node_cpu_seconds_total" in expressions
    assert "acc_tele_field" in expressions


def test_mmg_dashboard_hides_negative_llc_percentages() -> None:
    """The PCIe LLC ratio is absent, rather than misleading, below zero."""
    dashboard = json.loads(DASHBOARD.read_text())
    expressions = "\n".join(_expressions({"panels": dashboard["panels"]}))

    assert 'classification=~"hit|miss"' in expressions
    assert ">= 0" in expressions


def test_mmg_dashboard_matches_the_target_acc_aggregate_chart() -> None:
    """Initiator ACC telemetry has the target chart's aggregate history view."""
    dashboard = json.loads(DASHBOARD.read_text())
    panels = dashboard["panels"]
    aggregate_panel = next(
        panel
        for panel in panels
        if panel["title"] == "Initiator ACC aggregate busy % by host"
    )

    assert aggregate_panel["type"] == "timeseries"
    assert aggregate_panel["targets"][0]["expr"] == (
        'acc_cpu_busy_percent{host=~"mmgi.*",core="cpu"}'
    )


def test_mmg_dashboard_falcon_payload_derives_over_the_full_panel_range() -> None:
    """Falcon RDMA payload derives over ``$__range``, not a fixed window.

    irdma refreshes ``hw_counters`` asynchronously (roughly 1s), so a fixed
    60s window -- fine for NVMe's per-second host counters -- understated a
    45s run 4x here. ``deriv(...[$__range])`` spans the panel's own time
    range instead, which is why this panel no longer shares NVMe's window.
    """
    dashboard = json.loads(DASHBOARD.read_text())
    falcon_payload = next(
        panel
        for panel in dashboard["panels"]
        if panel["title"] == "Falcon RDMA payload (bits/s)"
    )

    expressions = [target["expr"] for target in falcon_payload["targets"]]

    assert expressions == [
        (
            'clamp_min(sum(deriv(acc_telemetry_bytes_total{host="mmgt",'
            'counter="bytes_to_ulp"}[$__range])), 0) * 8'
        ),
        (
            'clamp_min(sum(deriv(acc_telemetry_bytes_total{host="mmgt",'
            'counter="bytes_from_ulp_rc"}[$__range])), 0) * 8'
        ),
    ]
