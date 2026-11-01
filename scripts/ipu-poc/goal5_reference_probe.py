# SPDX-License-Identifier: Apache-2.0
"""Retain phase-aligned CPU/device evidence for the short matched XFS reference."""

# Standard
from pathlib import Path
from threading import Event, Thread
from typing import Any
import fcntl
import json
import sys

# Third Party
from goal5_async_fleet_probe import benchmark_limits
from goal5_onehost_diagnostics import sample, sample_summary, target_samples
from goal5_overnight import validate_evidence, write_json
from goal5_probe import bench, cleanup, host_directory, inspect, reclaim


def measured_bench(request: dict[str, Any]) -> dict[str, Any]:
    """Record the matched blocking filesystem process without altering its adapter."""
    cfg, host = request["cfg"], request["host"]
    limits = benchmark_limits()
    directory = host_directory(cfg)
    directory.mkdir(parents=True, exist_ok=True)
    artifact = request["artifact"]
    path = directory / f"{artifact}-samples.jsonl"
    stop = Event()
    sampler = Thread(target=sample, args=(stop, directory, [artifact], path))
    sampler.start()
    try:
        row = bench(request)
    finally:
        stop.set()
        sampler.join()
    cell = request["cell"]
    if not validate_evidence(
        row, cfg, host, cell["in_flight"], cell["repetition"], cell["mode"]
    ):
        raise ValueError("Reference benchmark failed; original evidence retained")
    row["limits"] = limits
    if cell["mode"] in ("load", "mixed", "precondition"):
        op = next(
            value
            for key, value in row["result"]["metrics"].items()
            if key.startswith("op_") and value["operation"] == "Load"
        )
        start, end = op["phase_start_time_seconds"], op["phase_end_time_seconds"]
        samples = [json.loads(line) for line in path.read_text().splitlines()]
        row["sampled"] = sample_summary(samples, start, end, artifact)
        if host == "mmgi1":
            row["target_samples"] = target_samples(start, end)
    write_json(directory / f"{artifact}-instrumented.json", row)
    return row


def main() -> None:
    """Dispatch bound reference requests under the private array lock."""
    request = json.load(sys.stdin)
    cfg = request["cfg"]
    if cfg["backend"] != "xfs" or cfg["repetitions"] != 2:
        raise ValueError("Matched reference requires XFS and two repetitions")
    result: Any
    if request["action"] == "inspect":
        result = inspect(cfg)
    elif request["action"] == "cleanup":
        result = cleanup(cfg)
    else:
        with Path("/run/lock/lmcache-goal5-md0.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if request["action"] == "bench":
                result = measured_bench(request)
            elif request["action"] == "reclaim":
                result = reclaim(request)
            else:
                raise ValueError("Unknown reference action")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
