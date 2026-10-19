# SPDX-License-Identifier: Apache-2.0
"""Shade a valid fleet result's measured overlap in the MMGT dashboard."""

from __future__ import annotations

# Standard
import argparse
import base64
import json
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DASHBOARD_UID = "mmgt-storage-target"


def annotation_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Build one Grafana region from a valid fleet measured overlap."""
    if result.get("valid") is not True:
        raise ValueError("fleet result is not valid")
    starts = result["phase_starts"]
    ends = result["host_results"]
    if len(starts) != 4 or len(ends) != 4:
        raise ValueError("measured phase needs four host timestamps")
    start = max(float(value) for value in starts.values())
    end = min(float(row["phase_end_time_seconds"]) for row in ends.values())
    if start <= 0 or end <= start:
        raise ValueError("measured overlap has invalid timestamps")
    run_id = str(result["run_id"])
    in_flight = int(result["in_flight"])
    return {
        "dashboardUID": DASHBOARD_UID,
        "time": round(start * 1000),
        "timeEnd": round(end * 1000),
        "tags": ["MMG measured", f"{run_id}-if{in_flight}"],
        "text": f"{run_id}, in_flight={in_flight}, measured overlap",
    }


def grafana_url() -> str:
    """Find the running Grafana listener from Docker's published port."""
    output = subprocess.check_output(
        ["docker", "port", "lmcache-grafana", "3000"], text=True
    )
    endpoint = output.splitlines()[0].strip()
    return f"http://{endpoint}"


def grafana_password(env_file: Path) -> str:
    """Read the locally provisioned Grafana password without printing it."""
    for line in env_file.read_text().splitlines():
        if line.startswith("GRAFANA_PASSWORD="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise ValueError("GRAFANA_PASSWORD is missing")


def publish(result: dict[str, Any], base_url: str, password: str) -> dict[str, Any]:
    """Create a Grafana region once for this fleet cell."""
    payload = annotation_payload(result)
    tag = payload["tags"][1]
    auth = base64.b64encode(f"admin:{password}".encode()).decode()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    params = urllib.parse.urlencode({"dashboardUID": DASHBOARD_UID, "tags": tag})
    headers = {"Authorization": f"Basic {auth}"}
    request = urllib.request.Request(
        f"{base_url}/api/annotations?{params}", headers=headers
    )
    with opener.open(request, timeout=5) as response:
        existing = json.load(response)
    if existing:
        return {"id": existing[0]["id"], "created": False}
    request = urllib.request.Request(
        f"{base_url}/api/annotations",
        data=json.dumps(payload).encode(),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(request, timeout=5) as response:
        created = json.load(response)
    return {"id": created["id"], "created": True}


def main() -> None:
    """Publish measured regions for valid fleet results on the monitor host."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", nargs="+", type=Path)
    parser.add_argument(
        "--grafana-env",
        type=Path,
        default=Path("/root/lmcache-telemetry/.env"),
    )
    args = parser.parse_args()
    url = grafana_url()
    password = grafana_password(args.grafana_env)
    for path in args.results:
        result = json.loads(path.read_text())
        response = publish(result, url, password)
        print(f"{path}: annotation {response['id']} created={response['created']}")


if __name__ == "__main__":
    main()
