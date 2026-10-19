# SPDX-License-Identifier: Apache-2.0
"""Check continuity of MMGT target throughput counters over a long range."""

from __future__ import annotations

# Standard
import argparse
import json
import math
import re
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any


QUERIES = {
    "rdma_write": (
        'sum(rate(acc_tele_field{job="acc_grpc",host="mmgt",'
        'field="bytes_from_ulp_rc"}[30s])) * 8 and on() '
        '(acc_telemetry_exporter_complete{job="acc_grpc",host="mmgt"} == 1)'
    ),
    "rdma_read": (
        'sum(rate(acc_tele_field{job="acc_grpc",host="mmgt",'
        'field="bytes_to_ulp"}[30s])) * 8 and on() '
        '(acc_telemetry_exporter_complete{job="acc_grpc",host="mmgt"} == 1)'
    ),
    "nvme_read": (
        'sum(rate(node_disk_read_bytes_total{host="mmgt",'
        'device=~"EXPORTED_DEVICES"}[30s])) * 8'
    ),
    "nvme_write": (
        'sum(rate(node_disk_written_bytes_total{host="mmgt",'
        'device=~"EXPORTED_DEVICES"}[30s])) * 8'
    ),
}


def assess_points(
    points: list[list[float | str]], start: float, end: float, step: int
) -> dict[str, int | float | bool | None]:
    """Check that every expected time has a finite, nonnegative rate."""
    expected = math.floor((end - start) / step) + 1
    values = [float(point[1]) for point in points]
    timestamps = {round(float(point[0])) for point in points}
    grid = {round(start + index * step) for index in range(expected)}
    missing = len(grid - timestamps)
    negative = sum(value < 0 for value in values)
    finite = all(math.isfinite(value) for value in values)
    return {
        "expected_points": expected,
        "observed_points": len(points),
        "missing_points": missing,
        "negative_points": negative,
        "min_bits_per_second": min(values) if values else None,
        "valid": len(points) == expected and missing == 0 and negative == 0 and finite,
    }


def prometheus_url() -> str:
    """Find the monitor's current published Prometheus listener."""
    output = subprocess.check_output(
        ["docker", "port", "lmcache-prom", "9090"], text=True
    )
    return f"http://{output.splitlines()[0].strip()}"


def query_range(
    base_url: str, expression: str, start: float, end: float, step: int
) -> list[list[float | str]]:
    """Read one instant-rate series across the requested Prometheus range."""
    params = urllib.parse.urlencode(
        {"query": expression, "start": start, "end": end, "step": step}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(f"{base_url}/api/v1/query_range?{params}")
    with opener.open(request, timeout=30) as response:
        payload = json.load(response)
    if payload["status"] != "success":
        raise ValueError(f"Prometheus query failed: {payload.get('error')}")
    series = payload["data"]["result"]
    if len(series) > 1:
        raise ValueError(f"expected one aggregate series, got {len(series)}")
    return series[0]["values"] if series else []


def exported_devices(base_url: str) -> list[str]:
    """Read the target's current 16 exported namespaces from Prometheus."""
    params = urllib.parse.urlencode(
        {"query": 'nvme_namespace_role{host="mmgt",role="exported"}'}
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(f"{base_url}/api/v1/query?{params}")
    with opener.open(request, timeout=10) as response:
        payload = json.load(response)
    if payload["status"] != "success":
        raise ValueError(f"namespace query failed: {payload.get('error')}")
    devices = sorted({row["metric"]["ns"] for row in payload["data"]["result"]})
    if len(devices) != 16 or any(
        re.fullmatch(r"nvme[0-9]+n[0-9]+", name) is None for name in devices
    ):
        raise ValueError(f"expected 16 exported NVMe namespaces, got {devices}")
    return devices


def check_range(start: float, end: float, step: int) -> dict[str, Any]:
    """Assess the four persistent target-rate signals."""
    url = prometheus_url()
    devices = exported_devices(url)
    device_regex = "|".join(devices)
    checks = {
        name: assess_points(
            query_range(
                url, query.replace("EXPORTED_DEVICES", device_regex), start, end, step
            ),
            start,
            end,
            step,
        )
        for name, query in QUERIES.items()
    }
    return {
        "start_timestamp_seconds": start,
        "end_timestamp_seconds": end,
        "step_seconds": step,
        "exported_devices": devices,
        "signals": checks,
        "valid": all(row["valid"] for row in checks.values()),
        "scope": (
            "persistent target RDMA/NVMe signals; md0 reconnect and "
            "app phase gaps excluded"
        ),
    }


def main() -> None:
    """Write a durable pass or failure record for a long dashboard range."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--step", type=int, default=16)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.step <= 0:
        parser.error("--step must be positive")
    start = datetime.fromisoformat(args.start.replace("Z", "+00:00")).timestamp()
    end = datetime.fromisoformat(args.end.replace("Z", "+00:00")).timestamp()
    if end <= start:
        parser.error("--end must follow --start")
    try:
        result = check_range(start, end, args.step)
    except Exception as exc:
        result = {"valid": False, "error": str(exc)}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    if not result["valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
