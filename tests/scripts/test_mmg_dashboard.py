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


def test_mmg_dashboard_filters_negative_ddio_absorption() -> None:
    """DDIO absorption hides negative lower bounds without a Boolean cast."""
    dashboard = json.loads(DASHBOARD.read_text())
    absorption_panel = next(
        panel
        for panel in dashboard["panels"]
        if panel["title"] == "Target DDIO Absorption"
    )

    expression = absorption_panel["targets"][0]["expr"]
    assert expression.endswith(">= 0")
    assert ">= bool 0" not in expression


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


def test_mmg_dashboard_documents_the_authoritative_four_ipu_map() -> None:
    """Target IPU labels must identify their serving initiator and address."""
    dashboard = json.loads(DASHBOARD.read_text())
    target_busy = next(
        panel
        for panel in dashboard["panels"]
        if panel["title"] == "Target IPU Cores Busy"
    )

    description = target_busy["description"]

    assert "IPU1 is acc2 / 200.0.5.2 / mmgi0 (POC-003)" in description
    assert "IPU2 is acc1 / 200.0.6.2 / mmgi1 (POC-001)" in description
    assert "IPU3 is acc3 / 200.0.7.2 / mmgi2 (B14-P9)" in description
    assert "IPU4 is acc4 / 200.0.8.2 / mmgi3 (POC-002)" in description


def test_mmg_dashboard_falcon_payload_averages_the_selected_range() -> None:
    """Falcon history uses exact counter growth over the selected range."""
    dashboard = json.loads(DASHBOARD.read_text())
    falcon_payload = next(
        panel
        for panel in dashboard["panels"]
        if panel["title"] == "Falcon RDMA payload (bits/s)"
    )

    expressions = [target["expr"] for target in falcon_payload["targets"]]

    assert expressions == [
        (
            'sum(increase(acc_tele_field{host="mmgt",'
            'field="bytes_to_ulp"}[$__range])) / $__range_s * 8'
        ),
        (
            'sum(increase(acc_tele_field{host="mmgt",'
            'field="bytes_from_ulp_rc"}[$__range])) / $__range_s * 8'
        ),
    ]


def test_mmg_dashboard_uses_four_ipu_tele_cli_rdm_payloads() -> None:
    """RDMA summaries and every IPU card must use the four-IPU source."""
    dashboard = json.loads(DASHBOARD.read_text())
    panels = {panel["title"]: panel for panel in dashboard["panels"]}
    ipu_details = next(
        panel for panel in dashboard["panels"] if panel["title"] == "IPU adapter details"
    )
    panels.update({panel["title"]: panel for panel in ipu_details["panels"]})

    assert panels["RDMA RD"]["targets"][0]["expr"] == (
        'sum(rate(acc_tele_field{host="mmgt",'
        'field="bytes_to_ulp"}[90s])) * 8'
    )
    assert panels["RDMA WR"]["targets"][0]["expr"] == (
        'sum(rate(acc_tele_field{host="mmgt",'
        'field="bytes_from_ulp_rc"}[90s])) * 8'
    )
    for title, acc, field in (
        ("IPU1 · RDMA RD", "acc2", "bytes_to_ulp"),
        ("IPU1 · RDMA WR", "acc2", "bytes_from_ulp_rc"),
        ("IPU2 · RDMA RD", "acc1", "bytes_to_ulp"),
        ("IPU2 · RDMA WR", "acc1", "bytes_from_ulp_rc"),
        ("IPU3 · RDMA RD", "acc3", "bytes_to_ulp"),
        ("IPU3 · RDMA WR", "acc3", "bytes_from_ulp_rc"),
        ("IPU4 · RDMA RD", "acc4", "bytes_to_ulp"),
        ("IPU4 · RDMA WR", "acc4", "bytes_from_ulp_rc"),
    ):
        assert panels[title]["targets"][0]["expr"] == (
            'sum(rate(acc_tele_field{host="mmgt",'
            f'acc="{acc}",field="{field}"}}[90s])) * 8'
        )


def test_mmg_dashboard_uses_current_target_devices_and_run_history() -> None:
    """Dashboard controls must cover raw 16-drive exports and selected ranges."""
    dashboard = json.loads(DASHBOARD.read_text())
    variables = {variable["name"]: variable for variable in dashboard["templating"]["list"]}
    titles = {panel["title"] for panel in dashboard["panels"]}
    runs = next(
        panel
        for panel in dashboard["panels"]
        if panel["title"] == "Runs in selected range — model, operation, phase, in-flight"
    )
    rdma_write = next(panel for panel in dashboard["panels"] if panel["title"] == "RDMA WR")

    assert variables["device"]["allValue"] == (
        "nvme(2|3|4|5|6|7|8|9|12|13|14|15|16|17|18|19)n1"
    )
    assert variables["device"]["query"] == (
        'label_values(node_disk_written_bytes_total{host="mmgt",'
        'device=~"nvme(2|3|4|5|6|7|8|9|12|13|14|15|16|17|18|19)n1"}, device)'
    )
    assert "LMCache / NVMe RD reconciliation" not in titles
    assert runs["targets"][0]["expr"] == (
        'last_over_time(lmcache_bench_l2_in_flight_target{job="lmcache_bench_mmg",'
        'host=~"mmgi.*"}[$__range])'
    )
    assert rdma_write["targets"][0]["expr"] == (
        'sum(rate(acc_tele_field{host="mmgt",'
        'field="bytes_from_ulp_rc"}[90s])) * 8'
    )
