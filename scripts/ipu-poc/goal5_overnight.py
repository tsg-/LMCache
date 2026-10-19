# SPDX-License-Identifier: Apache-2.0
"""Goal5: six private eight-namespace arrays, fixed QPs, and matched load curves.

Run on mmgi0. Use init-config, preflight, prepare, then run, each with --config.
prepare byte-checks a canary, seeds a corpus and checks load success in a fresh
process; run never formats, reconnects, or changes QPs. cleanup stops only
processes owned by this run.
Raw preparation requires --ack-raw-overwrite and an independently unmounted array.
SPDK target provisioning/comparison remains a separate follow-up.
"""

# Future
from __future__ import annotations

# Standard
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo
import argparse
import fcntl
import hashlib
import json
import math
import re
import shlex
import subprocess

# Third Party
from setup_initiator_namespace_6x8 import private_members
from setup_mmgt_namespace_6x8 import validate_manifest

HOSTS = tuple(f"mmgi{i}" for i in range(6))
SETUP = "/root/lmcache-namespace-6x8-20261018"
DEPLOY = "/root/goal5-overnight"
PROFILES = {
    "mixtral64k": "mixtral_8x22b_fp8_64k.yaml",
    "mixtral256k": "mixtral_8x22b_fp8.yaml",
    "minimax": "minimax_m3_bf16_tp8_odirect.yaml",
}


