# SPDX-License-Identifier: Apache-2.0
"""Run read-only MiniMax lane scale-out cells from mmgi1."""

from __future__ import annotations

# Standard
import argparse
import json
import os
import re
import shlex
import statistics
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


HOSTS = ("mmgi0", "mmgi1", "mmgi2", "mmgi3")
PORTS = {"mmgi0": 9101, "mmgi1": 9102, "mmgi2": 9103, "mmgi3": 9104}
GROUPS = (
    ("mmgi0",),
    ("mmgi1",),
    ("mmgi2",),
    ("mmgi3",),
    ("mmgi0", "mmgi1"),
    ("mmgi2", "mmgi3"),
    HOSTS,
)
REPO = "/root/anthropic/LMCache"
PROFILE = "scripts/ipu-poc/models/minimax_m3_bf16_tp8_odirect.yaml"
CORPUS = "minimaxqp-20260924-0142"
BENCH_SCRIPT = """\
set -euo pipefail
host=$1
output=$2
in_flight=$3
mkdir -m 700 -p -- "$(dirname "$output")"
cd /root/anthropic/LMCache
base="/mnt/lmcache/bench-l2/minimaxqp-20260924-0142/$(hostname -s)"
[ -d "$base" ]
[ -n "$(find "$base" -maxdepth 1 -name '*.data' -print -quit)" ]
BASE_PATH="$base" PREFIX=minimaxqp-20260924-0142 \
PYTHON=/root/anthropic/LMCache/.venv-bench-l2/bin/python \
OUTPUT="$output" IN_FLIGHT="$in_flight" NUM_WORKERS=32 \
WARMUP_SEC=90 DURATION_SEC=60 METRICS_PORT="$host" \
bash scripts/ipu-poc/run_model_geometry.sh sustained-load \
  scripts/ipu-poc/models/minimax_m3_bf16_tp8_odirect.yaml
"""
PREFLIGHT_SCRIPT = """\
set -euo pipefail
[ "$(findmnt -rn -M /mnt/lmcache -o SOURCE)" = /dev/md0 ]
[ "$(< /sys/block/md0/md/array_state)" != broken ]
members=$(find /sys/block/md0/slaves -mindepth 1 -maxdepth 1 -name 'nvme*n*')
[ "$(printf '%s\\n' "$members" | sed '/^$/d' | wc -l)" -eq 4 ]
for ctl in /sys/class/nvme/nvme[1-4]; do
  [ "$(cat "$ctl/state")" = live ]
  [ "$(cat "$ctl/queue_count")" = 129 ]
done
if pgrep -af '[l]mcache.cli.main bench l2|[f]io|[n]vme connect'; then
  exit 2
fi
base="/mnt/lmcache/bench-l2/minimaxqp-20260924-0142/$(hostname -s)"
file=$(find "$base" -maxdepth 1 -name '*.data' -print -quit)
[ -n "$file" ]
head -c 4096 "$file" >/dev/null
sha256sum /root/anthropic/LMCache/scripts/ipu-poc/run_model_geometry.sh \
  /root/anthropic/LMCache/scripts/ipu-poc/models/minimax_m3_bf16_tp8_odirect.yaml \
  /root/anthropic/LMCache/lmcache/cli/commands/bench/l2_adapter_bench/runner.py \
  /root/anthropic/LMCache/lmcache/cli/commands/bench/l2_adapter_bench/metrics.py
"""
ACC_MAPPING_SCRIPT = """\
set -euo pipefail
for binding in acc2:IPU1 acc1:IPU2 acc3:IPU3 acc4:IPU4; do
  IFS=: read -r acc ipu <<< "$binding"
  [ "$(sed -n 's/^ACC_GRPC_NETNS=//p' "/etc/default/acc-grpc-$acc")" = "$ipu" ]
done
"""


def _ssh(host: str, args: list[str], script: str, timeout: float) -> str:
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=5",
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=2",
            host,
            shlex.join(["bash", "-s", "--", *args]),
        ],
        input=script,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"{host}: exit {result.returncode}: {result.stdout[-2000:]}"
            f"{result.stderr[-2000:]}"
        )
    return result.stdout


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def preflight(hosts: tuple[str, ...]) -> dict[str, str]:
    """Require healthy QP128 storage and identical benchmark inputs."""
    _ssh("mmgt", [], ACC_MAPPING_SCRIPT, 10)
    with ThreadPoolExecutor(max_workers=len(HOSTS)) as pool:
        checks = {
            host: pool.submit(_ssh, host, [], PREFLIGHT_SCRIPT, 25) for host in HOSTS
        }
        hashes = {host: check.result() for host, check in checks.items()}
    if len(set(hashes.values())) != 1:
        raise RuntimeError("initiator benchmark code or profile hashes differ")
    for host in hosts:
        if not hashes[host]:
            raise RuntimeError(f"{host}: empty preflight")
    return hashes


def cells() -> list[tuple[tuple[str, ...], int, int]]:
    """Interleave lane groups and in-flight settings across repetitions."""
    return [
        (hosts, in_flight, repetition)
        for repetition in (1, 2, 3)
        for hosts in GROUPS
        for in_flight in (2, 4)
    ]


def _cell_name(hosts: tuple[str, ...], in_flight: int, repetition: int) -> str:
    lanes = "".join(host[-1] for host in hosts)
    return f"lanes{lanes}-if{in_flight}-rep{repetition}"


