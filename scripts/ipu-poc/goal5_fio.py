# SPDX-License-Identifier: Apache-2.0
"""Six-host O_DIRECT XFS controls; run on mmgi0 with --config and a fresh run ID."""

# Future
from __future__ import annotations

# Standard
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
import argparse
import fcntl
import hashlib
import json
import os
import shlex
import subprocess
import sys
import time

# Third Party
from goal5_overnight import DEPLOY, HOSTS, config_digest, preflight, remote, write_json
from goal5_probe import bench

FIO = f"{DEPLOY}/bin/fio"


def fio_args(cfg: dict[str, Any], host: str, block: str, mode: str) -> list[str]:
    """Return explicit job fanout using only this host's private XFS scratch set."""
    if block not in ("64k", "256k") or mode not in ("seed", "read", "mixed"):
        raise ValueError("Unsupported FIO cell")
    operation = {"seed": "write", "read": "randread", "mixed": "randrw"}[mode]
    return (
        [
            "numactl",
            f"--cpunodebind={cfg['hosts'][host]['numa']}",
            f"--membind={cfg['hosts'][host]['numa']}",
            FIO,
            "--name=goal5",
            f"--directory=/mnt/lmcache/{host}/fio/{cfg['run_id']}",
            "--filename_format=scratch.$jobnum",
            "--size=16G",
            "--numjobs=32",
            "--iodepth=32",
            f"--ioengine={DEPLOY}/bin/fio-libaio.so",
            "--direct=1",
            "--group_reporting=1",
            "--invalidate=1",
            "--lat_percentiles=1",
            "--percentile_list=50:99",
            "--output-format=json",
            f"--bs={'1M' if mode == 'seed' else block}",
            f"--rw={operation}",
        ]
        + (
            []
            if mode == "seed"
            else [
                "--time_based=1",
                "--runtime=60",
                "--ramp_time=30",
                "--allow_file_create=0",
            ]
        )
        + (["--readonly"] if mode == "read" else [])
        + (["--rwmixread=83"] if mode == "mixed" else [])
    )


def summarize_fio(result: dict[str, Any], mode: str | None = None) -> dict[str, Any]:
    """Report successful byte rates and completion quantiles from FIO JSON."""
    jobs = result["jobs"]
    if len(jobs) != 1 or jobs[0]["error"]:
        raise ValueError("Failed FIO or unexpected reporting groups")
    if mode in ("read", "mixed") and any(
        jobs[0][name]["io_bytes"] and jobs[0][name]["runtime"] < 59_999
        for name in ("read", "write")
    ):
        raise ValueError("Incomplete FIO measured window")
    if mode == "read" and (
        not jobs[0]["read"]["io_bytes"] or jobs[0]["write"]["io_bytes"]
    ):
        raise ValueError("FIO read-only cell differs")
    if mode == "mixed":
        read_bytes, write_bytes = (
            jobs[0][name]["io_bytes"] for name in ("read", "write")
        )
        if (
            not write_bytes
            or not 0.99 * 83 / 17 <= read_bytes / write_bytes <= 1.01 * 83 / 17
        ):
            raise ValueError("FIO achieved ratio differs from requested 83:17")
    if mode == "seed" and jobs[0]["write"]["io_bytes"] != 512 * 1024**3:
        raise ValueError("Incomplete FIO scratch seed")
    row: dict[str, Any] = {}
    for name in ("read", "write"):
        op = jobs[0][name]
        count = op["io_bytes"]
        runtime = op["runtime"]
        if count and runtime <= 0:
            raise ValueError("Failed FIO runtime")
        percentiles = op.get("clat_ns", {}).get("percentile", {})
        row[name] = {
            "successful_bytes": count,
            "runtime_ms": runtime,
            "payload_gbps_decimal": count * 8 / runtime / 1e6 if count else 0,
            "p50_io_us": percentiles.get("50.000000", 0) / 1000,
            "p99_io_us": percentiles.get("99.000000", 0) / 1000,
        }
    row["payload_gbps_decimal"] = sum(
        row[name]["payload_gbps_decimal"] for name in ("read", "write")
    )
    row["read_write_ratio_achieved"] = (
        row["read"]["successful_bytes"] / row["write"]["successful_bytes"]
        if row["write"]["successful_bytes"]
        else None
    )
    return row


