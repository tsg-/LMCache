# SPDX-License-Identifier: Apache-2.0
"""Gate the isolated async client and retain per-host phase-aligned counters."""

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
import resource
import statistics
import subprocess
import sys

# Third Party
from goal5_onehost_diagnostics import (
    sample,
    sample_interval,
    sample_summary,
    target_samples,
)
from goal5_overnight import DEPLOY, validate_evidence, write_json
from goal5_probe import bench, cleanup, inspect, reclaim


def benchmark_limits() -> dict[str, tuple[int, int]]:
    """Match pilot limits in this process and its children without changing PAM."""
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if hard != resource.RLIM_INFINITY and hard < 350000:
        raise ValueError("Insufficient hard descriptor limit for async benchmark")
    resource.setrlimit(resource.RLIMIT_NOFILE, (350000, hard))
    resource.setrlimit(
        resource.RLIMIT_MEMLOCK, (resource.RLIM_INFINITY, resource.RLIM_INFINITY)
    )
    return {
        "nofile": resource.getrlimit(resource.RLIMIT_NOFILE),
        "memlock": resource.getrlimit(resource.RLIMIT_MEMLOCK),
    }


def native_summary(
    rows: list[dict[str, Any]],
    start: float | None,
    end: float | None,
    measured_keys: int,
    workers: int,
    depth: int,
) -> dict[str, Any]:
    """Require terminal drain, sufficient completions, bounded depth and coverage."""
    if (
        not rows
        or not rows[-1]["terminal"]
        or rows[-1]["stats"]["pending_reads"]
        or rows[-1]["stats"]["submitted_reads"] != rows[-1]["stats"]["completed_reads"]
    ):
        raise ValueError("Native counter evidence lacks terminal drain")
    terminal = rows[-1]["stats"]
    if terminal["completed_reads"] < measured_keys:
        raise ValueError("Native completion counts below successful Load keys")
    if (
        terminal["max_worker_pending"] > depth
        or terminal["max_total_pending"] > workers * depth
    ):
        raise ValueError("Native issued depth exceeds bound configuration")
    result = {
        "terminal": terminal,
        "scope": "100ms public counters count submitted reads whose CQEs have "
        "not been consumed, not device queue depth. Lifetime counts and peaks "
        "include warmup/readback/drain. Recording overhead is included.",
    }
    if start is not None and end is not None:
        measured = sample_interval(rows, start, end)
        result.update(
            samples=len(measured),
            first_time=measured[0]["time"],
            last_time=measured[-1]["time"],
            mean_sampled_pending_reads=statistics.mean(
                row["stats"]["pending_reads"] for row in measured
            ),
            max_sampled_pending_reads=max(
                row["stats"]["pending_reads"] for row in measured
            ),
        )
    return result


def check_gate(cfg: dict[str, Any], host: str) -> dict[str, Any]:
    """Check bound source/library/receipt hashes and the actual loaded async ABI."""
    if (
        cfg["repetitions"] != 2
        or Path("/proc/sys/kernel/io_uring_disabled").read_text().strip() != "1"
        or Path("/proc/sys/kernel/io_uring_group").read_text().strip() != "-1"
    ):
        raise ValueError("Async restricted policy or repetition contract differs")
    for path, expected in cfg["async_artifacts"][host].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Async source or gate identity differs: {path}")
    receipt = json.loads(Path(cfg["hosts"][host]["policy_receipt"]).read_text())
    if receipt["state"] != "complete":
        raise ValueError("Async policy compatibility gate incomplete")
    script = """
import hashlib, json
from pathlib import Path
import lmcache_fs_uring
p = Path(lmcache_fs_uring.__file__)
libs = {line.split()[-1] for line in Path('/proc/self/maps').read_text().splitlines()
        if 'liburing.so' in line}
print(json.dumps({'module': hashlib.sha256(p.read_bytes()).hexdigest(),
 'libraries': [hashlib.sha256(Path(x).read_bytes()).hexdigest() for x in libs]}))
"""
    result = subprocess.run(
        [cfg["hosts"][host]["python"], "-c", script],
        cwd=cfg["hosts"][host]["repo"],
        env={**os.environ, **cfg["hosts"][host]["bench_env"]},
        check=True,
        text=True,
        capture_output=True,
        timeout=60,
    )
    runtime = json.loads(result.stdout)
    if runtime["module"] != cfg["async_module_sha256"] or runtime["libraries"] != [
        cfg["async_library_sha256"]
    ]:
        raise ValueError("Actual imported module or loaded liburing differs")
    return runtime


def measured_bench(request: dict[str, Any]) -> dict[str, Any]:
    """Retain original process evidence plus measured native/CPU/device streams."""
    cfg, host = request["cfg"], request["host"]
    limits = benchmark_limits()
    gate = check_gate(cfg, host)
    directory = Path(DEPLOY) / "host-results" / cfg["run_id"]
    directory.mkdir(parents=True, exist_ok=True)
    artifact = request["artifact"]
    output = directory / f"{artifact}-samples.jsonl"
    stop = Event()
    sampler = Thread(target=sample, args=(stop, directory, [artifact], output))
    sampler.start()
    try:
        row = bench(request)
    finally:
        stop.set()
        sampler.join()
    cell = request["cell"]
    if row["exit_code"] or not row["result"]:
        raise ValueError("Async application evidence failed; original files retained")
    row["async_runtime"] = gate
    row["async_limits"] = limits
    native = [
        json.loads(line)
        for line in (directory / f"{artifact}-native.jsonl").read_text().splitlines()
    ]
    op = next(
        (
            value
            for key, value in row["result"]["metrics"].items()
            if key.startswith("op_") and value["operation"] == "Load"
        ),
        None,
    )
    start = end = None
    if cell["mode"] in ("load", "mixed", "precondition"):
        assert op is not None
        start, end = op["phase_start_time_seconds"], op["phase_end_time_seconds"]
        samples = [json.loads(line) for line in output.read_text().splitlines()]
        row["sampled"] = sample_summary(samples, start, end, artifact)
        if host == "mmgi1":
            row["target_samples"] = target_samples(start, end)
    row["native"] = native_summary(
        native,
        start,
        end,
        op["total_success"] if op is not None else 0,
        cfg["load_workers"],
        cfg["filesystem_plugin"]["io_depth"],
    )
    write_json(directory / f"{artifact}-instrumented.json", row)
    if not validate_evidence(
        row, cfg, host, cell["in_flight"], cell["repetition"], cell["mode"]
    ):
        raise ValueError("Async application evidence failed; original files retained")
    return row


def main() -> None:
    """Dispatch one bound host request, holding the private array lock for I/O."""
    request = json.load(sys.stdin)
    cfg = request["cfg"]
    result: Any
    if request["action"] == "inspect":
        nqn = Path("/etc/nvme/hostnqn").read_text().strip()
        host = next(
            name
            for name, row in cfg["manifest"]["hosts"].items()
            if row["host_nqn"] == nqn
        )
        result = inspect(cfg)
        result["async_runtime"] = check_gate(cfg, host)
    elif request["action"] == "cleanup":
        result = cleanup(cfg)
    else:
        with Path("/run/lock/lmcache-goal5-md0.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if request["action"] == "bench":
                result = measured_bench(request)
            elif request["action"] == "reclaim":
                check_gate(cfg, request["host"])
                result = reclaim(request)
            else:
                raise ValueError("Unknown async fleet action")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
