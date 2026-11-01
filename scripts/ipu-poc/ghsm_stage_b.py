# SPDX-License-Identifier: Apache-2.0
"""lmc-ghsm Stage B: max-goodput sweep, FIO companions, 1024-token V4.1 run.

Run ``orchestrate`` on the mmgi0 coordinator after Stage A. Host-side actions
(``host-procs``, ``host-fio``) read one JSON request from stdin and print one
JSON line; they are invoked over SSH by the coordinator.
"""

# Future
from __future__ import annotations

# Standard
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any
import argparse
import fcntl
import hashlib
import json
import math
import os
import resource
import shlex
import signal
import subprocess
import sys
import threading
import time

DEPLOY = str(Path(__file__).resolve().parent)
HOSTS = ("mmgi0", "mmgi1", "mmgi2", "mmgi3", "mmgi5")
LOCK = "/run/lock/lmcache-goal5-md0.lock"
FIO = "/root/goal5-overnight/bin/fio"
FIO_ENGINE = "/root/goal5-overnight/bin/fio-libaio.so"
EXT = f"{DEPLOY}/ext"
MODELS = ("deepseek_v4_1_flash", "deepseek_v4_flash")
PROFILE_DIR = "scripts/ipu-poc/models"
TELEMETRY_UNIT = "lmc-ghsm-telemetry-b"
SEARCH_IN_FLIGHT = 64
SEARCH_WARMUP_SEC = 30
SEARCH_DURATION_SEC = 30
WINNER_REPETITIONS = 3
FIO_BLOCKS = {
    "224k": "v4_1_flash@256",
    "880k": "v4_flash@256",
    "892k": "v4_1_flash@1024",
}
FIO_SHAPES = {"32x32": 32, "32x1": 1}
FIO_MODES = ("read", "mixed")
FIO_RAMP_SEC = 30
FIO_RUNTIME_SEC = 60
FIO_SIZE_BYTES = 16 * 1024**3
CONDITIONAL_FRACTION = 0.85


def variants(uring_ok: bool) -> list[dict[str, Any]]:
    """Return the search matrix: engine x load workers x fs_uring io_depth.

    Two processes per host are not searched: with two bench processes the
    start-up time spreads across hosts by 20-70 s, so synchronized phases
    cannot be held within the controller's skew limit.
    """
    matrix = [
        {"engine": "fs_native", "workers": w, "procs": 1, "io_depth": 0}
        for w in (32, 64, 128)
    ]
    if uring_ok:
        matrix += [
            {"engine": "fs_uring", "workers": w, "procs": 1, "io_depth": d}
            for w, d in ((32, 16), (32, 32), (32, 64), (16, 32), (64, 32), (64, 64))
        ]
    for row in matrix:
        row["id"] = f"{row['engine']}-w{row['workers']}-p{row['procs']}" + (
            f"-d{row['io_depth']}" if row["io_depth"] else ""
        )
        row["in_flight"] = SEARCH_IN_FLIGHT
    return matrix


