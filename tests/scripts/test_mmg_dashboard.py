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
NVME_COLLECTOR = (
    ROOT
    / "docs/design/v1/platform/ipu-poc/instrumentation/host/bin"
    / "nvme_stats_textfile.sh"
)
DATASOURCE = (
    ROOT
    / "docs/design/v1/platform/ipu-poc/instrumentation/provisioning"
    / "datasources/prometheus.yml"
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


def _panels(panel: dict[str, object]) -> list[dict[str, object]]:
    """Flatten a dashboard panel tree for whole-dashboard checks."""
    return [
        child
        for nested in panel.get("panels", [])  # type: ignore[union-attr]
        for child in [nested, *_panels(nested)]
    ]


def _payload_rate(selector: str, window: str) -> str:
    """Expected scrape-time payload rate with a current-read health gate."""
    metric = f'acc_tele_field{{job="acc_grpc",{selector}}}'
    acc = next(
        (
            item.removeprefix('acc="').removesuffix('"')
            for item in selector.split(",")
            if item.startswith('acc="')
        ),
        None,
    )
    gate = (
        f'acc_telemetry_collector_success{{job="acc_grpc",host="mmgt",acc="{acc}"}}'
        if acc
        else 'acc_telemetry_exporter_complete{job="acc_grpc",host="mmgt"}'
    )
    return f"sum(rate({metric}[{window}])) * 8 and on() ({gate} == 1)"


def test_mmg_dashboard_tracks_the_repo_owned_metric_contract() -> None:
    """The checked-in dashboard follows the MMG job and NUMA CPU collector."""
    dashboard = json.loads(DASHBOARD.read_text())
    expressions = "\n".join(_expressions({"panels": dashboard["panels"]}))

    assert dashboard["uid"] == "mmgt-storage-target"
    assert "lmcache_bench_mmg" in expressions
    assert "numa_node_cpu_seconds_total" in expressions
    assert "acc_tele_field" in expressions


def test_mmg_dashboard_displays_measured_phase_regions() -> None:
    """Grafana should render the coordinator's interval annotations."""
    dashboard = json.loads(DASHBOARD.read_text())
    annotations = dashboard["annotations"]["list"]

    assert len(annotations) == 1
    assert annotations[0]["datasource"]["uid"] == "-- Grafana --"
    assert annotations[0]["name"] == "Measured phases"
    assert annotations[0]["enable"] is True
    assert annotations[0]["hide"] is False


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
    assert "IPU4 is acc4 / 200.0.8.2 / mmgi3 (B14-P11)" in description


def test_mmg_dashboard_falcon_payload_tracks_the_rate_window() -> None:
    """Falcon history uses the Grafana rate window."""
    dashboard = json.loads(DASHBOARD.read_text())
    falcon_payload = next(
        panel
        for panel in dashboard["panels"]
        if panel["title"] == "Falcon RDMA payload (bits/s)"
    )

    expressions = [target["expr"] for target in falcon_payload["targets"]]

    assert expressions == [
        _payload_rate('host="mmgt",field="bytes_to_ulp"', "$__rate_interval"),
        _payload_rate('host="mmgt",field="bytes_from_ulp_rc"', "$__rate_interval"),
    ]


def test_mmg_dashboard_uses_four_ipu_grpc_rdm_payloads() -> None:
    """RDMA summaries and every IPU card use the four-IPU gRPC source."""
    dashboard = json.loads(DASHBOARD.read_text())
    panels = {panel["title"]: panel for panel in dashboard["panels"]}
    ipu_details = next(
        panel
        for panel in dashboard["panels"]
        if panel["title"] == "IPU adapter details"
    )
    panels.update({panel["title"]: panel for panel in ipu_details["panels"]})

    assert panels["RDMA RD (last 30s)"]["targets"][0]["expr"] == _payload_rate(
        'host="mmgt",field="bytes_to_ulp"', "30s"
    )
    assert panels["RDMA WR (last 30s)"]["targets"][0]["expr"] == _payload_rate(
        'host="mmgt",field="bytes_from_ulp_rc"', "30s"
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
        assert panels[f"{title} (last 30s)"]["targets"][0]["expr"] == _payload_rate(
            f'host="mmgt",acc="{acc}",field="{field}"', "30s"
        )


def test_mmg_dashboard_uses_current_target_devices_and_run_history() -> None:
    """Dashboard controls must follow the target's current configfs exports."""
    dashboard = json.loads(DASHBOARD.read_text())
    variables = {
        variable["name"]: variable for variable in dashboard["templating"]["list"]
    }
    titles = {panel["title"] for panel in dashboard["panels"]}
    runs = next(
        panel
        for panel in dashboard["panels"]
        if panel["title"]
        == "Runs in selected range — model, operation, phase, in-flight"
    )
    rdma_write = next(
        panel for panel in dashboard["panels"] if panel["title"] == "RDMA WR (last 30s)"
    )

    assert "allValue" not in variables["device"]
    assert variables["device"]["query"] == (
        'label_values(nvme_namespace_role{host="mmgt",role="exported"}, ns)'
    )
    assert "LMCache / NVMe RD reconciliation" not in titles
    assert runs["targets"][0]["expr"] == (
        'last_over_time(lmcache_bench_l2_in_flight_target{job="lmcache_bench_mmg",'
        'host=~"mmgi.*"}[$__range])'
    )
    assert rdma_write["targets"][0]["expr"] == _payload_rate(
        'host="mmgt",field="bytes_from_ulp_rc"', "30s"
    )


def test_mmg_dashboard_documents_grpc_payload_cadence() -> None:
    """Payload panels identify the scrape-time collection path."""
    dashboard = json.loads(DASHBOARD.read_text())
    panels = {panel["title"]: panel for panel in dashboard["panels"]}
    payload_descriptions = "\n".join(
        panels[title]["description"]
        for title in (
            "RDMA WR (last 30s)",
            "RDMA RD (last 30s)",
            "Bidirectional RDMA Payload (last 30s)",
        )
    )
    history = next(
        panel
        for panel in dashboard["panels"]
        if panel["title"] == "Falcon RDMA payload (bits/s)"
    )
    notes = next(
        panel
        for panel in dashboard["panels"]
        if "Live tiles use a" in panel.get("options", {}).get("content", "")
    )

    assert "tele_cli" not in payload_descriptions
    assert "scrape time" in payload_descriptions
    assert "scrape time" in history["description"]
    assert "30s rate" in notes["options"]["content"]


def test_mmg_dashboard_summary_uses_the_measured_bench_window() -> None:
    """The summary goodput tile must exclude warmup traffic."""
    dashboard = json.loads(DASHBOARD.read_text())
    lmcache = next(
        panel for panel in dashboard["panels"] if panel["title"] == "LMCache (last 30s)"
    )

    assert lmcache["targets"][0]["expr"] == (
        "sum by (operation) (rate(lmcache_bench_l2_success_bytes_total{"
        'job="lmcache_bench_mmg",host=~"mmgi.*",phase="measured"}[30s])) * 8 '
        'and on() (count(up{job="lmcache_bench_mmg",host=~"mmgi.*"} == 1) == 4)'
    )
    assert lmcache["fieldConfig"]["defaults"]["noValue"] == "no measured phase"
    trend = next(
        panel
        for panel in dashboard["panels"]
        if panel["title"] == "LMCache aggregate goodput (bits/s)"
    )
    assert (
        "do not average the whole plotted curve for a 60s phase" in trend["description"]
    )


def test_nvme_collector_marks_configfs_backing_devices_as_exported() -> None:
    """The target export set follows configfs instead of unstable NVMe names."""
    contents = NVME_COLLECTOR.read_text()

    assert "/sys/kernel/config/nvmet/subsystems" in contents
    assert "role=exported" in contents


def test_mmg_dashboard_separates_target_nvme_from_initiator_md0() -> None:
    """Target media and initiator RAID0 traffic stay distinct observations."""
    dashboard = json.loads(DASHBOARD.read_text())
    panels = dashboard["panels"]
    titles = {panel["title"]: panel for panel in panels}

    assert "Target physical NVMe namespaces (MMG Target)" in titles
    assert "Initiator md0 / NVMe-oF" in titles

    target_read = titles["NVMe read throughput (bits/s)"]
    initiator_read = titles["Initiator md0 read throughput (bits/s)"]
    initiator_write = titles["Initiator md0 write throughput (bits/s)"]

    assert target_read["targets"][0]["expr"] == (
        'sum(rate(node_disk_read_bytes_total{host="mmgt",'
        'device=~"$device"}[$__rate_interval])) * 8'
    )
    assert initiator_read["targets"][0]["expr"] == (
        'sum by (host) (rate(node_disk_read_bytes_total{host=~"mmgi[0-3]",'
        'device="md0"}[$__rate_interval])) * 8'
    )
    assert initiator_write["targets"][0]["expr"] == (
        'sum by (host) (rate(node_disk_written_bytes_total{host=~"mmgi[0-3]",'
        'device="md0"}[$__rate_interval])) * 8'
    )


def test_mmg_dashboard_places_configured_qps_in_the_summary_row() -> None:
    """QP configuration uses the four-initiator summary-card layout."""
    dashboard = json.loads(DASHBOARD.read_text())
    panel_list = dashboard["panels"]
    panels = {panel["title"]: panel for panel in panel_list}

    target_cpu = panels["Target CPU Busy"]
    target_ipu = panels["Target IPU Cores Busy"]
    initiator_ipu = panels["Initiator IPU Cores Busy"]
    qps = panels["Initiator QPs Configured"]
    initiator_read = panels["Initiator md0 read throughput (bits/s)"]
    initiator_write = panels["Initiator md0 write throughput (bits/s)"]

    assert target_cpu["gridPos"] == {"h": 3, "w": 3, "x": 0, "y": 4}
    assert target_ipu["gridPos"] == {"h": 3, "w": 7, "x": 3, "y": 4}
    assert initiator_ipu["gridPos"] == {"h": 3, "w": 7, "x": 10, "y": 4}
    assert target_ipu["fieldConfig"]["defaults"]["decimals"] == 1
    assert initiator_ipu["fieldConfig"]["defaults"]["decimals"] == 1
    assert [target["legendFormat"] for target in initiator_ipu["targets"]] == [
        "I1",
        "I2",
        "I3",
        "I4",
    ]
    assert qps["type"] == "stat"
    assert qps["gridPos"] == {"h": 3, "w": 7, "x": 17, "y": 4}
    assert qps["options"]["textMode"] == "value_and_name"
    assert [target["legendFormat"] for target in qps["targets"]] == [
        "I1",
        "I2",
        "I3",
        "I4",
    ]
    assert [target["expr"] for target in qps["targets"]] == [
        'sum(nvmeof_configured_io_qps_total{host="mmgi0"}) or vector(NaN)',
        'sum(nvmeof_configured_io_qps_total{host="mmgi1"}) or vector(NaN)',
        'sum(nvmeof_configured_io_qps_total{host="mmgi2"}) or vector(NaN)',
        'sum(nvmeof_configured_io_qps_total{host="mmgi3"}) or vector(NaN)',
    ]
    assert initiator_read["gridPos"]["y"] == 45
    assert initiator_write["gridPos"]["y"] == 45


def test_mmg_dashboard_shows_bidirectional_falcon_payload_in_summary() -> None:
    """The summary has six equal tiles and sums the two Falcon directions."""
    dashboard = json.loads(DASHBOARD.read_text())
    panels = {panel["title"]: panel for panel in dashboard["panels"]}

    summary_titles = [
        "LMCache (last 30s)",
        "NVMe RD (last 30s)",
        "RDMA WR (last 30s)",
        "NVMe WR (last 30s)",
        "RDMA RD (last 30s)",
        "Bidirectional RDMA Payload (last 30s)",
    ]
    assert [panels[title]["gridPos"] for title in summary_titles] == [
        {"h": 3, "w": 4, "x": x, "y": 1} for x in range(0, 24, 4)
    ]
    assert panels["Bidirectional RDMA Payload (last 30s)"]["targets"][0][
        "expr"
    ] == _payload_rate('host="mmgt",field=~"bytes_from_ulp_rc|bytes_to_ulp"', "30s")


def test_scrape_time_payload_queries_gate_incomplete_acc_reads() -> None:
    """Four-ACC rates are absent when the current scrape is incomplete."""
    dashboard = json.loads(DASHBOARD.read_text())
    panels = {panel["title"]: panel for panel in dashboard["panels"]}
    for title in (
        "RDMA WR (last 30s)",
        "RDMA RD (last 30s)",
        "Bidirectional RDMA Payload (last 30s)",
    ):
        expression = panels[title]["targets"][0]["expr"]
        assert 'job="acc_grpc"' in expression
        assert "rate(acc_tele_field" in expression
        assert "acc_telemetry_exporter_complete" in expression
        assert "offset" not in expression
    expressions = "\n".join(_expressions({"panels": dashboard["panels"]}))
    assert "deriv(" not in expressions
    assert " / 30" not in expressions
    for expression in _expressions({"panels": dashboard["panels"]}):
        if "acc_tele_field" in expression:
            assert 'job="acc_grpc"' in expression
            assert "offset" not in expression


def test_mmg_throughput_charts_use_a_four_second_scrape_interval() -> None:
    """Comparable counter rates use the same Grafana window and bit unit."""
    dashboard = json.loads(DASHBOARD.read_text())
    panels = {
        panel["title"]: panel for panel in _panels({"panels": dashboard["panels"]})
    }
    for title in (
        "LMCache aggregate goodput (bits/s)",
        "LMCache goodput by initiator (bits/s)",
        "NVMe read throughput (bits/s)",
        "NVMe write throughput (bits/s)",
        "Initiator md0 read throughput (bits/s)",
        "Initiator md0 write throughput (bits/s)",
        "Per-drive read throughput (bits/s)",
        "Per-drive write throughput (bits/s)",
        "Falcon RDMA payload (bits/s)",
    ):
        panel = panels[title]
        assert panel["fieldConfig"]["defaults"]["unit"] == "bps"
        for expression in _expressions(panel):
            assert "[$__rate_interval]" in expression
    assert "timeInterval: 4s" in DATASOURCE.read_text()


def test_mmg_stat_tiles_use_instant_windows() -> None:
    """A stat does not retain the last point from a finished run."""
    dashboard = json.loads(DASHBOARD.read_text())
    for panel in _panels({"panels": dashboard["panels"]}):
        if panel["type"] == "stat":
            assert all(target["instant"] is True for target in panel["targets"])
            if panel["id"] in (19, 20, 47):
                assert panel["fieldConfig"]["defaults"]["noValue"] == (
                    "no measured phase"
                )
    panels = {panel["title"]: panel for panel in dashboard["panels"]}
    for title in (
        "LMCache (last 30s)",
        "NVMe RD (last 30s)",
        "NVMe WR (last 30s)",
        "RDMA WR (last 30s)",
        "RDMA RD (last 30s)",
    ):
        panel = panels[title]
        assert panel["targets"][0]["instant"] is True
        assert "[30s]" in panel["targets"][0]["expr"]
        assert "live view only" in panel["description"].lower()