def run_cell(
    run_id: str,
    directory: Path,
    fleet_script: Path,
    hosts: tuple[str, ...],
    in_flight: int,
    repetition: int,
) -> dict[str, Any]:
    """Run selected initiators and capture one phase-aligned fleet result."""
    name = _cell_name(hosts, in_flight, repetition)
    output_template = str(directory / f"{name}-{{host}}.json")
    fleet_path = directory / f"{name}-fleet.json"
    with ThreadPoolExecutor(max_workers=len(hosts) + 1) as pool:
        benches = {
            host: pool.submit(
                _ssh,
                host,
                [str(PORTS[host]), output_template.format(host=host), str(in_flight)],
                BENCH_SCRIPT,
                220,
            )
            for host in hosts
        }
        fleet = pool.submit(
            subprocess.run,
            [
                "/usr/bin/python3",
                str(fleet_script),
                "--run-id",
                run_id,
                "--in-flight",
                str(in_flight),
                "--duration-sec",
                "60",
                "--mode",
                "load",
                "--hosts",
                *hosts,
                "--host-result-template",
                output_template,
                "--output",
                str(fleet_path),
            ],
            capture_output=True,
            text=True,
            timeout=210,
            check=False,
        )
        errors = {}
        for host, future in benches.items():
            try:
                (directory / f"{name}-{host}.log").write_text(future.result())
            except Exception as exc:
                errors[host] = str(exc)
        fleet_process = fleet.result()
    if fleet_process.returncode:
        errors["fleet"] = fleet_process.stderr[-4000:]
    if errors:
        raise RuntimeError(f"{name}: {errors}")
    result = json.loads(fleet_path.read_text())
    if not result["valid"] or set(result["host_results"]) != set(hosts):
        raise RuntimeError(f"{name}: incomplete fleet result")
    return result


def _extension_cells(
    completed: dict[str, dict[str, Any]],
) -> list[tuple[tuple[str, ...], int, int]]:
    extensions = []
    for hosts in GROUPS:
        medians = {}
        for in_flight in (2, 4):
            values = [
                completed[_cell_name(hosts, in_flight, repetition)]["counter_deltas"][
                    "app_goodput_gbps_decimal"
                ]
                for repetition in (1, 2, 3)
            ]
            medians[in_flight] = statistics.median(values)
        if medians[4] > 1.05 * medians[2]:
            extensions.extend((hosts, 8, repetition) for repetition in (1, 2, 3))
    return extensions


def execute(run_id: str, directory: Path, fleet_script: Path) -> None:
    """Run the matrix, extending groups whose IF4 median exceeds IF2."""
    directory.mkdir(parents=True, exist_ok=False)
    status_path = directory / "status.json"
    status: dict[str, Any] = {
        "run_id": run_id,
        "state": "preflight",
        "started_at": time.time(),
        "completed": {},
    }
    _write_json(status_path, status)
    try:
        for hosts, in_flight, repetition in cells():
            name = _cell_name(hosts, in_flight, repetition)
            status["current"] = name
            status["state"] = "running"
            status["code_sha256"] = preflight(hosts)
            _write_json(status_path, status)
            print(f"START {name}", flush=True)
            result = run_cell(
                run_id, directory, fleet_script, hosts, in_flight, repetition
            )
            preflight(hosts)
            status["completed"][name] = {
                "hosts": hosts,
                "app_goodput_gbps_decimal": result["counter_deltas"][
                    "app_goodput_gbps_decimal"
                ],
                "rdma_write_gbps_decimal": result["counter_deltas"][
                    "rdma_write_gbps_decimal"
                ],
                "nvme_read_gbps_decimal": result["counter_deltas"][
                    "nvme_read_gbps_decimal"
                ],
                "fleet_result": str(directory / f"{name}-fleet.json"),
            }
            _write_json(status_path, status)
            print(f"DONE {name}", flush=True)
        for hosts, in_flight, repetition in _extension_cells(
            {
                name: json.loads(Path(row["fleet_result"]).read_text())
                for name, row in status["completed"].items()
            }
        ):
            name = _cell_name(hosts, in_flight, repetition)
            status["current"] = name
            status["code_sha256"] = preflight(hosts)
            _write_json(status_path, status)
            print(f"START {name}", flush=True)
            result = run_cell(
                run_id, directory, fleet_script, hosts, in_flight, repetition
            )
            preflight(hosts)
            status["completed"][name] = {
                "hosts": hosts,
                "app_goodput_gbps_decimal": result["counter_deltas"][
                    "app_goodput_gbps_decimal"
                ],
                "rdma_write_gbps_decimal": result["counter_deltas"][
                    "rdma_write_gbps_decimal"
                ],
                "nvme_read_gbps_decimal": result["counter_deltas"][
                    "nvme_read_gbps_decimal"
                ],
                "fleet_result": str(directory / f"{name}-fleet.json"),
            }
            _write_json(status_path, status)
            print(f"DONE {name}", flush=True)
        status["state"] = "complete"
        status["finished_at"] = time.time()
        status["current"] = None
        _write_json(status_path, status)
    except Exception as exc:
        status["state"] = "failed"
        status["error"] = str(exc)
        status["finished_at"] = time.time()
        _write_json(status_path, status)
        raise


def main() -> None:
    """Run a detached lane sweep with one owned results directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--fleet-script", type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"goal5-lanes-[0-9]{8}-[0-9]{6}", args.run_id):
        parser.error("invalid Goal 5 lane run ID")
    if args.root.name != args.run_id:
        parser.error("--root must end in --run-id")
    execute(args.run_id, args.root, args.fleet_script)


if __name__ == "__main__":
    main()