def config_digest(cfg: dict[str, Any]) -> str:
    """Return a stable digest binding commands, runtime expectations and results."""
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    """Atomically write an artifact; callers decide whether replacement is allowed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2) + "\n")
    temp.replace(path)


def adapter(cfg: dict[str, Any], host: str) -> dict[str, Any]:
    """Return O_DIRECT filesystem or ordinary raw-block I/O configuration."""
    if cfg["backend"] == "xfs":
        return {
            "type": "fs_native",
            "base_path": f"/mnt/lmcache/{host}/goal5/{cfg['run_id']}",
            "num_workers": cfg["load_workers"],
            "use_odirect": True,
        }
    if cfg["backend"] not in ("raw_posix", "raw_io_uring"):
        raise ValueError("Unknown backend")
    return {
        "type": "raw_block",
        "device_path": "/dev/md0",
        "slot_bytes": ((cfg["geometry"]["max_object_bytes"] + 8191) // 4096) * 4096,
        "header_bytes": 4096,
        "meta_total_bytes": 268435456,
        "use_odirect": True,
        "use_uring_cmd": False,
        "io_engine": "posix" if cfg["backend"] == "raw_posix" else "io_uring",
        "iouring_queue_depth": cfg["iouring_queue_depth"],
        "num_load_workers": cfg["load_workers"],
        "num_store_workers": 4,
        "num_lookup_workers": 1,
        "load_checkpoint_on_init": True,
    }


def cells(cfg: dict[str, Any]) -> list[tuple[int, int]]:
    """Return the load curve, rejecting IF values that change the seeded universe."""
    values = cfg["in_flights"]
    if (
        not values
        or len(set(values)) != len(values)
        or cfg["dataset_submits"] <= 0
        or cfg["repetitions"] <= 0
        or any(
            type(value) is not int or value <= 0 or cfg["dataset_submits"] % value
            for value in values
        )
    ):
        raise ValueError("Every in-flight must divide dataset_submits")
    return [
        (value, repetition)
        for repetition in range(1, cfg["repetitions"] + 1)
        for value in values
    ]


def benchmark_args(
    cfg: dict[str, Any], host: str, in_flight: int, mode: str
) -> list[str]:
    """Build a NUMA-bound command with a fixed, profile-specific key universe."""
    if mode not in ("load", "store", "verify", "canary"):
        raise ValueError("Unknown benchmark mode")
    if in_flight <= 0 or cfg["dataset_submits"] % in_flight:
        raise ValueError("In-flight must divide dataset_submits")
    row = cfg["hosts"][host]
    args = [
        "numactl",
        f"--cpunodebind={row['numa']}",
        f"--membind={row['numa']}",
        row["python"],
        "-m",
        "lmcache.cli.main",
        "bench",
        "l2",
        "--l2-adapter",
        json.dumps(adapter(cfg, host), sort_keys=True),
        "--kvcache-shape-profile",
        cfg["profile"],
        "--key-prefix",
        f"{cfg['run_id']}-{cfg['geometry']['profile_sha256'][:12]}"
        + ("-canary" if mode == "canary" else ""),
        "--in-flight",
        str(in_flight),
        "--rounds",
        "1" if mode == "canary" else str(cfg["dataset_submits"] // in_flight),
        "--warmup-rounds",
        "0",
        "--l1-align-bytes",
        "4096",
        "--format",
        "json",
    ]
    if mode != "canary":
        args += ["--only", "store" if mode == "store" else "load"]
    if mode == "load":
        args += [
            "--duration-sec",
            str(cfg["duration_sec"]),
            "--warmup-sec",
            str(cfg["warmup_sec"]),
            "--serve-metrics",
            str(row["metrics_port"]),
            "--metrics-bind-address",
            "127.0.0.1",
        ]
    if mode == "canary":
        args += ["--no-skip-verify"]
    return args


def validate_evidence(
    row: dict[str, Any],
    cfg: dict[str, Any],
    host: str,
    in_flight: int,
    repetition: int,
    mode: str,
) -> bool:
    """Reject mismatched evidence before inspecting success; never alter artifacts."""
    identity = {"in_flight": in_flight, "repetition": repetition, "mode": mode}
    if (
        row.get("config_sha256") != config_digest(cfg)
        or row.get("host") != host
        or row.get("cell") != identity
        or row.get("command") != benchmark_args(cfg, host, in_flight, mode)
    ):
        raise ValueError(
            "Evidence configuration differs; use a fresh results directory"
        )
    if row["exit_code"] != 0 or not row.get("result"):
        return False
    metrics = row["result"]["metrics"]
    reported = metrics["config"]
    if (
        reported["in_flight"] != in_flight
        or json.loads(reported["l2_adapter_json"]) != adapter(cfg, host)
        or metrics["geometry"]["profile_sha256"] != cfg["geometry"]["profile_sha256"]
        or reported["mode"] != ("sustained" if mode == "load" else "rounds")
        or (
            mode == "load"
            and (
                reported["duration_sec"] != cfg["duration_sec"]
                or reported["warmup_sec"] != cfg["warmup_sec"]
            )
        )
    ):
        raise ValueError("Reported benchmark configuration differs")
    operation = "Store" if mode == "store" else "Load"
    matches = [
        value
        for key, value in metrics.items()
        if key.startswith("op_") and value["operation"] == operation
    ]
    if len(matches) != 1:
        raise ValueError("Reported operation configuration differs")
    op = matches[0]
    if op["operation"] != ("Store" if mode == "store" else "Load"):
        raise ValueError("Reported operation configuration differs")
    if (
        op.get("timed_out", False)
        or not op["total_keys"]
        or op["total_success"] != op["total_keys"]
    ):
        return False
    if mode == "load":
        span = op["phase_end_time_seconds"] - op["phase_start_time_seconds"]
        payload = (
            op["total_keys"]
            // cfg["geometry"]["objects_per_submit"]
            * cfg["geometry"]["task_size_bytes"]
        )
        window = op["window_sec"]
        if (
            not math.isfinite(span)
            or span <= 0
            or not math.isfinite(window)
            # The CLI rounds this duration to milliseconds.
            or window + 0.001 < cfg["duration_sec"]
            or op["total_success_bytes"] != payload
        ):
            return False
        return all(
            math.isfinite(op[key]) and op[key] >= 0
            for key in ("submit_latency_p50_ms", "submit_latency_p99_ms")
        )
    return op["total_keys"] == (
        (1 if mode == "canary" else cfg["dataset_submits"])
        * cfg["geometry"]["objects_per_submit"]
    )


def summarize(
    rows: dict[str, Any], cfg: dict[str, Any], in_flight: int, repetition: int
) -> dict[str, Any]:
    """Sum per-host successful-payload rates; retain per-host submit quantiles."""
    if set(rows) != set(cfg["hosts"]):
        raise ValueError("Incomplete six-host evidence")
    for host, row in rows.items():
        if not validate_evidence(row, cfg, host, in_flight, repetition, "load"):
            raise ValueError(f"Failed I/O on {host}")
    ops = {host: row["result"]["metrics"]["op_0"] for host, row in rows.items()}
    starts = [op["phase_start_time_seconds"] for op in ops.values()]
    ends = [op["phase_end_time_seconds"] for op in ops.values()]
    if (
        max(starts) - min(starts) > cfg["max_phase_skew_sec"]
        or max(ends) - min(ends) > cfg["max_phase_skew_sec"]
    ):
        raise ValueError("Measurement phase skew exceeds configured limit")
    rates = {
        host: op["total_success_bytes"]
        * 8
        / (op["phase_end_time_seconds"] - op["phase_start_time_seconds"])
        / 1e9
        for host, op in ops.items()
    }
    return {
        "in_flight": in_flight,
        "repetition": repetition,
        "payload_gbps_decimal": sum(rates.values()),
        "latency_unit": "submit-to-observed completion; not individual object or TTFT",
        "hosts": {
            host: {
                "payload_gbps_decimal": rates[host],
                "p50_submit_ms": op["submit_latency_p50_ms"],
                "p99_submit_ms": op["submit_latency_p99_ms"],
                "submit_latency_count": op["submit_latency_count"],
                "submit_latency_sample_count": op.get(
                    "submit_latency_sample_count", op["submit_latency_count"]
                ),
                "latency_quantile_scope": (
                    "latest completions"
                    if op.get("submit_latency_sample_count", op["submit_latency_count"])
                    < op["submit_latency_count"]
                    else "all completions"
                ),
            }
            for host, op in ops.items()
        },
    }


def validate_host(row: dict[str, Any], cfg: dict[str, Any], host: str) -> None:
    """Raise ValueError unless private membership, QPs, code and backend are ready."""
    expected = cfg["hosts"][host]
    minimum_free = cfg.get("min_results_free_bytes", 2 * 1024**3)
    if row["results_free_bytes"] < minimum_free:
        raise ValueError(
            f"{host}: OS results filesystem has "
            f"{row['results_free_bytes'] / 1024**3:.2f} GiB free; "
            f"Goal5 requires {minimum_free / 1024**3:.2f} GiB"
        )
    checks = [
        len(row["namespace_uuids"]) == 8,
        sorted(row["namespace_uuids"]) == sorted(expected["namespace_uuids"]),
        sorted(row["md_members"]) == sorted(expected["namespace_uuids"]),
        len(row["controllers"]) == 2,
        all(
            c["state"] == "live" and c["queue_count"] == 129 and c["sqsize"] == 127
            for c in row["controllers"]
        ),
        row["raid_disks"] == 8,
        row["chunk_bytes"] == 524288,
        row["md_level"] == "raid0",
        row["md_state"] in ("clean", "active"),
        row["governors"] == ["performance"],
        row["numa"] == expected["numa"],
        row["min_perf_pct"] == 100,
        row["epb"] == ["0"],
        row["ntp_synchronized"],
        row["provenance"] == expected["provenance"],
        not row["active_io"],
        not row["metrics_busy"],
    ]
    if cfg["backend"] == "xfs":
        checks += [
            bool(row["mount"])
            and row["mount"]["uuid"] == row["filesystem_uuid"]
            and row["mount"]["noatime"],
            row["restore_enabled"],
        ]
    else:
        checks += [
            row["mount"] is None,
            not row["restore_enabled"],
            not row["restore_active"],
            row["rust_available"],
            cfg["backend"] != "raw_io_uring" or row["io_uring_disabled"] == 0,
        ]
    if not all(checks):
        raise ValueError(f"{host} readiness differs; inspect preflight.json")


def remote(host: str, request: dict[str, Any]) -> dict[str, Any]:
    """Run the deployed helper via SSH, or locally for the mmgi0 coordinator."""
    args = ["python3", f"{DEPLOY}/goal5_probe.py"]
    if host != "mmgi0":
        args = ["ssh", "-o", "BatchMode=yes", host, shlex.join(args)]
    completed = subprocess.run(
        args,
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=request.get("timeout", 120) + 30,
    )
    if completed.returncode:
        raise RuntimeError(f"{host}: {completed.stderr[-3000:]}")
    return json.loads(completed.stdout)


def preflight(cfg: dict[str, Any]) -> dict[str, Any]:
    """Collect read-only readiness evidence on all six hosts and the kernel target."""
    with ThreadPoolExecutor(max_workers=7) as pool:
        futures = {
            host: pool.submit(remote, host, {"action": "inspect", "cfg": cfg})
            for host in cfg["hosts"]
        }
        target = pool.submit(remote, "mmgt", {"action": "target", "cfg": cfg})
        snapshots = {host: future.result() for host, future in futures.items()}
        snapshots["target"] = target.result()
    write_json(Path(cfg["results_dir"]) / "preflight.json", snapshots)
    for host in cfg["hosts"]:
        validate_host(snapshots[host], cfg, host)
    if not snapshots["target"]["ready"]:
        raise ValueError("Target readiness differs; inspect preflight.json")
    return snapshots


class Run:
    """Bind a result directory to one immutable configuration and retain attempts."""

    def __init__(self, cfg: dict[str, Any]) -> None:
        """Reject config drift before creating or modifying existing run artifacts."""
        cells(cfg)
        if not re.fullmatch(r"goal5-[A-Za-z0-9_-]+", cfg["run_id"]):
            raise ValueError("Unsafe run_id")
        self.cfg = cfg
        self.directory = Path(cfg["results_dir"])
        self.status_path = self.directory / "status.json"
        self.digest = config_digest(cfg)
        if self.status_path.exists():
            status = json.loads(self.status_path.read_text())
            if status["config_sha256"] != self.digest:
                raise ValueError("Run configuration changed; choose fresh results")
        else:
            if self.directory.exists() and any(self.directory.iterdir()):
                raise ValueError("Unbound results directory is not empty")
            write_json(
                self.status_path, {"config_sha256": self.digest, "state": "created"}
            )
            write_json(self.directory / "config.json", cfg)

    def phase(self, in_flight: int, repetition: int, mode: str) -> dict[str, Any]:
        """Resume matching successful evidence or append a new bounded attempt."""
        self.check_stopped()
        stem = f"{mode}-if{in_flight}-r{repetition}"
        attempts = self.directory / stem
        attempts.mkdir(exist_ok=True)
        prior = sorted(attempts.glob("attempt-*"))
        if prior:
            rows = {}
            for host in self.cfg["hosts"]:
                path = prior[-1] / f"{host}.json"
                if path.exists():
                    row = json.loads(path.read_text())
                    if validate_evidence(
                        row, self.cfg, host, in_flight, repetition, mode
                    ):
                        rows[host] = row
            if len(rows) == 6:
                return rows
            if mode != "load":
                raise ValueError(
                    "Interrupted preparation may have seeded keys; use a fresh run "
                    "configuration. Existing corpus and evidence are preserved."
                )
        preflight(self.cfg)
        self.check_stopped()
        attempt = attempts / f"attempt-{len(prior) + 1:04d}"
        attempt.mkdir()
        requests = {
            host: {
                "action": "bench",
                "cfg": self.cfg,
                "host": host,
                "cell": {
                    "in_flight": in_flight,
                    "repetition": repetition,
                    "mode": mode,
                },
                "command": benchmark_args(self.cfg, host, in_flight, mode),
                "artifact": f"{attempt.name}-{stem}",
                "timeout": self.cfg["warmup_sec"] + self.cfg["duration_sec"] + 600,
                "config_sha256": self.digest,
            }
            for host in self.cfg["hosts"]
        }
        rows = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {}
            for host, request in requests.items():
                self.check_stopped()
                futures[host] = pool.submit(remote, host, request)
            for host, future in futures.items():
                try:
                    rows[host] = future.result()
                except Exception as error:
                    request = requests[host]
                    rows[host] = {
                        key: request[key]
                        for key in ("host", "cell", "command", "config_sha256")
                    }
                    rows[host].update(
                        exit_code=-255, result=None, transport_error=str(error)
                    )
                write_json(attempt / f"{host}.json", rows[host])
        for host, row in rows.items():
            if not validate_evidence(row, self.cfg, host, in_flight, repetition, mode):
                raise ValueError(f"Failed {mode} on {host}; evidence retained")
        return rows

    def check_stopped(self) -> None:
        """Raise ValueError when cleanup has cancelled further work for this run."""
        if (self.directory / "STOP").exists():
            raise ValueError("Run stopped; use a fresh run configuration")

    def execute(self, stop_at: str | None) -> None:
        """Measure after preparation; STOP/deadline leaves an incomplete run."""
        for mode in ("canary", "store", "verify"):
            attempts = self.directory / f"{mode}-if1-r0"
            if not attempts.exists():
                raise ValueError("Run prepare first")
            latest = sorted(attempts.glob("attempt-*"))[-1]
            for host in self.cfg["hosts"]:
                path = latest / f"{host}.json"
                if not path.exists() or not validate_evidence(
                    json.loads(path.read_text()), self.cfg, host, 1, 0, mode
                ):
                    raise ValueError("Preparation evidence is incomplete")
        summaries = []
        write_json(self.status_path, {"config_sha256": self.digest, "state": "running"})
        for in_flight, repetition in cells(self.cfg):
            if (self.directory / "STOP").exists() or (
                stop_at
                and datetime.now(ZoneInfo("America/Phoenix"))
                >= datetime.fromisoformat(stop_at)
            ):
                write_json(
                    self.status_path,
                    {"config_sha256": self.digest, "state": "stopped-incomplete"},
                )
                return
            rows = self.phase(in_flight, repetition, "load")
            summaries.append(summarize(rows, self.cfg, in_flight, repetition))
            write_json(self.directory / "summary.json", summaries)
        write_json(
            self.status_path, {"config_sha256": self.digest, "state": "complete"}
        )


def initialize(path: Path, model: str, backend: str) -> None:
    """Create a fresh OS configuration from live identities and source hashes."""
    if path.exists():
        raise ValueError("Configuration already exists")
    manifest = json.loads(Path(f"{SETUP}/manifest.json").read_text())
    validate_manifest(manifest)
    run_id = (
        datetime.now(ZoneInfo("America/Phoenix")).strftime("goal5-%Y%m%d-%H%M%S")
        + f"-kernel-{backend}-{model}"
    )
    cfg: dict[str, Any] = {
        "run_id": run_id,
        "results_dir": f"{DEPLOY}/results/{run_id}",
        "backend": backend,
        "target": "kernel",
        "profile": f"scripts/ipu-poc/models/{PROFILES[model]}",
        "manifest": manifest,
        "dataset_submits": 256,
        "in_flights": [1, 2, 4, 8, 16, 32, 64, 128, 256],
        "repetitions": 3,
        "warmup_sec": 90,
        "duration_sec": 60,
        "load_workers": 32,
        "iouring_queue_depth": 256,
        "max_phase_skew_sec": 5,
        "min_results_free_bytes": 2 * 1024**3,
        "hosts": {},
    }
    for index, host in enumerate(HOSTS):
        repo = "/root/anthropic/LMCache" if index < 4 else "/root/LMCache"
        cfg["hosts"][host] = {
            "repo": repo,
            "python": f"{repo}/.venv-bench-l2/bin/python",
            "numa": 1,
            "metrics_port": 9101 + index,
            "namespace_uuids": [m["uuid"] for m in private_members(manifest, host)],
        }
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {
            host: pool.submit(remote, host, {"action": "inspect", "cfg": cfg})
            for host in HOSTS
        }
        snapshots = {host: future.result() for host, future in futures.items()}
    for host, row in snapshots.items():
        cfg["hosts"][host]["provenance"] = row["provenance"]
    if (
        len(
            {
                json.dumps(
                    {
                        "files": r["provenance"]["files"],
                        "helpers": r["provenance"]["helpers"],
                        "runtime": {
                            name: artifact["sha256"]
                            for name, artifact in r["provenance"]["runtime"].items()
                        },
                    },
                    sort_keys=True,
                )
                for r in snapshots.values()
            }
        )
        != 1
    ):
        raise ValueError("Benchmark/profile source differs across hosts")
    cfg["geometry"] = snapshots["mmgi0"]["geometry"]
    if any(r["geometry"] != cfg["geometry"] for r in snapshots.values()):
        raise ValueError("Geometry differs across hosts")
    write_json(path, cfg)


def main() -> None:
    """Dispatch setup-independent planning, readiness, corpus and benchmark stages."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=("init-config", "plan", "preflight", "prepare", "run", "cleanup"),
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--model", choices=PROFILES, default="mixtral64k")
    parser.add_argument(
        "--backend", choices=("xfs", "raw_posix", "raw_io_uring"), default="xfs"
    )
    parser.add_argument("--ack-raw-overwrite", action="store_true")
    parser.add_argument(
        "--stop-at", help="ISO timestamp with UTC offset; checked per cell"
    )
    args = parser.parse_args()
    if args.action == "init-config":
        initialize(args.config, args.model, args.backend)
        return
    cfg = json.loads(args.config.read_text())
    if set(cfg["hosts"]) != set(HOSTS) or cfg["target"] != "kernel":
        raise ValueError("Goal5 currently requires all six hosts and the kernel target")
    cells(cfg)
    if args.stop_at and datetime.fromisoformat(args.stop_at).tzinfo is None:
        raise ValueError("--stop-at needs a UTC offset")
    if args.action == "plan":
        print(
            json.dumps(
                {
                    "run_id": cfg["run_id"],
                    "cells": cells(cfg),
                    "adapter": {h: adapter(cfg, h) for h in HOSTS},
                    "commands": {h: benchmark_args(cfg, h, 1, "load") for h in HOSTS},
                },
                indent=2,
            )
        )
        return
    if not Path(f"{SETUP}/manifest.json").exists() or (
        Path("/etc/nvme/hostnqn").read_text().strip()
        != cfg["manifest"]["hosts"]["mmgi0"]["host_nqn"]
    ):
        raise ValueError("Execute on the mmgi0 coordinator")
    run = Run(cfg)
    if args.action == "cleanup":
        (run.directory / "STOP").touch()
        for host in HOSTS:
            print(host, remote(host, {"action": "cleanup", "cfg": cfg}))
        return
    with (Path(DEPLOY) / "fleet.lock").open("w") as fleet:
        fcntl.flock(fleet, fcntl.LOCK_EX | fcntl.LOCK_NB)
        dispatch(run, args)


def dispatch(run: Run, args: argparse.Namespace) -> None:
    """Serialize a run's mutations and dispatch its explicitly selected stage."""
    cfg = run.cfg
    with (run.directory / "lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            if args.action == "preflight":
                preflight(cfg)
                print("All six hosts and kernel target ready")
            elif args.action == "prepare":
                if cfg["backend"] != "xfs" and not args.ack_raw_overwrite:
                    raise ValueError("Raw prepare requires --ack-raw-overwrite")
                run.phase(1, 0, "canary")
                run.phase(1, 0, "store")
                run.phase(1, 0, "verify")
            else:
                run.execute(args.stop_at)
        except Exception:
            write_json(
                run.status_path,
                {
                    "config_sha256": run.digest,
                    "state": (
                        "stopped-incomplete"
                        if (run.directory / "STOP").exists()
                        else "failed"
                    ),
                },
            )
            raise


if __name__ == "__main__":
    main()