def write_json(path: Path, value: Any) -> None:
    """Atomically write a JSON artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.replace(path)


def log(message: str) -> None:
    """Print one timestamped, flushed progress line."""
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}", flush=True)


def read_cpu() -> tuple[int, int]:
    """Return (busy, total) jiffies from the aggregate /proc/stat line."""
    fields = [int(x) for x in Path("/proc/stat").read_text().split("\n")[0].split()[1:]]
    total = sum(fields[:8])
    return total - fields[3] - fields[4], total


# ---------------------------------------------------------------- host side


def host_procs(request: dict[str, Any]) -> dict[str, Any]:
    """Run concurrent benchmark processes under the array lock, sampling CPU.

    Args:
        request: run, cell, cwd, env, commands (argv lists), timeout.

    Returns:
        Per-process exit codes, parsed benchmark JSON and 1 Hz CPU samples.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = 350000 if hard == resource.RLIM_INFINITY else min(350000, hard)
    if want > soft:
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    directory = Path(DEPLOY) / "stage-b-host" / request["run"]
    directory.mkdir(parents=True, exist_ok=True)
    samples: list[list[float]] = []
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            busy, total = read_cpu()
            samples.append([time.time(), busy, total])
            stop.wait(1.0)

    children: list[subprocess.Popen] = []

    def interrupt(signum: int, _frame: Any) -> None:
        for child in children:
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupt)
    results = []
    with Path(LOCK).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sampler = threading.Thread(target=sample)
        sampler.start()
        try:
            outputs = []
            for index, argv in enumerate(request["commands"]):
                stem = directory / f"{request['cell']}-p{index}"
                output, logfile = stem.with_suffix(".json"), stem.with_suffix(".log")
                if output.exists() or logfile.exists():
                    raise ValueError(f"Host artifact exists: {stem}")
                handle = logfile.open("w")
                children.append(
                    subprocess.Popen(
                        argv + ["--output", str(output)],
                        cwd=request["cwd"],
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        env={**os.environ, **request["env"]},
                    )
                )
                handle.close()
                outputs.append((output, logfile))
            deadline = time.time() + request["timeout"]
            for child in children:
                try:
                    child.wait(timeout=max(1.0, deadline - time.time()))
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGTERM)
                    try:
                        child.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(child.pid, signal.SIGKILL)
                        child.wait()
            for child, (output, logfile) in zip(children, outputs):  # noqa: B905 - host python is 3.9
                tail = logfile.read_text(errors="replace")[-1500:]
                parsed = None
                if output.exists():
                    try:
                        parsed = json.loads(output.read_text())
                    except json.JSONDecodeError:
                        parsed = None
                results.append(
                    {
                        "exit_code": child.returncode,
                        "result": parsed,
                        "log_tail": tail,
                        "output": str(output),
                    }
                )
        except BaseException:
            for child in children:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGTERM)
            raise
        finally:
            stop.set()
            sampler.join()
    return {"processes": results, "cpu": samples, "ncpu": os.cpu_count()}


def fio_argv(request: dict[str, Any], mode: str) -> list[str]:
    """Return the FIO command; job shape matches the goal5 1538 Gb/s baseline."""
    directory = f"/mnt/lmcache/{request['host']}/fio/{request['run']}"
    numa = request["numa"]
    argv = [
        "numactl",
        f"--cpunodebind={numa}",
        f"--membind={numa}",
        FIO,
        "--name=goal5",
        f"--directory={directory}",
        "--filename_format=scratch.$jobnum",
        "--size=16G",
        "--numjobs=32",
        f"--iodepth={request['iodepth']}",
        f"--ioengine={FIO_ENGINE}",
        "--direct=1",
        "--group_reporting=1",
        "--invalidate=1",
        "--lat_percentiles=1",
        "--percentile_list=50:99",
        "--output-format=json",
        f"--bs={'1M' if mode == 'seed' else request['block']}",
        "--rw=" + {"seed": "write", "read": "randread", "mixed": "randrw"}[mode],
    ]
    if mode != "seed":
        argv += [
            "--time_based=1",
            f"--runtime={FIO_RUNTIME_SEC}",
            f"--ramp_time={FIO_RAMP_SEC}",
            "--allow_file_create=0",
        ]
    if mode == "read":
        argv.append("--readonly")
    if mode == "mixed":
        argv.append("--rwmixread=83")
    return argv


