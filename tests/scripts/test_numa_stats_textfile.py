# SPDX-License-Identifier: Apache-2.0
"""Tests for the node-exporter NUMA textfile collector."""

# Standard
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[2]
COLLECTOR = (
    ROOT
    / "docs/design/v1/platform/ipu-poc/instrumentation/host/bin"
    / "numa_stats_textfile.sh"
)


def _write_node(node_dir: Path, node: int, cpulist: str) -> None:
    """Create the kernel files needed for one synthetic NUMA node."""
    node_dir.mkdir(parents=True)
    (node_dir / "cpulist").write_text(f"{cpulist}\n")
    (node_dir / "meminfo").write_text(
        f"Node {node} MemTotal: 100 kB\n"
        f"Node {node} MemFree: 20 kB\n"
        f"Node {node} MemUsed: 80 kB\n"
    )
    (node_dir / "numastat").write_text(
        "numa_hit 10\n"
        "numa_miss 2\n"
        "numa_foreign 3\n"
        "local_node 7\n"
        "other_node 1\n"
    )


def test_collector_exports_cpu_seconds_by_numa_node(tmp_path: Path) -> None:
    """CPU counters are grouped using each node's Linux CPU list."""
    nodes = tmp_path / "nodes"
    _write_node(nodes / "node0", 0, "0-1")
    _write_node(nodes / "node1", 1, "2")
    proc_stat = tmp_path / "stat"
    proc_stat.write_text(
        "cpu 0 0 0 0 0 0 0 0 0 0\n"
        "cpu0 100 20 30 40 5 6 7 8 9 10\n"
        "cpu1 200 10 50 60 4 3 2 1 0 0\n"
        "cpu2 300 0 70 80 9 8 7 6 5 4\n"
    )
    output_dir = tmp_path / "textfile"
    output_dir.mkdir()

    result = subprocess.run(
        ["bash", str(COLLECTOR)],
        check=False,
        env=os.environ
        | {
            "OUT_DIR": str(output_dir),
            "SYS_NODE_DIR": str(nodes),
            "PROC_STAT": str(proc_stat),
            "CLK_TCK": "100",
        },
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    rendered = (output_dir / "numa_stats.prom").read_text()
    assert 'numa_node_cpu_seconds_total{node="0",mode="user"} 3.000000' in rendered
    assert 'numa_node_cpu_seconds_total{node="0",mode="idle"} 1.000000' in rendered
    assert 'numa_node_cpu_seconds_total{node="1",mode="system"} 0.700000' in rendered
