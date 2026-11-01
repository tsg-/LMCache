# SPDX-License-Identifier: Apache-2.0
"""Run two read-only mmgi1 async trials behind bound correctness/policy gates."""

# Future
from __future__ import annotations

# Standard
from pathlib import Path
from threading import Event, Thread
from typing import Any
import argparse
import copy
import fcntl
import hashlib
import json
import os
import statistics

# Third Party
from goal5_onehost_diagnostics import (
    commands,
    sample,
    sample_interval,
    sample_summary,
    target_samples,
    validate_load,
)
from goal5_overnight import DEPLOY, config_digest, validate_host, write_json
from goal5_probe import bench, inspect


def native_summary(
    rows: list[dict[str, Any]], start: float, end: float
) -> dict[str, Any]:
    """Require phase coverage and terminal drain; preserve counter scope."""
    if (
        not rows
        or not rows[-1]["terminal"]
        or rows[-1]["stats"]["pending_reads"]
        or rows[-1]["stats"]["submitted_reads"] != rows[-1]["stats"]["completed_reads"]
    ):
        raise ValueError("Native counter evidence lacks terminal drain")
    measured = sample_interval(rows, start, end)
    terminal = rows[-1]["stats"]
    if terminal["max_worker_pending"] < 32 or terminal["max_total_pending"] > 1024:
        raise ValueError("Native issued depth outside the bound worker/depth contract")
    return {
        "samples": len(measured),
        "first_time": measured[0]["time"],
        "last_time": measured[-1]["time"],
        "mean_sampled_pending_reads": statistics.mean(
            row["stats"]["pending_reads"] for row in measured
        ),
        "max_sampled_pending_reads": max(
            row["stats"]["pending_reads"] for row in measured
        ),
        "terminal": terminal,
        "scope": "100ms samples of submitted reads whose CQEs have not been "
        "consumed. Lifetime maxima and totals include warmup and drain; these "
        "counters are not device queue depth. Recording overhead is included.",
    }


def pilot_command(cfg: dict[str, Any], identity: str) -> list[str]:
    """Keep the retained read universe while selecting the isolated async plugin."""
    argv = commands(
        cfg,
        {"kind": "lmcache", "workers": 32, "in_flight": 32, "processes": 1},
    )[0]
    position = argv.index("--l2-adapter") + 1
    original = json.loads(argv[position])
    argv[position] = json.dumps(
        {
            "type": "native_plugin",
            "module_path": "goal5_async_stats",
            "class_name": "RecordingFSUringClient",
            "adapter_params": {
                "base_path": original["base_path"],
                "num_workers": 32,
                "use_odirect": True,
                "io_depth": 32,
                "stats_path": str(
                    Path(cfg["results_dir"]) / f"{identity}-native.jsonl"
                ),
            },
        },
        sort_keys=True,
    )
    return argv


def run_host(cfg: dict[str, Any]) -> None:
    """Hold the private-array lock and retain both original measured rows."""
    if cfg["repetitions"] != 2 or cfg["warmup_sec"] != 90 or cfg["duration_sec"] != 60:
        raise ValueError("Pilot requires two repetitions with 90/60 second windows")
    for path, expected in cfg["pilot_artifacts"].items():
        if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
            raise ValueError(f"Pilot gate or source identity differs: {path}")
    policy = json.loads(Path(cfg["policy_receipt"]).read_text())
    correctness = json.loads(Path(cfg["correctness_receipt"]).read_text())
    if policy["state"] != "complete" or correctness["state"] != "complete":
        raise ValueError("Policy and correctness gates must be complete")
    if (
        Path("/proc/sys/kernel/io_uring_disabled").read_text().strip() != "1"
        or Path("/proc/sys/kernel/io_uring_group").read_text().strip() != "-1"
    ):
        raise ValueError("Restricted io_uring policy differs")
    root = Path(cfg["results_dir"])
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / "config.json", cfg)
    digest = config_digest(cfg)
    host_dir = Path(DEPLOY) / "host-results" / cfg["run_id"]
    host_dir.mkdir(parents=True, exist_ok=False)
    with Path("/run/lock/lmcache-goal5-md0.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            summaries: list[dict[str, Any]] = []
            for repetition in (1, 2):
                identity = f"async-read-if32-r{repetition}"
                readiness = inspect(cfg)
                validate_host(readiness, cfg, "mmgi1")
                write_json(root / f"{identity}-preflight.json", readiness)
                argv = pilot_command(cfg, identity)
                write_json(
                    root / "status.json",
                    {"state": "running", "cell": identity, "accepted": len(summaries)},
                )
                stop = Event()
                output = root / f"{identity}-samples.jsonl"
                sampler = Thread(
                    target=sample, args=(stop, host_dir, [identity], output)
                )
                sampler.start()
                try:
                    row = bench(
                        {
                            "cfg": cfg,
                            "host": "mmgi1",
                            "cell": {
                                "mode": "async-pilot-read",
                                "repetition": repetition,
                            },
                            "command": argv,
                            "artifact": identity,
                            "timeout": 750,
                            "config_sha256": digest,
                        }
                    )
                finally:
                    stop.set()
                    sampler.join()
                write_json(root / f"{identity}-hosts.json", [row])
                result = validate_load(row, cfg, argv)
                samples = [json.loads(line) for line in output.read_text().splitlines()]
                result["sampled"] = sample_summary(
                    samples, result["start"], result["end"], identity
                )
                native = [
                    json.loads(line)
                    for line in (root / f"{identity}-native.jsonl")
                    .read_text()
                    .splitlines()
                ]
                result["native"] = native_summary(
                    native, result["start"], result["end"]
                )
                write_json(
                    root / f"{identity}-target.json",
                    target_samples(result["start"], result["end"]),
                )
                summaries.append({"identity": identity, **result})
                write_json(root / "summary.json", summaries)
            write_json(root / "status.json", {"state": "complete", "cells": 2})
        except BaseException as error:
            write_json(root / "status.json", {"state": "failed", "error": str(error)})
            raise


def main() -> None:
    """Execute on mmgi1; the launching coordinator must hold the fleet lock."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    os.environ["PYTHONPATH"] = ":".join(cfg["pilot_pythonpath"])
    run_host(copy.deepcopy(cfg))


if __name__ == "__main__":
    main()
