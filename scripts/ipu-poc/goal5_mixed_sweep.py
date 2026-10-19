# SPDX-License-Identifier: Apache-2.0
"""Run capacity-gated four-lane MiniMax and Mixtral 5:1 load-curve cells."""

from __future__ import annotations

# Standard
import argparse
import json
import os
import re
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


HOSTS = ("mmgi0", "mmgi1", "mmgi2", "mmgi3")
PORTS = {"mmgi0": 9101, "mmgi1": 9102, "mmgi2": 9103, "mmgi3": 9104}
MODELS = {
    "minimax": (
        "minimaxqp-20260924-0142",
        "scripts/ipu-poc/models/minimax_m3_bf16_tp8_odirect.yaml",
    ),
    "mixtral": (
        "mixtral64qp-20260924-1359",
        "scripts/ipu-poc/models/mixtral_8x22b_fp8_64k.yaml",
    ),
}
MODEL_SHA12 = {"minimax": "138e44c4c604", "mixtral": "406f93535503"}
RESERVE_BYTES = 1_000_000_000_000
CELL_HEADROOM_BYTES = 250_000_000_000
PRECONDITION_SEC = 70
MEASURED_SEC = 30

RECLAIM_PYTHON = """\
import json
import os
import re
import sys
from pathlib import Path

base, prefix, expected_count, expected_bytes, mode = sys.argv[1:]
expected_count, expected_bytes = int(expected_count), int(expected_bytes)
if not re.fullmatch(
    r"goal5-mixed-curves-[0-9]{8}-[0-9]{6}-(minimax|mixtral)-"
    r"if[0-9]+-rep[0-9]+-mmgi[0-3]-[0-9a-f]{12}",
    prefix,
):
    raise SystemExit("invalid write prefix")
if mode not in ("verify", "remove") or expected_count <= 0 or expected_bytes <= 0:
    raise SystemExit("invalid reclaim request")
if not Path(base).is_dir() or Path(base).is_symlink():
    raise SystemExit("invalid base directory")
stem = prefix + "-bench-model@"
files = []
count = byte_count = 0
with os.scandir(base) as entries:
    for entry in entries:
        if not entry.name.startswith(stem):
            continue
        if not entry.name.endswith(".data") or not entry.is_file(
            follow_symlinks=False
        ):
            raise SystemExit("unexpected matching Store entry: " + entry.name)
        count += 1
        byte_count += entry.stat(follow_symlinks=False).st_size
        if mode == "remove":
            files.append(entry.path)
if count != expected_count or byte_count != expected_bytes:
    raise SystemExit(
        f"Store manifest mismatch: {count}/{byte_count}, "
        f"expected {expected_count}/{expected_bytes}"
    )
if mode == "remove":
    for path in files:
        os.unlink(path)
print(json.dumps({"files": count, "bytes": byte_count, "mode": mode}))
"""
RECLAIM_SCRIPT = """\
set -euo pipefail
corpus=$1
reported_base=$2
prefix=$3
expected_files=$4
expected_bytes=$5
mode=$6
[ "$(findmnt -rn -M /mnt/lmcache -o SOURCE)" = /dev/md0 ]
base="/mnt/lmcache/bench-l2/$corpus/$(hostname -s)"
[ "$base" = "$reported_base" ]
[ "$(readlink -f "$base")" = "$base" ]
if pgrep -af '[l]mcache.cli.main bench l2|[f]io|[n]vme connect'; then
  exit 2
fi
/usr/bin/python3 - "$base" "$prefix" "$expected_files" "$expected_bytes" "$mode" <<'PY'
__RECLAIM_PYTHON__
PY
if [ "$mode" = remove ]; then
  sync -f "$base"
fi
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
for prefix in minimaxqp-20260924-0142 mixtral64qp-20260924-1359; do
  base="/mnt/lmcache/bench-l2/$prefix/$(hostname -s)"
  file=$(find "$base" -maxdepth 1 -name '*.data' -print -quit)
  [ -n "$file" ]
  head -c 4096 "$file" >/dev/null
done
cd /root/anthropic/LMCache
sha256sum scripts/ipu-poc/run_model_geometry.sh \
  scripts/ipu-poc/models/minimax_m3_bf16_tp8_odirect.yaml \
  scripts/ipu-poc/models/mixtral_8x22b_fp8_64k.yaml \
  lmcache/cli/commands/bench/l2_adapter_bench/runner.py \
  lmcache/cli/commands/bench/l2_adapter_bench/metrics.py
df -B1 --output=avail /mnt/lmcache | tail -1
"""
ACC_MAPPING_SCRIPT = """\
set -euo pipefail
for binding in acc2:IPU1 acc1:IPU2 acc3:IPU3 acc4:IPU4; do
  IFS=: read -r acc ipu <<< "$binding"
  [ "$(sed -n 's/^ACC_GRPC_NETNS=//p' "/etc/default/acc-grpc-$acc")" = "$ipu" ]
done
"""
BENCH_SCRIPT = """\
set -euo pipefail
port=$1
output=$2
in_flight=$3
prefix=$4
profile=$5
stage=$6
write_prefix=$7
mkdir -m 700 -p -- "$(dirname "$output")"
cd /root/anthropic/LMCache
base="/mnt/lmcache/bench-l2/$prefix/$(hostname -s)"
[ -n "$(find "$base" -maxdepth 1 -name '*.data' -print -quit)" ]
if [ "$stage" = precondition ]; then
  BASE_PATH="$base" PREFIX="$prefix" \
  PYTHON=/root/anthropic/LMCache/.venv-bench-l2/bin/python \
  OUTPUT="$output" IN_FLIGHT="$in_flight" NUM_WORKERS=32 \
  WARMUP_SEC=0 DURATION_SEC=70 METRICS_PORT="$port" \
  bash scripts/ipu-poc/run_model_geometry.sh sustained-load "$profile"
else
  BASE_PATH="$base" PREFIX="$prefix" WRITE_PREFIX="$write_prefix" \
  PYTHON=/root/anthropic/LMCache/.venv-bench-l2/bin/python \
  OUTPUT="$output" IN_FLIGHT="$in_flight" NUM_WORKERS=32 \
  READ_WRITE_RATIO=5:1 DURATION_SEC=30 METRICS_PORT="$port" \
  bash scripts/ipu-poc/run_model_geometry.sh mixed "$profile"
fi
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
            f"{host}: exit {result.returncode}: "
            f"{result.stdout[-2000:]}{result.stderr[-2000:]}"
        )
    return result.stdout


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def preflight(min_free_bytes: int) -> dict[str, int]:
    """Require quiet QP128 lanes, matching inputs, and capacity on every host."""
    _ssh("mmgt", [], ACC_MAPPING_SCRIPT, 10)
    with ThreadPoolExecutor(max_workers=len(HOSTS)) as pool:
        futures = {
            host: pool.submit(_ssh, host, [], PREFLIGHT_SCRIPT, 25) for host in HOSTS
        }
        outputs = {host: future.result() for host, future in futures.items()}
    hashes = {}
    free = {}
    for host, output in outputs.items():
        *hash_lines, free_line = output.strip().splitlines()
        hashes[host] = "\n".join(hash_lines)
        free[host] = int(free_line.strip())
        if free[host] < min_free_bytes:
            raise RuntimeError(
                f"{host}: free {free[host]} B below required {min_free_bytes} B"
            )
    if len(set(hashes.values())) != 1:
        raise RuntimeError("initiator benchmark code or profile hashes differ")
    return free


def cells(in_flights: tuple[int, ...] = (1, 2, 4, 8)) -> list[tuple[str, int, int]]:
    """Cover each model and in-flight setting with three repetitions."""
    return [
        (model, in_flight, repetition)
        for repetition in (1, 2, 3)
        for model in MODELS
        for in_flight in (in_flights[::-1] if repetition == 2 else in_flights)
    ]


def _cell_name(model: str, in_flight: int, repetition: int) -> str:
    return f"{model}-if{in_flight}-rep{repetition}"


def _run_benches(
    model: str,
    in_flight: int,
    name: str,
    directory: Path,
    stage: str,
    fleet_script: Path | None = None,
    run_id: str | None = None,
) -> dict[str, Any] | None:
    prefix, profile = MODELS[model]
    output_template = str(directory / f"{name}-{stage}-{{host}}.json")
    write_prefix = f"{run_id}-{name}" if run_id else ""
    with ThreadPoolExecutor(max_workers=len(HOSTS) + 1) as pool:
        benches = {
            host: pool.submit(
                _ssh,
                host,
                [
                    str(PORTS[host]),
                    output_template.format(host=host),
                    str(in_flight),
                    prefix,
                    profile,
                    stage,
                    f"{write_prefix}-{host}",
                ],
                BENCH_SCRIPT,
                130 if stage == "precondition" else 90,
            )
            for host in HOSTS
        }
        fleet = None
        fleet_path = directory / f"{name}-fleet.json"
        if stage == "mixed":
            if fleet_script is None or run_id is None:
                raise ValueError("mixed stage needs a fleet script and run ID")
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
                    str(MEASURED_SEC),
                    "--mode",
                    "mixed",
                    "--hosts",
                    *HOSTS,
                    "--host-result-template",
                    output_template,
                    "--output",
                    str(fleet_path),
                ],
                capture_output=True,
                text=True,
                timeout=90,
                check=False,
            )
        errors = {}
        for host, future in benches.items():
            try:
                (directory / f"{name}-{stage}-{host}.log").write_text(future.result())
            except Exception as exc:
                errors[host] = str(exc)
        if fleet is not None:
            fleet_process = fleet.result()
            if fleet_process.returncode:
                errors["fleet"] = fleet_process.stderr[-4000:]
    if errors:
        raise RuntimeError(f"{name}/{stage}: {errors}")
    with ThreadPoolExecutor(max_workers=len(HOSTS)) as pool:
        futures = {
            host: pool.submit(
                _ssh,
                host,
                [output_template.format(host=host)],
                'cat "$1"',
                20,
            )
            for host in HOSTS
        }
        host_documents = {
            host: json.loads(future.result()) for host, future in futures.items()
        }
    for host, document in host_documents.items():
        _write_json(Path(output_template.format(host=host)), document)
    if stage == "mixed":
        result = json.loads(fleet_path.read_text())
        if not result["valid"] or set(result["host_results"]) != set(HOSTS):
            raise RuntimeError(f"{name}: incomplete fleet result")
        if not 4.9 <= result["counter_deltas"]["app_read_write_ratio"] <= 5.1:
            raise RuntimeError(f"{name}: fleet snapshot ratio differs from 5:1")
        return result
    for host, document in host_documents.items():
        op = document["metrics"]["op_0"]
        if (
            op["operation"] != "Load"
            or op["total_success"] != op["total_keys"]
            or op["total_keys"] <= 0
            or op["window_sec"] < PRECONDITION_SEC
        ):
            raise RuntimeError(f"{name}: {host} read-only preconditioning failed")
    return None


def run_cell(
    model: str,
    in_flight: int,
    repetition: int,
    run_id: str,
    directory: Path,
    fleet_script: Path,
) -> dict[str, Any]:
    """Precondition reads, measure mixed traffic, and retain both stage results."""
    name = _cell_name(model, in_flight, repetition)
    precondition_free = preflight(RESERVE_BYTES + CELL_HEADROOM_BYTES)
    print(f"START {name}: read-only {PRECONDITION_SEC}s", flush=True)
    _run_benches(model, in_flight, name, directory, "precondition")
    before_mixed_free = preflight(RESERVE_BYTES + CELL_HEADROOM_BYTES)
    print(f"START {name}: 5:1 {MEASURED_SEC}s", flush=True)
    result = _run_benches(
        model, in_flight, name, directory, "mixed", fleet_script, run_id
    )
    if result is None:
        raise RuntimeError(f"{name}: missing mixed fleet result")
    after_mixed_free = preflight(RESERVE_BYTES)
    result["storage_free_bytes_before_precondition"] = precondition_free
    result["storage_free_bytes_before_mixed"] = before_mixed_free
    result["storage_free_bytes_after_mixed"] = after_mixed_free
    result["storage_used_bytes_during_mixed"] = {
        host: before_mixed_free[host] - after_mixed_free[host] for host in HOSTS
    }
    for host, used in result["storage_used_bytes_during_mixed"].items():
        successful = result["host_results"][host]["operations"]["Store"][
            "total_success_bytes"
        ]
        if used < 0.8 * successful:
            raise RuntimeError(
                f"{name}: {host} consumed {used} B for {successful} B"
                " of successful Stores; write prefix may not be fresh"
            )
    result["write_prefix_by_host"] = {host: f"{run_id}-{name}-{host}" for host in HOSTS}
    _write_json(directory / f"{name}-fleet.json", result)
    print(f"DONE {name}", flush=True)
    return result


def _reclaim_request(
    fleet: dict[str, Any],
    host_result: dict[str, Any],
    run_id: str,
    name: str,
    model: str,
    host: str,
) -> list[str]:
    corpus, _ = MODELS[model]
    metrics = host_result["metrics"]
    config = metrics["config"]
    adapter = json.loads(config["l2_adapter_json"])
    base = adapter["base_path"]
    if (
        adapter.get("type") != "fs_native"
        or adapter.get("use_odirect") is not True
        or not re.fullmatch(rf"/mnt/lmcache/bench-l2/{re.escape(corpus)}/[^/]+", base)
    ):
        raise ValueError(f"{host}: unexpected Store base path or adapter")
    profile_sha = metrics["geometry"]["profile_sha256"]
    if not re.fullmatch(r"[0-9a-f]{64}", profile_sha):
        raise ValueError(f"{host}: invalid profile hash")
    if profile_sha[:12] != MODEL_SHA12[model]:
        raise ValueError(f"{host}: Store profile changed")
    prefix = f"{run_id}-{name}-{host}-{profile_sha[:12]}"
    if config["write_key_prefix"] != prefix:
        raise ValueError(f"{host}: Store write prefix differs from this cell")
    store = next(
        value
        for value in metrics.values()
        if isinstance(value, dict) and value.get("operation") == "Store"
    )
    expected = fleet["host_results"][host]["operations"]["Store"]
    count = int(expected["total_success"])
    byte_count = int(expected["total_success_bytes"])
    if (
        count <= 0
        or byte_count <= 0
        or int(store["total_success"]) != count
        or int(store["total_success_bytes"]) != byte_count
        or int(store["total_keys"]) != count
    ):
        raise ValueError(f"{host}: Store totals differ from fleet result")
    return [corpus, base, prefix, str(count), str(byte_count)]


def _reclaim_cell(
    model: str,
    in_flight: int,
    repetition: int,
    run_id: str,
    directory: Path,
    result: dict[str, Any],
) -> dict[str, Any]:
    name = _cell_name(model, in_flight, repetition)
    requests = {}
    for host in HOSTS:
        path = directory / f"{name}-mixed-{host}.json"
        requests[host] = _reclaim_request(
            result, json.loads(path.read_text()), run_id, name, model, host
        )
    script = RECLAIM_SCRIPT.replace("__RECLAIM_PYTHON__", RECLAIM_PYTHON)
    audits = {}
    for mode in ("verify", "remove"):
        with ThreadPoolExecutor(max_workers=len(HOSTS)) as pool:
            futures = {
                host: pool.submit(
                    _ssh,
                    host,
                    [*request, mode],
                    script,
                    180 if mode == "verify" else 900,
                )
                for host, request in requests.items()
            }
            audits[mode] = {
                host: json.loads(future.result()) for host, future in futures.items()
            }
    time.sleep(5)
    free = preflight(RESERVE_BYTES + CELL_HEADROOM_BYTES)
    reclaimed = {}
    for host in HOSTS:
        expected = int(requests[host][4])
        returned = free[host] - result["storage_free_bytes_after_mixed"][host]
        if returned < 0.8 * expected:
            raise RuntimeError(f"{name}: {host} reclaimed {returned} B of {expected} B")
        reclaimed[host] = {
            **audits["remove"][host],
            "free_increase_bytes": returned,
        }
    result["storage_reclaim"] = reclaimed
    result["storage_free_bytes_after_reclaim"] = free
    _write_json(directory / f"{name}-fleet.json", result)
    return result


def _check_canary(canary: dict[str, Any], baseline: dict[str, Any]) -> dict[str, float]:
    """Compare short-window MiniMax IF8 directions with Goal 5A."""
    if not baseline.get("valid"):
        raise ValueError("Goal 5A baseline is invalid")
    differences = {}
    for direction in ("load", "store"):
        key = f"app_{direction}_gbps_decimal"
        old = baseline["counter_deltas"][key]
        new = canary["counter_deltas"][key]
        if old <= 0:
            raise ValueError(f"Goal 5A {direction} rate is zero")
        differences[direction] = (new / old - 1) * 100
    if any(abs(value) > 5 for value in differences.values()):
        raise RuntimeError(
            f"30s mixed window differs from Goal 5A by {differences};"
            " keep results and review duration before more writes"
        )
    return differences


def execute(
    run_id: str,
    directory: Path,
    fleet_script: Path,
    baseline_path: Path,
    in_flights: tuple[int, ...] = (1, 2, 4, 8),
    reclaim_writes: bool = False,
    canary_only: bool = False,
) -> None:
    """Run the gated canary and selected mixed load-curve cells."""
    baseline = json.loads(baseline_path.read_text())
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
        plan = [("minimax", 8, 0)]
        if not canary_only:
            plan.extend(cells(in_flights))
        for model, in_flight, repetition in plan:
            name = _cell_name(model, in_flight, repetition)
            status["state"] = "running"
            status["current"] = name
            _write_json(status_path, status)
            result = run_cell(
                model, in_flight, repetition, run_id, directory, fleet_script
            )
            if repetition == 0:
                status["canary_difference_percent"] = _check_canary(result, baseline)
            if reclaim_writes:
                result = _reclaim_cell(
                    model, in_flight, repetition, run_id, directory, result
                )
            status["completed"][name] = {
                "model": model,
                "in_flight": in_flight,
                "repetition": repetition,
                "fleet_result": str(directory / f"{name}-fleet.json"),
                "app_load_gbps_decimal": result["counter_deltas"][
                    "app_load_gbps_decimal"
                ],
                "app_store_gbps_decimal": result["counter_deltas"][
                    "app_store_gbps_decimal"
                ],
                "storage_free_bytes_after": result.get(
                    "storage_free_bytes_after_reclaim",
                    result["storage_free_bytes_after_mixed"],
                ),
            }
            _write_json(status_path, status)
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
    """Run a detached Goal 5C sweep with one owned result directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--fleet-script", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--in-flight-values", type=int, nargs="+", default=[1, 2, 4, 8])
    parser.add_argument("--reclaim-writes", action="store_true")
    parser.add_argument("--canary-only", action="store_true")
    args = parser.parse_args()
    if not re.fullmatch(r"goal5-mixed-curves-[0-9]{8}-[0-9]{6}", args.run_id):
        parser.error("invalid Goal 5 mixed-curves run ID")
    if args.root.name != args.run_id:
        parser.error("--root must end in --run-id")
    if any(value <= 0 for value in args.in_flight_values) or len(
        set(args.in_flight_values)
    ) != len(args.in_flight_values):
        parser.error("--in-flight-values must contain distinct positive values")
    execute(
        args.run_id,
        args.root,
        args.fleet_script,
        args.baseline,
        tuple(args.in_flight_values),
        args.reclaim_writes,
        args.canary_only,
    )


if __name__ == "__main__":
    main()