def host_fio(request: dict[str, Any]) -> dict[str, Any]:
    """Seed or measure one private scratch-file set under the array lock."""
    mode = request["mode"]
    directory = Path(f"/mnt/lmcache/{request['host']}/fio/{request['run']}")
    if directory.is_symlink():
        raise ValueError("Unsafe scratch path")
    if mode == "seed":
        directory.mkdir(parents=True, exist_ok=True)
        if any(directory.iterdir()):
            raise ValueError("Scratch directory must be fresh before seed")
        stat = os.statvfs(directory)
        if stat.f_bavail * stat.f_frsize < 768 * 1024**3:
            raise ValueError("Insufficient scratch capacity")
    elif any(
        not (directory / f"scratch.{i}").exists()
        or (directory / f"scratch.{i}").stat().st_size != FIO_SIZE_BYTES
        for i in range(32)
    ):
        raise ValueError("Incomplete scratch seed")
    artifact = (
        Path(DEPLOY) / "stage-b-host" / request["run"] / f"{request['cell']}.fio.json"
    )
    artifact.parent.mkdir(parents=True, exist_ok=True)
    if artifact.exists():
        raise ValueError("FIO evidence already exists")
    argv = fio_argv(request, mode) + ["--output", str(artifact)]
    with Path(LOCK).open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.time()
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=request["timeout"]
        )
        ended = time.time()
    if proc.returncode:
        raise ValueError(f"fio failed: {proc.stderr[-1500:]}")
    return {
        "argv": argv,
        "started_at": started,
        "ended_at": ended,
        "result": json.loads(artifact.read_text()),
        "fio_sha256": hashlib.sha256(Path(FIO).read_bytes()).hexdigest(),
        "engine_sha256": hashlib.sha256(Path(FIO_ENGINE).read_bytes()).hexdigest(),
    }


# ------------------------------------------------------------- coordinator