def worker(request: dict[str, Any]) -> dict[str, Any]:
    """Run a bounded scratch-file FIO process under the array ownership lock."""
    cfg, host = request["cfg"], request["host"]
    directory = Path(f"/mnt/lmcache/{host}/fio/{cfg['run_id']}")
    directory.mkdir(parents=True, exist_ok=True)
    if directory.is_symlink() or not str(directory.resolve()).startswith(
        f"/mnt/lmcache/{host}/fio/"
    ):
        raise ValueError("Unsafe scratch path")
    artifact = Path(DEPLOY) / "fio-results" / cfg["run_id"] / request["cell"]
    artifact.parent.mkdir(parents=True, exist_ok=True)
    output = artifact.with_suffix(".fio.json")
    if output.exists():
        raise ValueError("FIO evidence already exists")
    argv = fio_args(cfg, host, request["block"], request["mode"])
    if request["mode"] == "seed":
        if any(directory.iterdir()):
            raise ValueError("Scratch directory must be fresh before seed")
        if (
            os.statvfs(directory).f_bavail * os.statvfs(directory).f_frsize
            < 768 * 1024**3
        ):
            raise ValueError("Insufficient scratch capacity")
    else:
        if any(
            (directory / f"scratch.{i}").stat().st_size != 16 * 1024**3
            for i in range(32)
        ):
            raise ValueError("Incomplete scratch seed")
    with Path("/run/lock/lmcache-goal5-md0.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = time.time()
        row = bench(
            {
                "cfg": cfg,
                "host": host,
                "command": argv,
                "cell": {"mode": f"fio-{request['mode']}"},
                "artifact": f"fio-{request['cell']}",
                "timeout": 900,
                "config_sha256": config_digest(cfg),
            }
        )
        end = time.time()
    if row["exit_code"] or not row["result"]:
        raise ValueError(f"Failed FIO: see {row['host_artifacts']}")
    result = row["result"]
    write_json(output, result)
    return {
        "host": host,
        "command": argv,
        "started_at": start,
        "ended_at": end,
        "fio_version": subprocess.check_output([FIO, "--version"], text=True).strip(),
        "fio_sha256": hashlib.sha256(Path(FIO).read_bytes()).hexdigest(),
        "engine_sha256": hashlib.sha256(
            (Path(DEPLOY) / "bin/fio-libaio.so").read_bytes()
        ).hexdigest(),
        "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "result": result,
        "summary": summarize_fio(result, request["mode"]),
        "host_artifacts": row["host_artifacts"],
    }


def remote_worker(host: str, request: dict[str, Any]) -> dict[str, Any]:
    """Execute the identical deployed worker on one of the six initiators."""
    argv = ["python3", f"{DEPLOY}/goal5_fio.py", "--worker"]
    if host != "mmgi0":
        argv = ["ssh", "-o", "BatchMode=yes", host, shlex.join(argv)]
    proc = subprocess.run(
        argv, input=json.dumps(request), text=True, capture_output=True, timeout=960
    )
    if proc.returncode:
        raise ValueError(f"{host}: {proc.stderr}")
    return json.loads(proc.stdout)


def execute(cfg: dict[str, Any]) -> None:
    """Seed each private file set, then measure four synchronized six-host cells."""
    if cfg["backend"] != "xfs" or set(cfg["hosts"]) != set(HOSTS):
        raise ValueError("FIO requires the six private XFS arrays")
    directory = Path(DEPLOY) / "fio-results" / cfg["run_id"]
    directory.mkdir(parents=True, exist_ok=True)
    bound = directory / "config.json"
    if bound.exists() and json.loads(bound.read_text()) != cfg:
        raise ValueError("FIO configuration changed; use a fresh run ID")
    with (Path(DEPLOY) / "fleet.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        preflight(cfg)
        write_json(directory / "config.json", cfg)
        try:
            summaries = []
            for block, mode in [("256k", "seed")] + [
                (block, mode) for block in ("64k", "256k") for mode in ("read", "mixed")
            ]:
                if (directory / "STOP").exists():
                    raise ValueError("FIO run stopped")
                cell = f"{block}-{mode}"
                write_json(
                    directory / "status.json", {"state": "running", "cell": cell}
                )
                requests = {
                    host: {
                        "cfg": cfg,
                        "host": host,
                        "block": block,
                        "mode": mode,
                        "cell": cell,
                    }
                    for host in HOSTS
                }
                rows = {}
                errors = {}
                with ThreadPoolExecutor(6) as pool:
                    futures = {
                        host: pool.submit(remote_worker, host, request)
                        for host, request in requests.items()
                    }
                    for host, future in futures.items():
                        try:
                            rows[host] = future.result()
                            write_json(directory / f"{cell}-{host}.json", rows[host])
                        except Exception as error:
                            errors[host] = str(error)
                            write_json(
                                directory / f"{cell}-{host}.error.json",
                                {"host": host, "error": str(error)},
                            )
                if errors:
                    raise ValueError(f"FIO cell failed: {errors}")
                if len({row["driver_sha256"] for row in rows.values()}) != 1:
                    raise ValueError("FIO driver differs across hosts")
                if len({row["fio_sha256"] for row in rows.values()}) != 1:
                    raise ValueError("FIO binary differs across hosts")
                if len({row["engine_sha256"] for row in rows.values()}) != 1:
                    raise ValueError("FIO engine differs across hosts")
                if mode == "seed":
                    continue
                if any(
                    max(row[k] for row in rows.values())
                    - min(row[k] for row in rows.values())
                    > 5
                    for k in ("started_at", "ended_at")
                ):
                    raise ValueError("FIO cell phase skew exceeds 5 seconds")
                summary: dict[str, Any] = {
                    "cell": cell,
                    "hosts": {h: r["summary"] for h, r in rows.items()},
                    "requested_read_write_ratio": "83:17"
                    if mode == "mixed"
                    else "read-only",
                }
                summary["payload_gbps_decimal"] = sum(
                    r["summary"]["payload_gbps_decimal"] for r in rows.values()
                )
                summary["meets_1_6_tbps_payload"] = (
                    summary["payload_gbps_decimal"] >= 1600
                )
                summaries.append(summary)
                write_json(directory / "summary.json", summaries)
                print(json.dumps(summary), flush=True)
            write_json(directory / "status.json", {"state": "complete"})
        except Exception:
            write_json(directory / "status.json", {"state": "failed"})
            raise


def main() -> None:
    """Dispatch a local coordinator or a stdin-only remote worker."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(json.load(sys.stdin))))
    elif args.config:
        cfg = json.loads(args.config.read_text())
        if args.cleanup:
            (Path(DEPLOY) / "fio-results" / cfg["run_id"] / "STOP").touch()
            for host in HOSTS:
                print(host, remote(host, {"action": "cleanup", "cfg": cfg}))
        else:
            execute(cfg)
    else:
        parser.error("--config is required")


if __name__ == "__main__":
    main()
