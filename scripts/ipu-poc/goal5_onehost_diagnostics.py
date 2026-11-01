# SPDX-License-Identifier: Apache-2.0
"""Read-only mmgi1 depth/producer controls using retained XFS corpora."""

# Future
from __future__ import annotations

# Standard
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from threading import Event, Thread
from typing import Any
import argparse
import copy
import fcntl
import hashlib
import json
import multiprocessing
import os
import statistics
import subprocess
import time
import urllib.parse

# Third Party
from goal5_fio import FIO, fio_args, summarize_fio
from goal5_overnight import (
    DEPLOY,
    benchmark_args,
    config_digest,
    preflight,
    validate_host,
    write_json,
)
from goal5_probe import bench, inspect


def commands(cfg: dict[str, Any], cell: dict[str, Any]) -> list[list[str]]:
    """Build read-only controls with separate ports and retained corpus keys."""
    control = copy.deepcopy(cfg)
    if cell["kind"] == "fio":
        control["run_id"] = cfg["fio_corpus_run_id"]
        return [
            [
                f"--iodepth={cell['depth']}" if arg == "--iodepth=32" else arg
                for arg in fio_args(control, "mmgi1", "64k", "read")
            ]
        ]
    control["run_id"] = cfg["corpus_run_id"]
    control["load_workers"] = cell["workers"]
    result = []
    for index in range(cell["processes"]):
        control["hosts"]["mmgi1"]["metrics_port"] = 9201 + index
        result.append(benchmark_args(control, "mmgi1", cell["in_flight"], "load"))
    return result


def validate_load(
    row: dict[str, Any], cfg: dict[str, Any], argv: list[str]
) -> dict[str, Any]:
    """Reject unsuccessful, misbound, partial, or truncated Load measurements."""
    if row["exit_code"] or row["command"] != argv or not row["result"]:
        raise ValueError("Load evidence process or command differs")
    metrics = row["result"]["metrics"]
    reported = metrics["config"]
    geometry = metrics["geometry"]
    operations = [v for k, v in metrics.items() if k.startswith("op_")]
    if (
        len(operations) != 1
        or json.loads(reported["l2_adapter_json"])
        != json.loads(argv[argv.index("--l2-adapter") + 1])
        or reported["in_flight"] != int(argv[argv.index("--in-flight") + 1])
        or reported["warmup_sec"] != cfg["warmup_sec"]
        or reported["duration_sec"] != cfg["duration_sec"]
        or any(
            geometry[k] != cfg["geometry"][k]
            for k in ("profile_sha256", "objects_per_submit", "task_size_bytes")
        )
    ):
        raise ValueError("Load evidence configuration differs")
    op = operations[0]
    span = op["phase_end_time_seconds"] - op["phase_start_time_seconds"]
    submits = op["submits"]
    if (
        op["operation"] != "Load"
        or op.get("timed_out", False)
        or submits <= 0
        or op["total_keys"] != submits * geometry["objects_per_submit"]
        or op["total_success"] != op["total_keys"]
        or op["total_success_bytes"] != submits * geometry["task_size_bytes"]
        or op["submit_latency_count"] != submits
        or span < cfg["duration_sec"] - 0.001
        or abs(op["window_sec"] - span) > 0.0011
    ):
        raise ValueError("Load evidence counts, bytes, or window differ")
    return {
        "successful_bytes": op["total_success_bytes"],
        "payload_gbps_decimal": op["total_success_bytes"] * 8 / span / 1e9,
        "start": op["phase_start_time_seconds"],
        "end": op["phase_end_time_seconds"],
        "operation": op,
    }


def sample_interval(
    samples: list[dict[str, Any]], start: float, end: float
) -> list[dict[str, Any]]:
    """Select actual samples inside a phase; require endpoint and gap coverage."""
    selected = [s for s in samples if start <= s["time"] <= end]
    if (
        len(selected) < 2
        or selected[0]["time"] - start > 0.5
        or end - selected[-1]["time"] > 0.5
        or any(
            selected[i + 1]["time"] - selected[i]["time"] > 0.5
            for i in range(len(selected) - 1)
        )
    ):
        raise ValueError("Inadequate measured-phase sample coverage")
    return selected


