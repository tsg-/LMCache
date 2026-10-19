# SPDX-License-Identifier: Apache-2.0
"""Compare idle ACC core use with telemetry collectors off, on, and off."""

from __future__ import annotations

# Standard
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))
from acc_ssh_stats import sample_remote


ACC_NETNS = {"acc1": "IPU2", "acc2": "IPU1", "acc3": "IPU3", "acc4": "IPU4"}
EXPORTER = "acc-grpc-exporter.service"
STATS_TIMER = "acc-stats.timer"
STATS_SERVICE = "acc-stats.service"


def parse_proc_stat(text: str) -> dict[str, tuple[int, ...]]:
    """Parse only per-core CPU jiffies from an ACC /proc/stat response."""
    rows: dict[str, tuple[int, ...]] = {}
    for line in text.splitlines():
        parts = line.strip().split()
        if not parts or not parts[0].startswith("cpu") or not parts[0][3:].isdigit():
            continue
        rows[parts[0]] = tuple(int(value) for value in parts[1:9])
    if not rows:
        raise ValueError("ACC response contained no per-core /proc/stat rows")
    return rows


def busy_from_snapshots(
    before: dict[str, tuple[int, ...]], after: dict[str, tuple[int, ...]]
) -> dict[str, float | int]:
    """Compute core equivalents from the first eight Linux CPU time fields."""
    if set(before) != set(after):
        raise ValueError("ACC CPU set changed between snapshots")
    busy = total = 0
    for core in before:
        delta = [
            end - start for start, end in zip(before[core], after[core], strict=True)
        ]
        if any(value < 0 for value in delta):
            raise ValueError(f"{core}: /proc/stat counter decreased")
        total += sum(delta)
        busy += sum(delta) - delta[3] - delta[4]
    if total <= 0:
        raise ValueError("ACC /proc/stat did not advance")
    cores = len(before)
    return {
        "cores": cores,
        "busy_percent": 100 * busy / total,
        "cores_equivalent": cores * busy / total,
    }


def _read_raw() -> dict[str, dict[str, Any]]:
    readings: dict[str, dict[str, Any]] = {}
    for acc, netns in ACC_NETNS.items():
        started = time.time()
        values = parse_proc_stat(sample_remote(netns, "cat /proc/stat"))
        readings[acc] = {"timestamp": (started + time.time()) / 2, "values": values}
        if len(values) != 4:
            raise ValueError(f"{acc}: expected four ACC cores, got {len(values)}")
    return readings


def _unit(action: str, *units: str) -> None:
    subprocess.run(["systemctl", action, *units], check=True, timeout=30)


def _is_active(unit: str) -> bool:
    return (
        subprocess.run(
            ["systemctl", "is-active", "--quiet", unit], check=False
        ).returncode
        == 0
    )


def _window(name: str, duration: float) -> dict[str, Any]:
    print(f"{name}: sampling {duration}s idle window", flush=True)
    first = _read_raw()
    time.sleep(duration)
    last = _read_raw()
    result: dict[str, Any] = {"duration_seconds": duration, "acc": {}}
    for acc in ACC_NETNS:
        row = busy_from_snapshots(first[acc]["values"], last[acc]["values"])
        row["read_span_seconds"] = last[acc]["timestamp"] - first[acc]["timestamp"]
        result["acc"][acc] = row
    result["fleet_cores_equivalent"] = sum(
        row["cores_equivalent"] for row in result["acc"].values()
    )
    return result


def probe(duration: float) -> dict[str, Any]:
    """Run identical raw /proc/stat windows across off, on, and off states."""
    if not os.environ.get("IMC_PASSWORD"):
        raise ValueError("IMC_PASSWORD is required for the independent probe")
    if not _is_active(EXPORTER) or not _is_active(STATS_TIMER):
        raise ValueError("ACC exporter and acc-stats timer must start active")
    try:
        _unit("stop", STATS_TIMER, STATS_SERVICE, EXPORTER)
        off_before = _window("off_before", duration)
        _unit("start", EXPORTER, STATS_TIMER, STATS_SERVICE)
        on = _window("on", duration)
        _unit("stop", STATS_TIMER, STATS_SERVICE, EXPORTER)
        off_after = _window("off_after", duration)
    finally:
        _unit("start", EXPORTER, STATS_TIMER)
    baselines = (
        off_before["fleet_cores_equivalent"],
        off_after["fleet_cores_equivalent"],
    )
    return {
        "window_seconds": duration,
        "off_before": off_before,
        "on": on,
        "off_after": off_after,
        "collector_overhead_cores_equivalent": (
            on["fleet_cores_equivalent"] - sum(baselines) / 2
        ),
        "off_window_drift_cores_equivalent": abs(baselines[1] - baselines[0]),
        "services_restored": _is_active(EXPORTER) and _is_active(STATS_TIMER),
    }


def main() -> None:
    """Write the idle overhead comparison as an evidence JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=90)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = probe(args.duration)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        "collector overhead core equivalents:",
        result["collector_overhead_cores_equivalent"],
    )


if __name__ == "__main__":
    main()
