# SPDX-License-Identifier: Apache-2.0
"""Ordinary io_uring/O_DIRECT controls in the tail reserved outside raw KV capacity."""

# Standard
from pathlib import Path
from typing import Any
import fcntl
import json
import os
import signal
import subprocess
import sys
import time

# Third Party
from goal5_async_fleet_probe import benchmark_limits
from goal5_fio import FIO, summarize_fio
from goal5_overnight import config_digest, validate_host, write_json
from goal5_probe import inspect
from goal5_raw_probe import gate
from goal5_uring_readiness import probe


def fio_args(cfg: dict[str, Any], host: str, block: str, mode: str) -> list[str]:
    """Build 32 disjoint 16GiB tail jobs using the ordinary io_uring FIO engine.

    Args:
        cfg: Bound two-repetition raw configuration with a reserved tail.
        host: Private array owner.
        block: 64k or 256k performance I/O size.
        mode: Seed, read or approximate 83:17 mixed control.

    Returns:
        NUMA-bound FIO arguments.

    Raises:
        ValueError: Backend, repetitions, alignment or control identity differs.
    """
    if cfg["backend"] != "raw_io_uring" or cfg["repetitions"] != 2:
        raise ValueError("Raw FIO requires ordinary io_uring and two repetitions")
    offset = cfg["raw_capacity_bytes"][host]
    if offset <= 0 or offset % 4096 or block not in ("64k", "256k"):
        raise ValueError("Raw FIO tail alignment or block size differs")
    if mode not in ("seed", "read", "mixed"):
        raise ValueError("Unknown raw FIO mode")
    return [
        "numactl",
        f"--cpunodebind={cfg['hosts'][host]['numa']}",
        f"--membind={cfg['hosts'][host]['numa']}",
        FIO,
        "--name=goal5-raw-tail",
        "--filename=/dev/md0",
        f"--offset={offset}",
        f"--offset_increment={16 * 1024**3}",
        f"--size={16 * 1024**3}",
        "--numjobs=32",
        "--iodepth=32",
        "--ioengine=io_uring",
        "--direct=1",
        "--group_reporting=1",
        "--invalidate=1",
        "--lat_percentiles=1",
        "--percentile_list=50:99",
        "--output-format=json",
        f"--bs={'1M' if mode == 'seed' else block}",
        f"--rw={dict(seed='write', read='randread', mixed='randrw')[mode]}",
        *(
            []
            if mode == "seed"
            else [
                "--time_based=1",
                "--runtime=60",
                "--ramp_time=30",
                "--allow_file_create=0",
            ]
        ),
        *(["--readonly"] if mode == "read" else []),
        *(["--rwmixread=83"] if mode == "mixed" else []),
    ]


def worker(request: dict[str, Any]) -> dict[str, Any]:
    """Check exact tail ownership, retain FIO originals and reject partial controls."""
    cfg, host = request["cfg"], request["host"]
    os.environ.update(cfg["hosts"][host]["bench_env"])
    runtime, limits = gate(cfg, host), benchmark_limits()
    snapshot = inspect(cfg)
    snapshot["io_uring_group"] = -1
    snapshot["io_uring_ready"] = probe()["uring_setup_rc"] >= 0
    validate_host(snapshot, cfg, host)
    size = int(
        subprocess.check_output(["blockdev", "--getsize64", "/dev/md0"], text=True)
    )
    if cfg["raw_capacity_bytes"][host] + 512 * 1024**3 != size:
        raise ValueError("Reserved FIO tail does not match actual private capacity")
    mode, block, repetition = (request[key] for key in ("mode", "block", "repetition"))
    if repetition not in ((0,) if mode == "seed" else (1, 2)):
        raise ValueError("FIO repetition differs from the bound matrix")
    directory = (
        Path("/home/goal5-overnight/fio-results")
        / cfg["run_id"]
        / f"{block}-{mode}-r{repetition}"
    )
    directory.mkdir(parents=True, exist_ok=False)
    argv = fio_args(cfg, host, block, mode)
    write_json(directory / "command.json", argv)
    start = time.time()
    with (
        (directory / "fio.json").open("x") as output,
        (directory / "fio.log").open("x") as log,
    ):
        process = subprocess.Popen(
            argv, stdout=output, stderr=log, start_new_session=True
        )
        try:
            code = process.wait(timeout=1800 if mode == "seed" else 240)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=30)
    end = time.time()
    if code:
        raise ValueError("Raw FIO failed; originals retained")
    result = json.loads((directory / "fio.json").read_text())
    summary = summarize_fio(result, mode)
    job = result["jobs"][0]
    if job["job options"]["ioengine"] != "io_uring":
        raise ValueError("FIO selected another engine")
    if mode != "seed" and job["iodepth_level"]["32"] < 99:
        raise ValueError("Raw FIO achieved depth fell below the control contract")
    row = {
        "host": host,
        "config_sha256": config_digest(cfg),
        "mode": mode,
        "block": block,
        "repetition": repetition,
        "command": argv,
        "exit_code": code,
        "execution_start": start,
        "execution_end": end,
        "runtime": runtime,
        "limits": limits,
        "result": result,
        "summary": summary,
        "scope": "FIO successful io_bytes over reported operation runtime. "
        "Mixed requests approximately 83:17, distinct from LMCache exact 5:1. "
        "FIO completion quantiles describe individual I/O; execution timestamps "
        "include ramp/report/drain and do not identify the exact measured phase.",
    }
    write_json(directory / "row.json", row)
    return row


def main() -> None:
    """Serialize the control under the private array's one-owner lock."""
    request = json.load(sys.stdin)
    with Path("/run/lock/lmcache-goal5-md0.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        row = worker(request)
    print(json.dumps(row))


if __name__ == "__main__":
    main()