def host_call(
    host: str, action: str, request: dict[str, Any], timeout: float
) -> dict[str, Any]:
    """Run a host-side action locally (mmgi0) or over SSH."""
    argv = ["python3", f"{DEPLOY}/ghsm_stage_b.py", action]
    if host != "mmgi0":
        argv = ["ssh", "-o", "BatchMode=yes", host, shlex.join(argv)]
    proc = subprocess.run(
        argv, input=json.dumps(request), text=True, capture_output=True, timeout=timeout
    )
    if proc.returncode:
        raise RuntimeError(f"{host}: {proc.stderr[-2000:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def parallel(hosts: tuple[str, ...], function: Any) -> dict[str, Any]:
    """Run function(host) on every host concurrently, failing on any error."""
    with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
        futures = {host: pool.submit(function, host) for host in hosts}
        results, errors = {}, {}
        for host, future in futures.items():
            try:
                results[host] = future.result()
            except Exception as error:
                errors[host] = str(error)
    if errors:
        raise RuntimeError(json.dumps(errors))
    return results


def shell(host: str, script: str, timeout: float = 120) -> str:
    """Run a shell snippet on a host and return stdout; raise on failure."""
    argv = ["bash", "-c", script]
    if host != "mmgi0":
        argv = ["ssh", "-o", "BatchMode=yes", host, script]
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if proc.returncode:
        raise RuntimeError(f"{host}: {script}: {proc.stderr[-1000:]}")
    return proc.stdout


class StageB:
    """Coordinator state: result directory, telemetry and cell execution."""

    def __init__(self) -> None:
        """Import the controller helpers and bind to the stage-b results tree."""
        # Third Party
        import goal5_overnight as controller

        self.controller = controller
        self.out = Path(DEPLOY) / "stage-b"
        self.out.mkdir(exist_ok=True)
        self.summary_path = self.out / "summary.json"
        self.summary: dict[str, Any] = (
            json.loads(self.summary_path.read_text())
            if self.summary_path.exists()
            else {}
        )

    def status(self, state: str, **extra: Any) -> None:
        """Persist the coordinator state for external monitors."""
        write_json(
            self.out / "status.json", {"state": state, "time": time.time(), **extra}
        )

    def stopped(self) -> None:
        """Raise when the operator touched stage-b/STOP."""
        if (self.out / "STOP").exists():
            raise RuntimeError("stage-b/STOP present")

    def start_telemetry(self) -> None:
        """Start the raw target sampler (bounded; also stopped in finally)."""
        script = f"{DEPLOY}/ghsm_telemetry.sh"
        shell("mmgt", f"systemctl stop {TELEMETRY_UNIT} 2>/dev/null; true")
        shell(
            "mmgt",
            f"systemd-run --unit={TELEMETRY_UNIT} --collect "
            f"--property=RuntimeMaxSec=28800 /usr/bin/bash {script} "
            f"{DEPLOY}/stage-b-telemetry.log 14400",
        )
        log("telemetry started")

    def stop_telemetry(self) -> None:
        """Stop the target sampler, tolerating an already-stopped unit."""
        try:
            shell("mmgt", f"systemctl stop {TELEMETRY_UNIT}; true")
        except Exception as error:
            log(f"WARNING: telemetry stop failed: {error}")

    def probe_uring(self) -> bool:
        """Return True only if every host imports the fs_uring extension."""
        script = (
            f"cd {EXT} && PYTHONPATH=source/build LD_LIBRARY_PATH=deps/liburing/lib "
            "{py} -c 'import lmcache_fs_uring'"
        )
        cfg = json.loads((Path(DEPLOY) / f"{MODELS[0]}.json").read_text())
        try:
            parallel(
                HOSTS,
                lambda h: shell(h, script.format(py=cfg["hosts"][h]["python"])),
            )
        except RuntimeError as error:
            log(f"fs_uring unavailable, skipping: {error}")
            return False
        return True

    def gate(self, cfg: dict[str, Any]) -> None:
        """Run the Stage A controller preflight without touching Stage A results."""
        gated = deepcopy(cfg)
        gated["results_dir"] = str(self.out / "preflight" / cfg["run_id"])
        Path(gated["results_dir"]).mkdir(parents=True, exist_ok=True)
        self.controller.preflight(gated)

    def commands(
        self,
        cfg: dict[str, Any],
        host: str,
        variant: dict[str, Any],
        warmup: int,
        duration: int,
    ) -> list[list[str]]:
        """Build per-process read-only Load commands over the retained corpus."""
        local = deepcopy(cfg)
        local["load_workers"] = variant["workers"]
        local["warmup_sec"] = warmup
        local["duration_sec"] = duration
        result = []
        for index in range(variant["procs"]):
            local["hosts"][host]["metrics_port"] = (
                cfg["hosts"][host]["metrics_port"] + 100 * index
            )
            argv = self.controller.benchmark_args(
                local, host, variant["in_flight"], "load"
            )
            if variant["engine"] == "fs_uring":
                position = argv.index("--l2-adapter") + 1
                base = json.loads(argv[position])
                argv[position] = json.dumps(
                    {
                        "type": "native_plugin",
                        "module_path": "lmcache_fs_uring",
                        "class_name": "LMCacheFSUringClient",
                        "adapter_params": {
                            "base_path": base["base_path"],
                            "num_workers": variant["workers"],
                            "use_odirect": True,
                            "io_depth": variant["io_depth"],
                        },
                    },
                    sort_keys=True,
                )
            result.append(argv)
        return result

    def measure(
        self,
        name: str,
        cfg: dict[str, Any],
        variant: dict[str, Any],
        warmup: int,
        duration: int,
    ) -> dict[str, Any]:
        """Run one synchronized five-host cell and return its validated summary."""
        self.stopped()
        self.gate(cfg)
        directory = self.out / "cells"
        attempt = len(list(directory.glob(f"{name}-a*.json"))) + 1
        cell = f"{name}-a{attempt}"
        env_extra = (
            {
                "PYTHONPATH": f"{EXT}/source/build",
                "LD_LIBRARY_PATH": f"{EXT}/deps/liburing/lib",
            }
            if variant["engine"] == "fs_uring"
            else {}
        )
        self.status("running", cell=cell)
        log(f"CELL {cell} {json.dumps(variant)} warmup={warmup} duration={duration}")
        began = time.time()
        responses = parallel(
            HOSTS,
            lambda host: host_call(
                host,
                "host-procs",
                {
                    "run": cfg["run_id"] + "-stage-b",
                    "cell": cell,
                    "cwd": cfg["hosts"][host]["repo"],
                    "env": env_extra,
                    "commands": self.commands(cfg, host, variant, warmup, duration),
                    "timeout": warmup + duration + 300,
                },
                warmup + duration + 420,
            ),
        )
        row = self.validate(cfg, variant, responses, duration)
        row.update(cell=cell, name=name, variant=variant, wall=[began, time.time()])
        write_json(directory / f"{cell}.json", row)
        log(f"CELL {cell} aggregate {row['aggregate_gbps']:.1f} Gb/s")
        return row

    def validate(
        self,
        cfg: dict[str, Any],
        variant: dict[str, Any],
        responses: dict[str, Any],
        duration: int,
    ) -> dict[str, Any]:
        """Reject any failed, partial or misbound process; aggregate goodput."""
        geometry = cfg["geometry"]
        starts, ends, hosts = [], [], {}
        for host, response in responses.items():
            procs = response["processes"]
            if len(procs) != variant["procs"]:
                raise RuntimeError(f"{host}: process count differs")
            rates, p50, p99, windows = [], [], [], []
            for proc in procs:
                if proc["exit_code"] or not proc["result"]:
                    raise RuntimeError(f"{host}: failed: {proc['log_tail']}")
                metrics = proc["result"]["metrics"]
                loads = [
                    v
                    for k, v in metrics.items()
                    if k.startswith("op_") and v["operation"] == "Load"
                ]
                if (
                    len(loads) != 1
                    or metrics["geometry"]["profile_sha256"]
                    != geometry["profile_sha256"]
                    or metrics["config"]["in_flight"] != variant["in_flight"]
                ):
                    raise RuntimeError(f"{host}: reported configuration differs")
                op = loads[0]
                span = op["phase_end_time_seconds"] - op["phase_start_time_seconds"]
                submits = op["total_keys"] // geometry["objects_per_submit"]
                if (
                    op.get("timed_out", False)
                    or not op["total_keys"]
                    or op["total_success"] != op["total_keys"]
                    or op["total_keys"] % geometry["objects_per_submit"]
                    or op["total_success_bytes"]
                    != submits * geometry["task_size_bytes"]
                    or not math.isfinite(span)
                    or span <= 0
                    or op["window_sec"] + 0.001 < duration
                ):
                    raise RuntimeError(f"{host}: incomplete or failed Load")
                rates.append(op["total_success_bytes"] * 8 / span / 1e9)
                p50.append(op["submit_latency_p50_ms"])
                p99.append(op["submit_latency_p99_ms"])
                windows.append(
                    (op["phase_start_time_seconds"], op["phase_end_time_seconds"])
                )
            starts += [w[0] for w in windows]
            ends += [w[1] for w in windows]
            begin, end = min(w[0] for w in windows), max(w[1] for w in windows)
            inside = [s for s in response["cpu"] if begin <= s[0] <= end]
            busy = None
            if len(inside) >= 2 and inside[-1][2] > inside[0][2]:
                busy = (
                    (inside[-1][1] - inside[0][1])
                    / (inside[-1][2] - inside[0][2])
                    * response["ncpu"]
                )
            hosts[host] = {
                "gbps": sum(rates),
                "p50_submit_ms": sum(p50) / len(p50),
                "p99_submit_ms": max(p99),
                "busy_cores": busy,
                "processes": len(procs),
            }
        skew = cfg.get("max_phase_skew_sec", 5)
        if max(starts) - min(starts) > skew or max(ends) - min(ends) > skew:
            raise RuntimeError(
                "Measurement phase skew exceeds limit: starts spread "
                f"{max(starts) - min(starts):.1f}s, ends spread "
                f"{max(ends) - min(ends):.1f}s"
            )
        return {
            "aggregate_gbps": sum(h["gbps"] for h in hosts.values()),
            "hosts": hosts,
            "window": [min(starts), max(ends)],
        }

    def guarded(
        self,
        name: str,
        cfg: dict[str, Any],
        variant: dict[str, Any],
        warmup: int,
        duration: int,
    ) -> dict[str, Any] | None:
        """Run a cell; record failure and return None so the chain continues."""
        try:
            return self.measure(name, cfg, variant, warmup, duration)
        except Exception as error:
            log(f"CELL FAILED {name} {variant.get('id')}: {error}")
            write_json(
                self.out / "cells" / f"{name}-failed-{int(time.time())}.json",
                {"name": name, "variant": variant, "error": str(error)},
            )
            if "stage-b/STOP" in str(error):
                raise
            return None

    def sweep(self, model: str, uring_ok: bool) -> dict[str, Any]:
        """Search variants once, then repeat the best variant at Stage A windows."""
        cfg = json.loads((Path(DEPLOY) / f"{model}.json").read_text())
        key = f"sweep-{model}"
        if key in self.summary and "winner" in self.summary[key]:
            return self.summary[key]
        search = []
        for variant in variants(uring_ok):
            row = self.guarded(
                f"{model}-search-{variant['id']}",
                cfg,
                variant,
                SEARCH_WARMUP_SEC,
                SEARCH_DURATION_SEC,
            )
            if row:
                search.append(row)
                self.summary[key] = {"search": search}
                write_json(self.summary_path, self.summary)
        if not search:
            raise RuntimeError(f"{model}: every search cell failed")
        best = max(search, key=lambda r: r["aggregate_gbps"])
        log(f"{model} winner {best['variant']['id']} {best['aggregate_gbps']:.1f}")
        reps = []
        for rep in range(1, WINNER_REPETITIONS + 1):
            row = self.guarded(
                f"{model}-winner-{best['variant']['id']}-r{rep}",
                cfg,
                best["variant"],
                cfg["warmup_sec"],
                cfg["duration_sec"],
            )
            if row:
                reps.append(row)
        self.summary[key] = {
            "search": search,
            "winner": best["variant"],
            "winner_search_gbps": best["aggregate_gbps"],
            "winner_reps": reps,
        }
        write_json(self.summary_path, self.summary)
        return self.summary[key]

    def fio_cell(
        self,
        run: str,
        cfg: dict[str, Any],
        cell: str,
        mode: str,
        block: str,
        depth: int,
    ) -> dict[str, Any]:
        """Run one synchronized FIO cell on all five hosts and summarize it."""
        self.stopped()
        self.status("running", cell=f"fio-{cell}")
        log(f"FIO {cell}")
        timeout = 1800 if mode == "seed" else FIO_RAMP_SEC + FIO_RUNTIME_SEC + 300
        responses = parallel(
            HOSTS,
            lambda host: host_call(
                host,
                "host-fio",
                {
                    "run": run,
                    "cell": cell,
                    "host": host,
                    "numa": cfg["hosts"][host]["numa"],
                    "mode": mode,
                    "block": block,
                    "iodepth": depth,
                    "timeout": timeout,
                },
                timeout + 120,
            ),
        )
        if len({r["fio_sha256"] for r in responses.values()}) != 1:
            raise RuntimeError("FIO binary differs across hosts")
        if mode == "seed":
            return {"cell": cell}
        hosts: dict[str, dict[str, Any]] = {}
        for host, response in responses.items():
            jobs = response["result"]["jobs"]
            if len(jobs) != 1 or jobs[0]["error"]:
                raise RuntimeError(f"{host}: FIO failed")
            row: dict[str, Any] = {}
            for op_name in ("read", "write"):
                op = jobs[0][op_name]
                if op["io_bytes"] and op["runtime"] < 59_999:
                    raise RuntimeError(f"{host}: incomplete FIO window")
                percentiles = op.get("clat_ns", {}).get("percentile", {})
                row[op_name] = {
                    "bytes": op["io_bytes"],
                    "gbps": op["io_bytes"] * 8 / op["runtime"] / 1e6
                    if op["io_bytes"]
                    else 0,
                    "p50_us": percentiles.get("50.000000", 0) / 1000,
                    "p99_us": percentiles.get("99.000000", 0) / 1000,
                }
            row["gbps"] = row["read"]["gbps"] + row["write"]["gbps"]
            row["window"] = [
                response["started_at"] + FIO_RAMP_SEC,
                response["started_at"] + FIO_RAMP_SEC + FIO_RUNTIME_SEC,
            ]
            hosts[host] = row
        starts = [h["window"][0] for h in hosts.values()]
        if max(starts) - min(starts) > 5:
            raise RuntimeError("FIO phase skew exceeds 5 s")
        summary = {
            "cell": cell,
            "mode": mode,
            "block": block,
            "iodepth": depth,
            "aggregate_gbps": sum(h["gbps"] for h in hosts.values()),
            "window": [min(starts), max(h["window"][1] for h in hosts.values())],
            "hosts": hosts,
        }
        write_json(self.out / "fio" / f"{cell}.json", summary)
        log(f"FIO {cell} aggregate {summary['aggregate_gbps']:.1f} Gb/s")
        return summary

    def fio(self) -> dict[str, Any]:
        """Seed scratch files, then run read and 5:1-equivalent mixed cells."""
        cfg = json.loads((Path(DEPLOY) / f"{MODELS[0]}.json").read_text())
        run = "goal5-fio-ghsm-20261025"
        done = self.summary.setdefault("fio", {})
        if not (self.out / "fio" / "seeded").exists():
            self.fio_cell(run, cfg, "seed", "seed", "1M", 32)
            write_json(self.out / "fio" / "seeded", {"time": time.time()})
        for block in FIO_BLOCKS:
            for shape, depth in FIO_SHAPES.items():
                for mode in FIO_MODES:
                    cell = f"{block}-{shape}-{mode}"
                    if cell in done:
                        continue
                    try:
                        done[cell] = self.fio_cell(run, cfg, cell, mode, block, depth)
                    except Exception as error:
                        log(f"FIO FAILED {cell}: {error}")
                        if "stage-b/STOP" in str(error):
                            raise
                        done[cell] = {"cell": cell, "error": str(error)}
                    write_json(self.summary_path, self.summary)
        return done

    def run_1024(self) -> None:
        """Seed and measure V4.1-Flash at 1024-token chunks via the controller."""
        base = Path(DEPLOY) / f"{MODELS[0]}.json"
        path = Path(DEPLOY) / f"{MODELS[0]}_1024.json"
        stage_cfg = json.loads(base.read_text())
        if not path.exists():
            source = Path(stage_cfg["hosts"]["mmgi0"]["repo"]) / stage_cfg["profile"]
            text = source.read_text()
            if text.count("lmcache_tokens_per_chunk: 256") != 1:
                raise RuntimeError("Unexpected profile chunk-size field")
            derived = text.replace(
                "lmcache_tokens_per_chunk: 256", "lmcache_tokens_per_chunk: 1024"
            )
            profile = f"{PROFILE_DIR}/{MODELS[0]}_1024.yaml"
            for host in HOSTS:
                target = f"{stage_cfg['hosts'][host]['repo']}/{profile}"
                argv = ["bash", "-c", f"set -C; cat > {target}"]
                if host != "mmgi0":
                    argv = [
                        "ssh",
                        "-o",
                        "BatchMode=yes",
                        host,
                        f"set -C; cat > {target}",
                    ]
                subprocess.run(argv, input=derived, text=True, check=True)
            cfg = deepcopy(stage_cfg)
            cfg.update(
                run_id=stage_cfg["run_id"].replace(MODELS[0], f"{MODELS[0]}_1024"),
                results_dir=stage_cfg["results_dir"].replace(
                    f"{MODELS[0]}-a", f"{MODELS[0]}_1024-a"
                ),
                profile=profile,
            )
            snapshots = parallel(
                HOSTS,
                lambda h: self.controller.remote(h, {"action": "inspect", "cfg": cfg}),
            )
            cfg["geometry"] = snapshots["mmgi0"]["geometry"]
            for host, snapshot in snapshots.items():
                if snapshot["geometry"] != cfg["geometry"]:
                    raise RuntimeError(f"{host}: geometry differs")
                cfg["hosts"][host]["provenance"] = snapshot["provenance"]
            if cfg["geometry"]["max_object_bytes"] != 913408:
                raise RuntimeError("1024-token geometry differs from 913408 B")
            self.controller.Run(cfg)
            self.controller.preflight(cfg)
            write_json(path, cfg)
            log(f"1024 config bound {json.dumps(cfg['geometry'])}")
        for action in ("prepare", "run"):
            self.status("running", cell=f"1024-{action}")
            log(f"1024 controller {action}")
            subprocess.run(
                [
                    "python3",
                    f"{DEPLOY}/goal5_overnight.py",
                    action,
                    "--config",
                    str(path),
                ],
                check=True,
            )


def orchestrate() -> None:
    """Run the Stage B chain, tolerating per-cell failures but not gate failures."""
    stage = StageB()
    lock = (Path(DEPLOY) / "fleet.lock").open("w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stage.start_telemetry()
    try:
        uring_ok = stage.probe_uring()
        log(f"fs_uring available: {uring_ok}")
        for model in MODELS:
            stage.sweep(model, uring_ok)
        fio = stage.fio()
        best = max(
            (
                r["aggregate_gbps"]
                for r in stage.summary[f"sweep-{MODELS[0]}"]["winner_reps"]
            ),
            default=0,
        )
        ceiling = fio.get("224k-32x32-read", {}).get("aggregate_gbps", 0)
        proceed = ceiling > 0 and best < CONDITIONAL_FRACTION * ceiling
        stage.summary["conditional_1024"] = {
            "v4_1_best_gbps": best,
            "fio_224k_32x32_read_gbps": ceiling,
            "fraction_threshold": CONDITIONAL_FRACTION,
            "run": proceed,
        }
        write_json(stage.summary_path, stage.summary)
        log(f"conditional 1024: {json.dumps(stage.summary['conditional_1024'])}")
        fcntl.flock(lock, fcntl.LOCK_UN)
        if proceed:
            stage.run_1024()
        stage.status("complete")
        log("STAGE_B_COMPLETE")
    except BaseException as error:
        stage.status("failed", error=str(error))
        log(f"STAGE_B_FAILED {error!r}")
        raise
    finally:
        stage.stop_telemetry()


def plan() -> None:
    """Print the search matrix and one resolved command per profile (no traffic)."""
    stage = StageB()
    for model in MODELS:
        cfg = json.loads((Path(DEPLOY) / f"{model}.json").read_text())
        for variant in variants(True):
            commands = stage.commands(cfg, "mmgi0", variant, 30, 30)
            print(model, variant["id"], len(commands))
        print(shlex.join(stage.commands(cfg, "mmgi0", variants(True)[-3], 30, 30)[0]))
        print(shlex.join(stage.commands(cfg, "mmgi0", variants(True)[0], 30, 30)[0]))


def main() -> None:
    """Dispatch coordinator or stdin-driven host actions."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("orchestrate", "plan", "host-procs", "host-fio")
    )
    args = parser.parse_args()
    if args.action == "orchestrate":
        orchestrate()
    elif args.action == "plan":
        plan()
    elif args.action == "host-procs":
        print(json.dumps(host_procs(json.load(sys.stdin))))
    else:
        print(json.dumps(host_fio(json.load(sys.stdin))))


if __name__ == "__main__":
    main()