def sample(stop: Event, directory: Path, artifacts: list[str], output: Path) -> None:
    """Retain 100ms process/main-thread CPU and md/member block counters."""
    devices = ["md0"] + sorted(
        (p / "block").resolve().name
        for p in Path("/sys/class/block/md0/md").glob("dev-*")
    )
    retained_ticks: dict[str, dict[str, int]] = {name: {} for name in artifacts}
    with output.open("x") as stream:
        while not stop.is_set():
            row: dict[str, Any] = {"time": time.time(), "processes": {}, "devices": {}}
            for name in artifacts:
                pid_path = directory / f"{name}.pid.json"
                if not pid_path.exists():
                    continue
                try:
                    pid = json.loads(pid_path.read_text())["pid"]
                    proc = Path(f"/proc/{pid}")
                    total = (proc / "stat").read_text().rsplit(")", 1)[1].split()
                    main = (
                        (proc / "task" / str(pid) / "stat")
                        .read_text()
                        .rsplit(")", 1)[1]
                        .split()
                    )
                    retained_ticks[name][str(pid)] = int(total[11]) + int(total[12])
                    children = (proc / "task" / str(pid) / "children").read_text()
                    for child in children.split():
                        child_stat = (
                            Path(f"/proc/{child}/stat")
                            .read_text()
                            .rsplit(")", 1)[1]
                            .split()
                        )
                        retained_ticks[name][child] = int(child_stat[11]) + int(
                            child_stat[12]
                        )
                    row["processes"][name] = {
                        "pid": pid,
                        "total_ticks": sum(retained_ticks[name].values()),
                        "producer_ticks": int(main[11]) + int(main[12]),
                    }
                except (FileNotFoundError, ProcessLookupError, json.JSONDecodeError):
                    pass
            for device in devices:
                row["devices"][device] = [
                    int(x)
                    for x in Path(f"/sys/class/block/{device}/stat").read_text().split()
                ]
            stream.write(json.dumps(row) + "\n")
            stream.flush()
            stop.wait(0.1)


def sample_summary(
    samples: list[dict[str, Any]], start: float, end: float, artifact: str
) -> dict[str, Any]:
    """Report sampled CPU and block-layer depth using each actual sample span."""
    rows = sample_interval(
        [r for r in samples if artifact in r["processes"]], start, end
    )
    first, last = rows[0], rows[-1]
    span = last["time"] - first["time"]
    clock_ticks = os.sysconf("SC_CLK_TCK")
    cpu = {
        k: (last["processes"][artifact][k] - first["processes"][artifact][k])
        / clock_ticks
        / span
        for k in ("producer_ticks", "total_ticks")
    }
    devices = {}
    for name in first["devices"]:
        a, b = first["devices"][name], last["devices"][name]
        if any(b[i] < a[i] for i in (0, 2, 4, 6, 10)):
            raise ValueError("Device counter reset")
        devices[name] = {
            "read_payload_gbps": (b[2] - a[2]) * 512 * 8 / span / 1e9,
            "write_bytes": (b[6] - a[6]) * 512,
            "mean_sampled_io_in_progress": statistics.mean(
                r["devices"][name][8] for r in rows
            ),
            "weighted_io_time_average_depth": (b[10] - a[10]) / 1000 / span,
        }
    return {
        "span_sec": span,
        "samples": len(rows),
        "first_time": first["time"],
        "last_time": last["time"],
        "producer_cpu_cores": cpu["producer_ticks"],
        "total_process_cpu_cores": cpu["total_ticks"],
        "devices": devices,
        "scope": "CPU delta in /proc including direct child jobs; producer is main "
        "thread of the benchmark parent. Depth is sampled "
        "block-layer outstanding requests, not adapter submit IF or configured QPs.",
    }


