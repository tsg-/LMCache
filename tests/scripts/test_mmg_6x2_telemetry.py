# SPDX-License-Identifier: Apache-2.0
"""Check the deployed six-initiator QP emitter against controller inventories."""

# Standard
from pathlib import Path
import json
import os
import shutil
import subprocess

# Third Party
import pytest

ROOT = Path(__file__).parents[2]
INSTRUMENTATION = ROOT / "docs/design/v1/platform/ipu-poc/instrumentation"
EMITTER = INSTRUMENTATION / "profiles/6x2/host/bin/emit-nvmeof-qps"


@pytest.mark.parametrize(
    ("controllers", "expected"),
    [
        (
            [
                ("nqn.2026-10.io.lmcache.mmg.ns6x8:ipu3-mmgi4", 129),
                ("nqn.2026-10.io.lmcache.mmg.ns6x8:ipu4-mmgi4", 129),
                ("nqn.2026-10.unrelated:test", 129),
            ],
            256,
        ),
        (
            [
                ("nqn.2026-10.lab.mmgt.6x2:ipu1", 129),
                ("nqn.2026-10.lab.mmgt2:ipu5", 3),
                ("nqn.2026-10XlabYmmgtZ6x2:unrelated", 129),
            ],
            130,
        ),
        (
            [
                ("nqn.2026-10.io.lmcache.mmg.ns6x8:ipu3-mmgi4", 0),
                ("nqn.2026-10.io.lmcache.mmg.ns6x8:ipu4-mmgi4", 1),
            ],
            0,
        ),
    ],
)
def test_qp_emitter_counts_only_workload_io_queues(
    tmp_path: Path, controllers: list[tuple[str, int]], expected: int
) -> None:
    """Private and legacy controllers contribute I/O queues, excluding admin."""
    inventory = tmp_path / "controllers"
    output = tmp_path / "textfile"
    inventory.mkdir()
    output.mkdir()
    for index, (nqn, queues) in enumerate(controllers):
        controller = inventory / f"nvme{index}"
        controller.mkdir()
        (controller / "subsysnqn").write_text(nqn)
        (controller / "queue_count").write_text(str(queues))
    script = tmp_path / "emit"
    script.write_text(
        EMITTER.read_text()
        .replace("/sys/class/nvme/", str(inventory) + "/")
        .replace("/var/lib/lmcache-6x2/textfile", str(output))
    )

    subprocess.run(["bash", str(script)], check=True)

    samples = [
        line
        for line in (output / "nvmeof_qps.prom").read_text().splitlines()
        if not line.startswith("#")
    ]
    assert samples == [f"nvmeof_configured_io_qps_total {expected}"]
    assert sorted(path.name for path in output.iterdir()) == ["nvmeof_qps.prom"]


def test_dashboard_queries_isolate_targets_and_exclude_boot_devices(
    tmp_path: Path,
) -> None:
    """Target selection must preserve host identity and exclude unexported I/O."""
    promtool = shutil.which("promtool")
    remote = os.environ.get("MMG_PROMTOOL_HOST")
    if not promtool and not remote:
        pytest.skip("promtool or MMG_PROMTOOL_HOST is required")
    dashboard = json.loads(
        (INSTRUMENTATION / "dashboards/lmcache-6x2.json").read_text()
    )
    panels = {panel["id"]: panel for panel in dashboard["panels"]}
    input_series = [
        {
            "series": 'node_disk_read_bytes_total{host="mmgt",device="nvme0n1"}',
            "values": "0+100x60",
        },
        {
            "series": 'node_disk_read_bytes_total{host="mmgt",device="nvme9n1"}',
            "values": "0+10000x60",
        },
        {
            "series": 'node_disk_read_bytes_total{host="mmgt2",device="nvme0n1"}',
            "values": "0+10000x60",
        },
        {
            "series": 'nvme_namespace_role{host="mmgt",ns="nvme0n1",role="exported"}',
            "values": "1+0x60",
        },
        {
            "series": 'nvme_namespace_role{host="mmgt",ns="nvme9n1",role="local"}',
            "values": "1+0x60",
        },
        {
            "series": 'nvme_namespace_role{host="mmgt2",ns="nvme0n1",role="local"}',
            "values": "1+0x60",
        },
        {
            "series": 'up{job="node_6x2",host="mmgt"}',
            "values": "1+0x60",
        },
        {
            "series": 'up{job="node_6x2",host="mmgt2"}',
            "values": "1+0x60",
        },
    ]
    tests = []
    for selection, reads, counts in [
        (
            "mmgt",
            [{"labels": '{host="mmgt"}', "value": 800}],
            [
                {"labels": '{host="mmgt"}', "value": 1},
            ],
        ),
        ("mmgt2", [], [{"labels": '{host="mmgt2"}', "value": 0}]),
        (
            "mmgt|mmgt2",
            [{"labels": '{host="mmgt"}', "value": 800}],
            [
                {"labels": '{host="mmgt"}', "value": 1},
                {"labels": '{host="mmgt2"}', "value": 0},
            ],
        ),
    ]:
        for panel_id, samples in [(45, reads), (100, counts)]:
            expression = panels[panel_id]["targets"][0]["expr"]
            tests.append(
                {
                    "expr": expression.replace("${target:regex}", selection).replace(
                        "${device:regex}", ".*"
                    ),
                    "eval_time": "60s",
                    "exp_samples": samples,
                }
            )
    fixture = json.dumps(
        {
            "rule_files": [],
            "evaluation_interval": "1s",
            "tests": [
                {
                    "interval": "1s",
                    "input_series": input_series,
                    "promql_expr_test": tests,
                }
            ],
        }
    )
    if promtool:
        path = tmp_path / "queries.yml"
        path.write_text(fixture)
        command = [promtool, "test", "rules", str(path)]
    else:
        command = [
            "ssh",
            "-o",
            "BatchMode=yes",
            str(remote),
            "docker exec -i lmcache-prom-6x2 sh -c "
            "'p=$(mktemp /tmp/mmg-query-test.XXXXXX); "
            'trap "rm -f $p" EXIT; cat > "$p"; promtool test rules "$p"\'',
        ]
    subprocess.run(command, input=fixture, text=True, check=True)
