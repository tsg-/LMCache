# SPDX-License-Identifier: Apache-2.0
"""Bound ordinary raw io_uring launch, checkpoint reclamation and instrumentation."""

# Future
from __future__ import annotations

# Standard
from pathlib import Path
from threading import Event, Thread
from typing import Any
import fcntl
import hashlib
import json
import os
import statistics
import subprocess
import sys

# Third Party
from goal5_async_fleet_probe import benchmark_limits
from goal5_onehost_diagnostics import (
    sample,
    sample_interval,
    sample_summary,
    target_samples,
)
from goal5_overnight import DEPLOY, validate_evidence, write_json
from goal5_probe import bench, cleanup, inspect
from goal5_uring_readiness import probe


def gate(cfg: dict[str, Any], host: str) -> dict[str, Any]:
    """Require bound raw sources, imported module ABI and the restricted policy."""
    if (
        cfg["backend"] != "raw_io_uring"
        or cfg["repetitions"] != 2
        or Path("/proc/sys/kernel/io_uring_disabled").read_text().strip() != "1"
        or Path("/proc/sys/kernel/io_uring_group").read_text().strip() != "-1"
    ):
        raise ValueError("Raw backend, repetitions or restricted policy differs")
    for path, expected in cfg["raw_artifacts"][host].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Raw artifact differs: {path}")
    script = """
import hashlib,importlib,json
from pathlib import Path
import lmcache_rust_raw_block_io as api
native=importlib.import_module(
 'lmcache_rust_raw_block_io.lmcache_rust_raw_block_io')
assert api.RawBlockDevice is native.RawBlockDevice
assert hasattr(native.RawBlockDevice, 'io_stats')
print(json.dumps({'path':native.__file__,
 'sha256':hashlib.sha256(Path(native.__file__).read_bytes()).hexdigest()}))
"""
    result = subprocess.run(
        [cfg["hosts"][host]["python"], "-c", script],
        cwd=cfg["hosts"][host]["repo"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    runtime = json.loads(result.stdout)
    if runtime["sha256"] != cfg["raw_module_sha256"]:
        raise ValueError("Imported raw module differs")
    return runtime


def index_request(request: dict[str, Any]) -> dict[str, Any]:
    """Use the benchmark interpreter for public raw index/checkpoint operations."""
    cfg, host = request["cfg"], request["host"]
    result = subprocess.run(
        [cfg["hosts"][host]["python"], f"{DEPLOY}/{cfg['index_helper']}"],
        cwd=cfg["hosts"][host]["repo"],
        input=json.dumps(request),
        check=True,
        capture_output=True,
        text=True,
        timeout=900,
    )
    return json.loads(result.stdout)


def guard(request: dict[str, Any]) -> dict[str, Any]:
    """Reject a previously written prefix using a separately recovered raw index."""
    return index_request({**request, "action": "guard"})


def native_summary(
    rows: list[dict[str, Any]], start: float | None, end: float | None
) -> dict[str, Any]:
    """Require checkpoint persistence and native drain; retain measured issued depth."""
    if not rows or not rows[-1]["terminal"]:
        raise ValueError("Raw native stream lacks terminal status")
    final = rows[-1]["status"]["core"]
    native = final["native_io"]
    if (
        native is None
        or native["issued_pending"]
        or native["outstanding_requests"]
        or final["inflight_io_count"]
        or final["locked_key_count"]
        or final["metadata_persisted"] != final["metadata_dirty_total"]
    ):
        raise ValueError("Raw native drain or checkpoint persistence failed")
    result = {
        "terminal": rows[-1]["status"],
        "scope": "100ms public status counters; issued_pending tracks submitted "
        "requests until CQE consumption, not device queue depth. Outstanding also "
        "includes producer-queued requests. Fields are sampled individually; "
        "lifetime peak includes recovery, warmup, readback and checkpoint I/O.",
    }
    if start is not None and end is not None:
        measured = sample_interval(rows, start, end)
        pending = [
            row["status"]["core"]["native_io"]["issued_pending"] for row in measured
        ]
        result.update(
            samples=len(measured),
            first_time=measured[0]["time"],
            last_time=measured[-1]["time"],
            mean_sampled_issued_pending=statistics.mean(pending),
            max_sampled_issued_pending=max(pending),
        )
    return result


def measured_bench(request: dict[str, Any]) -> dict[str, Any]:
    """Retain native, CPU, block, target and successful application evidence."""
    cfg, host = request["cfg"], request["host"]
    limits, runtime = benchmark_limits(), gate(cfg, host)
    if request["cell"]["mode"] in ("canary", "store"):
        guard(request)
    directory = Path(DEPLOY) / "host-results" / cfg["run_id"]
    directory.mkdir(parents=True, exist_ok=True)
    artifact = request["artifact"]
    output = directory / f"{artifact}-samples.jsonl"
    stop = Event()
    sampler = Thread(target=sample, args=(stop, directory, [artifact], output))
    sampler.start()
    try:
        row = bench(request, raw_guard=guard)
    finally:
        stop.set()
        sampler.join()
    cell = request["cell"]
    write_json(directory / f"{artifact}-instrumented.json", row)
    if not validate_evidence(
        row, cfg, host, cell["in_flight"], cell["repetition"], cell["mode"]
    ):
        raise ValueError("Raw benchmark failed; original evidence retained")
    row.update(raw_runtime=runtime, raw_limits=limits)
    start = end = None
    if cell["mode"] in ("load", "mixed", "precondition"):
        load = next(
            op
            for key, op in row["result"]["metrics"].items()
            if key.startswith("op_") and op["operation"] == "Load"
        )
        start, end = load["phase_start_time_seconds"], load["phase_end_time_seconds"]
        samples = [json.loads(line) for line in output.read_text().splitlines()]
        row["sampled"] = sample_summary(samples, start, end, artifact)
        if host == "mmgi1":
            row["target_samples"] = target_samples(start, end)
    native = [
        json.loads(line)
        for line in (directory / f"{artifact}-native.jsonl").read_text().splitlines()
    ]
    row["native"] = native_summary(native, start, end)
    write_json(directory / f"{artifact}-instrumented.json", row)
    return row


def main() -> None:
    """Dispatch a bound request; serialize raw index ownership under the array lock."""
    request = json.load(sys.stdin)
    cfg = request["cfg"]
    nqn = Path("/etc/nvme/hostnqn").read_text().strip()
    host = next(
        name for name, row in cfg["manifest"]["hosts"].items() if row["host_nqn"] == nqn
    )
    os.environ.update(cfg["hosts"][host]["bench_env"])
    result: Any
    if request["action"] == "inspect":
        result = inspect(cfg)
        result["raw_runtime"] = gate(cfg, host)
        readiness = probe()
        result["io_uring_group"] = int(readiness["io_uring_group"])
        result["io_uring_ready"] = readiness["uring_setup_rc"] >= 0
    elif request["action"] == "cleanup":
        result = cleanup(cfg)
    else:
        with Path("/run/lock/lmcache-goal5-md0.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if request["action"] == "bench":
                result = measured_bench(request)
            elif request["action"] in ("reclaim", "restart-verify"):
                gate(cfg, host)
                result = index_request(request)
            else:
                raise ValueError("Unknown raw action")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