def target_samples(start: float, end: float) -> dict[str, Any]:
    """Retain historical ACC counter/read-timestamp samples for this phase."""
    query = (
        '{__name__=~"acc_telemetry_bytes_total|acc_telemetry_read_timestamp_seconds",'
        'job="acc_grpc_6x2",host="mmgt"}'
        f"[{int(end - start) + 2}s] @ {end:.9f}"
    )
    url = "http://127.0.0.1:9090/api/v1/query?" + urllib.parse.urlencode(
        {"query": query}
    )
    result = subprocess.run(
        ["docker", "exec", "lmcache-prom-6x2", "wget", "-qO-", url],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    data = json.loads(result.stdout)
    if data["status"] != "success" or not data["data"]["result"]:
        raise ValueError("Target telemetry absent")
    return {"query": query, "start": start, "end": end, "raw": data}


def run_host(cfg: dict[str, Any]) -> None:
    """Run the bound two-repetition matrix while holding the private array lock."""
    root = Path(cfg["results_dir"])
    if root.exists():
        raise ValueError("Diagnostic results must be fresh")
    root.mkdir(parents=True)
    digest = config_digest(cfg)
    write_json(root / "config.json", cfg)
    with Path("/run/lock/lmcache-goal5-md0.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            readiness = inspect(cfg)
            write_json(root / "preflight.json", readiness)
            validate_host(readiness, cfg, "mmgi1")
            fio_dir = Path(f"/mnt/lmcache/mmgi1/fio/{cfg['fio_corpus_run_id']}")
            if fio_dir.is_symlink() or any(
                (fio_dir / f"scratch.{i}").stat().st_size != 16 * 1024**3
                for i in range(32)
            ):
                raise ValueError("Retained FIO corpus differs")
            host_dir = Path(DEPLOY) / "host-results" / cfg["run_id"]
            host_dir.mkdir(parents=True, exist_ok=True)
            summaries = []
            for cell in cfg["diagnostic_cells"]:
                identity = cell["identity"]
                readiness = inspect(cfg)
                validate_host(readiness, cfg, "mmgi1")
                write_json(root / f"{identity}-preflight.json", readiness)
                argv_list = commands(cfg, cell)
                artifacts = [f"{identity}-p{i}" for i in range(len(argv_list))]
                requests = [
                    {
                        "cfg": cfg,
                        "host": "mmgi1",
                        "cell": {"mode": "diagnostic-read", **cell},
                        "command": argv,
                        "artifact": artifacts[i],
                        "timeout": 750,
                        "config_sha256": digest,
                    }
                    for i, argv in enumerate(argv_list)
                ]
                write_json(root / "status.json", {"state": "running", "cell": cell})
                stop = Event()
                output = root / f"{identity}-samples.jsonl"
                sampler = Thread(
                    target=sample, args=(stop, host_dir, artifacts, output)
                )
                sampler.start()
                try:
                    with ProcessPoolExecutor(
                        max_workers=len(requests),
                        mp_context=multiprocessing.get_context("spawn"),
                    ) as pool:
                        rows = list(pool.map(bench, requests))
                finally:
                    stop.set()
                    sampler.join()
                write_json(root / f"{identity}-hosts.json", rows)
                samples = [json.loads(line) for line in output.read_text().splitlines()]
                results = []
                for i, row in enumerate(rows):
                    argv, artifact = argv_list[i], artifacts[i]
                    if row["exit_code"] or row["command"] != argv:
                        raise ValueError("Diagnostic process failed")
                    if cell["kind"] == "fio":
                        result = summarize_fio(row["result"], "read")
                        end = row["result"]["timestamp_ms"] / 1000
                        start = end - result["read"]["runtime_ms"] / 1000
                        result.update(
                            start=start,
                            end=end,
                            fio_job=row["result"]["jobs"][0],
                            phase_scope="FIO phase estimated from report timestamp "
                            "minus runtime; reporting/drain offset is unmeasured.",
                        )
                    else:
                        result = validate_load(row, cfg, argv)
                    result["sampled"] = sample_summary(
                        samples, result["start"], result["end"], artifact
                    )
                    results.append(result)
                start = max(r["start"] for r in results)
                end = min(r["end"] for r in results)
                if (
                    len(results) > 1
                    and max(r["start"] for r in results)
                    - min(r["start"] for r in results)
                    > 5
                ):
                    raise ValueError("Multi-process phase skew exceeds five seconds")
                write_json(root / f"{identity}-target.json", target_samples(start, end))
                summaries.append(
                    {
                        "cell": cell,
                        "payload_gbps_decimal": sum(
                            r["payload_gbps_decimal"] for r in results
                        ),
                        "processes": results,
                        "latency_scope": "Separate per-process submit quantiles; "
                        "four IF8 producers do not share the single IF32 "
                        "latency contract.",
                    }
                )
                write_json(root / "summary.json", summaries)
            write_json(
                root / "status.json", {"state": "complete", "cells": len(summaries)}
            )
        except BaseException as error:
            write_json(root / "status.json", {"state": "failed", "error": str(error)})
            raise


def main() -> None:
    """Coordinate on mmgi0, or execute the locked matrix on mmgi1."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", action="store_true")
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != cfg["driver_sha256"]:
        raise ValueError("Diagnostic driver identity differs")
    if cfg["repetitions"] != 2 or len(cfg["diagnostic_cells"]) != 12:
        raise ValueError("Diagnostic matrix must contain exactly two repetitions")
    if args.host:
        if any(
            hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected
            for path, expected in cfg["fio_artifacts"].items()
        ):
            raise ValueError("FIO control identity differs")
        if FIO not in cfg["fio_artifacts"]:
            raise ValueError("FIO executable is unbound")
        run_host(cfg)
        return
    with (Path(DEPLOY) / "fleet.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        preflight(cfg)
        subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "mmgi1",
                "python3",
                f"{DEPLOY}/goal5_onehost_diagnostics.py",
                "--host",
                "--config",
                str(args.config),
            ],
            check=True,
            timeout=6000,
        )


if __name__ == "__main__":
    main()
