# SPDX-License-Identifier: Apache-2.0
"""Goal5: private eight-namespace arrays, fixed QPs and read/mixed curves.

Run on mmgi0. Use init-config, preflight, prepare, then run, each with --config.
prepare byte-checks a canary, seeds a corpus and checks load success in a fresh
process. XFS runs include 5:1 mixed cells after separate read preconditioning,
then reclaim exactly each cell's successful Store manifest. cleanup stops only
processes owned by this run; run preserves array geometry and QPs.
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
DEPLOY = str(Path(__file__).resolve().parent)
PROFILES = {
    "mixtral64k": "mixtral_8x22b_fp8_64k.yaml",
    "mixtral256k": "mixtral_8x22b_fp8.yaml",
    "minimax": "minimax_m3_bf16_tp8_odirect.yaml",
    "deepseek_v4_1_flash": "deepseek_v4_1_flash.yaml",
    "deepseek_v4_flash": "deepseek_v4_flash.yaml",
}


def validate_hosts(hosts: tuple[str, ...]) -> None:
    """Validate a nonempty, distinct initiator selection containing mmgi0.

    Args:
        hosts: Selected initiator aliases from mmgi0 through mmgi5.

    Raises:
        ValueError: A host is unknown, duplicated, or the coordinator is absent.
    """
    if (
        "mmgi0" not in hosts
        or len(set(hosts)) != len(hosts)
        or not set(hosts).issubset(HOSTS)
    ):
        raise ValueError("Select distinct known hosts including coordinator mmgi0")


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
        params = {
            "type": "fs_native",
            "base_path": f"/mnt/lmcache/{host}/goal5/{cfg['run_id']}",
            "num_workers": cfg["load_workers"],
            "use_odirect": True,
        }
        if "filesystem_plugin" in cfg:
            plugin = cfg["filesystem_plugin"]
            params.pop("type")
            params.update(
                io_depth=plugin["io_depth"],
                stats_directory=f"{DEPLOY}/host-results/{cfg['run_id']}",
            )
            return {
                "type": "native_plugin",
                "module_path": plugin["module_path"],
                "class_name": plugin["class_name"],
                "adapter_params": params,
            }
        return params
    if cfg["backend"] not in ("raw_posix", "raw_io_uring"):
        raise ValueError("Unknown backend")
    return {
        "type": "goal5_raw_recording" if cfg.get("raw_recorder") else "raw_block",
        "device_path": "/dev/md0",
        "slot_bytes": ((cfg["geometry"]["max_object_bytes"] + 8191) // 4096) * 4096,
        "header_bytes": 4096,
        "meta_total_bytes": cfg.get("raw_meta_total_bytes", 268435456),
        "use_odirect": True,
        "use_uring_cmd": False,
        "io_engine": "posix" if cfg["backend"] == "raw_posix" else "io_uring",
        "iouring_queue_depth": cfg["iouring_queue_depth"],
        "num_load_workers": cfg["load_workers"],
        "num_store_workers": 4,
        "num_lookup_workers": 1,
        "load_checkpoint_on_init": True,
        **(
            {"capacity_bytes": cfg["raw_capacity_bytes"][host]}
            if "raw_capacity_bytes" in cfg
            else {}
        ),
    }


def filesystem_base(cfg: dict[str, Any], host: str) -> str:
    """Return the owned filesystem root for blocking or native plugin clients."""
    selected = adapter(cfg, host)
    return str(selected.get("adapter_params", selected)["base_path"])


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
    matrix = [
        (value, repetition)
        for repetition in range(1, cfg["repetitions"] + 1)
        for value in values
    ]
    if "measurement_cells" not in cfg:
        return matrix
    selection = cfg["measurement_cells"]
    if not isinstance(selection, list) or not selection:
        raise ValueError("measurement_cells must select distinct matrix cells")
    selected = []
    for pair in selection:
        if (
            not isinstance(pair, (list, tuple))
            or len(pair) != 2
            or any(type(value) is not int for value in pair)
            or tuple(pair) not in matrix
            or tuple(pair) in selected
        ):
            raise ValueError("measurement_cells must select distinct matrix cells")
        selected.append(tuple(pair))
    return selected


def benchmark_args(
    cfg: dict[str, Any], host: str, in_flight: int, mode: str, repetition: int = 0
) -> list[str]:
    """Build a NUMA-bound command with a fixed, profile-specific key universe."""
    if mode not in ("load", "store", "verify", "canary", "mixed", "precondition"):
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
        "goal5_raw_recording" if cfg.get("raw_recorder") else "lmcache.cli.main",
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
    if mode not in ("canary", "mixed"):
        args += ["--only", "store" if mode == "store" else "load"]
    if mode in ("load", "mixed", "precondition"):
        args += [
            "--duration-sec",
            str(cfg["warmup_sec"] if mode == "precondition" else cfg["duration_sec"]),
            "--serve-metrics",
            str(row["metrics_port"]),
            "--metrics-bind-address",
            "127.0.0.1",
        ]
        if mode != "mixed":
            args += ["--warmup-sec", str(cfg["warmup_sec"] if mode == "load" else 0)]
        else:
            if cfg["backend"] not in ("xfs", "raw_io_uring") or repetition <= 0:
                raise ValueError("Mixed requires XFS/raw io_uring and a repetition")
            args += [
                "--read-write-ratio",
                "5:1",
                "--write-key-prefix",
                write_prefix(cfg, host, in_flight, repetition),
                "--no-skip-verify",
            ]
    if mode == "canary":
        args += ["--no-skip-verify"]
    return args


def write_prefix(
    cfg: dict[str, Any], host: str, in_flight: int, repetition: int
) -> str:
    """Return the Store namespace owned by exactly one host and mixed cell."""
    return (
        f"{cfg['run_id']}-writes-if{in_flight}-r{repetition}-{host}-"
        f"{cfg['geometry']['profile_sha256'][:12]}"
    )


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
        or row.get("command") != benchmark_args(cfg, host, in_flight, mode, repetition)
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
        or reported["mode"]
        != ("sustained" if mode in ("load", "mixed", "precondition") else "rounds")
        or (
            mode in ("load", "mixed", "precondition")
            and (
                reported["duration_sec"]
                != (
                    cfg["warmup_sec"] if mode == "precondition" else cfg["duration_sec"]
                )
                or reported["warmup_sec"]
                != (cfg["warmup_sec"] if mode == "load" else 0)
            )
        )
    ):
        raise ValueError("Reported benchmark configuration differs")
    if "filesystem_plugin" in cfg:
        native = row.get("native", {})
        terminal = native.get("terminal", {})
        reads = sum(
            op["total_success"]
            for key, op in metrics.items()
            if key.startswith("op_") and op["operation"] == "Load"
        )
        if (
            not terminal
            or terminal["pending_reads"]
            or terminal["submitted_reads"] != terminal["completed_reads"]
            or terminal["completed_reads"] < reads
            or terminal["max_worker_pending"] > cfg["filesystem_plugin"]["io_depth"]
            or terminal["max_total_pending"]
            > cfg["load_workers"] * cfg["filesystem_plugin"]["io_depth"]
            or (
                mode in ("load", "mixed", "precondition")
                and (
                    native.get("samples", 0) < 2
                    or row.get("sampled", {}).get("samples", 0) < 2
                )
            )
        ):
            return False
    if mode == "mixed":
        if (
            reported["write_key_prefix"]
            != write_prefix(cfg, host, in_flight, repetition)
            or reported["read_write_ratio_requested"] != "5:1"
        ):
            raise ValueError("Reported mixed configuration differs")
        operations = {
            value["operation"]: value
            for key, value in metrics.items()
            if key.startswith("op_")
        }
        if set(operations) != {"Load", "Store"}:
            raise ValueError("Reported mixed operations differ")
        for op in operations.values():
            span = op["phase_end_time_seconds"] - op["phase_start_time_seconds"]
            if (
                op.get("timed_out", False)
                or not op["total_keys"]
                or op["total_success"] != op["total_keys"]
                or op["total_keys"] % cfg["geometry"]["objects_per_submit"]
                or op["total_success_bytes"]
                != (
                    op["total_keys"]
                    // cfg["geometry"]["objects_per_submit"]
                    * cfg["geometry"]["task_size_bytes"]
                )
                or not math.isfinite(span)
                or span <= 0
                or not math.isfinite(op["window_sec"])
                or op["window_sec"] + 0.001 < cfg["duration_sec"]
                or not all(
                    math.isfinite(op[k]) and op[k] >= 0
                    for k in ("submit_latency_p50_ms", "submit_latency_p99_ms")
                )
            ):
                return False
        load, store = operations["Load"], operations["Store"]
        if any(
            load[k] != store[k]
            for k in (
                "phase_start_time_seconds",
                "phase_end_time_seconds",
                "window_sec",
            )
        ):
            return False
        return (
            4.95 <= load["total_success_bytes"] / store["total_success_bytes"] <= 5.05
        )
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
    if mode in ("load", "precondition"):
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
            or window + 0.001
            < (cfg["warmup_sec"] if mode == "precondition" else cfg["duration_sec"])
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
    rows: dict[str, Any],
    cfg: dict[str, Any],
    in_flight: int,
    repetition: int,
    mode: str = "load",
) -> dict[str, Any]:
    """Sum per-host successful-payload rates; retain per-host submit quantiles."""
    if set(rows) != set(cfg["hosts"]):
        raise ValueError("Incomplete selected-host evidence")
    for host, row in rows.items():
        if not validate_evidence(row, cfg, host, in_flight, repetition, mode):
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
    if mode == "mixed":
        hosts = {}
        read_bytes = write_bytes = 0
        for host, row in rows.items():
            operations = {}
            for key, op in row["result"]["metrics"].items():
                if not key.startswith("op_"):
                    continue
                span = op["phase_end_time_seconds"] - op["phase_start_time_seconds"]
                operations[op["operation"]] = {
                    "payload_gbps_decimal": op["total_success_bytes"] * 8 / span / 1e9,
                    "successful_bytes": op["total_success_bytes"],
                    "p50_submit_ms": op["submit_latency_p50_ms"],
                    "p99_submit_ms": op["submit_latency_p99_ms"],
                    "submit_latency_count": op["submit_latency_count"],
                    "submit_latency_sample_count": op.get(
                        "submit_latency_sample_count", op["submit_latency_count"]
                    ),
                    "latency_quantile_scope": (
                        "latest completions"
                        if op.get(
                            "submit_latency_sample_count", op["submit_latency_count"]
                        )
                        < op["submit_latency_count"]
                        else "all completions"
                    ),
                }
            hosts[host] = {
                "operations": operations,
                **(
                    {"sampled": row["sampled"], "native": row["native"]}
                    if "filesystem_plugin" in cfg
                    else {}
                ),
            }
            read_bytes += operations["Load"]["successful_bytes"]
            write_bytes += operations["Store"]["successful_bytes"]
        return {
            "in_flight": in_flight,
            "repetition": repetition,
            "mode": mode,
            "payload_gbps_decimal": sum(
                op["payload_gbps_decimal"]
                for h in hosts.values()
                for op in h["operations"].values()
            ),
            "read_write_ratio_achieved": read_bytes / write_bytes,
            "latency_unit": (
                "submit-to-observed completion; not individual object or TTFT"
            ),
            "hosts": hosts,
        }
    return {
        "in_flight": in_flight,
        "repetition": repetition,
        "payload_gbps_decimal": sum(rates.values()),
        "latency_unit": "submit-to-observed completion; not individual object or TTFT",
        "hosts": {
            host: {
                "payload_gbps_decimal": rates[host],
                **(
                    {"sampled": rows[host]["sampled"], "native": rows[host]["native"]}
                    if "filesystem_plugin" in cfg
                    else {}
                ),
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
            cfg["backend"] != "raw_io_uring"
            or (
                row["io_uring_disabled"]
                == cfg.get("io_uring_policy", {}).get("disabled", 0)
                and (
                    "io_uring_policy" not in cfg
                    or (
                        row.get("io_uring_group") == cfg["io_uring_policy"]["group"]
                        and row.get("io_uring_ready") is True
                    )
                )
            ),
        ]
    if not all(checks):
        raise ValueError(f"{host} readiness differs; inspect preflight.json")


def remote(host: str, request: dict[str, Any]) -> dict[str, Any]:
    """Run the deployed helper via SSH, or locally for the mmgi0 coordinator."""
    helper = (
        request["cfg"].get("probe_helper", "goal5_probe.py")
        if host in request["cfg"]["hosts"]
        else "goal5_probe.py"
    )
    args = ["python3", f"{DEPLOY}/{helper}"]
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
    """Collect readiness evidence on selected hosts and the kernel target."""
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

    def phase(
        self, in_flight: int, repetition: int, mode: str, fresh: bool = False
    ) -> dict[str, Any]:
        """Resume matching successful evidence or append a new bounded attempt."""
        self.check_stopped()
        stem = f"{mode}-if{in_flight}-r{repetition}"
        attempts = self.directory / stem
        attempts.mkdir(exist_ok=True)
        prior = sorted(attempts.glob("attempt-*"))
        if fresh and mode != "precondition":
            raise ValueError("Only read-only preconditioning can force a fresh attempt")
        if prior and not fresh:
            rows = {}
            for host in self.cfg["hosts"]:
                path = prior[-1] / f"{host}.json"
                if path.exists():
                    row = json.loads(path.read_text())
                    if validate_evidence(
                        row, self.cfg, host, in_flight, repetition, mode
                    ):
                        rows[host] = row
            if len(rows) == len(self.cfg["hosts"]):
                return rows
            if mode not in ("load", "precondition"):
                raise ValueError(
                    "Interrupted preparation may have seeded keys; use a fresh run "
                    "configuration. Existing corpus and evidence are preserved."
                )
        preflight(self.cfg)
        self.check_stopped()
        attempt = attempts / f"attempt-{len(prior) + 1:04d}"
        attempt.mkdir()
        write_json(
            self.status_path,
            {
                "config_sha256": self.digest,
                "state": "running",
                "mode": mode,
                "in_flight": in_flight,
                "repetition": repetition,
            },
        )
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
                "command": benchmark_args(self.cfg, host, in_flight, mode, repetition),
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
        read_cells = cells(self.cfg) if self.cfg.get("include_read", True) else []
        for in_flight, repetition in read_cells:
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
        if not read_cells:
            write_json(self.directory / "summary.json", summaries)
        if self.cfg.get("include_mixed", False):
            mixed_summaries = []
            for in_flight, repetition in cells(self.cfg):
                self.check_stopped()
                if stop_at and datetime.now(ZoneInfo("America/Phoenix")) >= (
                    datetime.fromisoformat(stop_at)
                ):
                    write_json(
                        self.status_path,
                        {"config_sha256": self.digest, "state": "stopped-incomplete"},
                    )
                    return
                if not any(
                    (self.directory / f"mixed-if{in_flight}-r{repetition}").glob(
                        "attempt-*"
                    )
                ):
                    self.phase(in_flight, repetition, "precondition", fresh=True)
                rows = self.phase(in_flight, repetition, "mixed")
                summary = summarize(rows, self.cfg, in_flight, repetition, "mixed")
                for host, row in rows.items():
                    receipt = remote(
                        host,
                        {
                            "action": "reclaim",
                            "cfg": self.cfg,
                            "host": host,
                            "row": row,
                        },
                    )
                    write_json(
                        self.directory
                        / f"reclaim-if{in_flight}-r{repetition}-{host}.json",
                        receipt,
                    )
                mixed_summaries.append(summary)
                write_json(self.directory / "mixed-summary.json", mixed_summaries)
        write_json(
            self.status_path, {"config_sha256": self.digest, "state": "complete"}
        )


def initialize(
    path: Path, model: str, backend: str, hosts: tuple[str, ...] = HOSTS
) -> None:
    """Create a fresh configuration from selected hosts' live identities.

    Args:
        path: New configuration file path.
        model: Registered profile name.
        backend: Storage backend name.
        hosts: Distinct initiators including the mmgi0 coordinator.

    Raises:
        ValueError: Selection is invalid, the file exists, or host sources differ.
    """
    validate_hosts(hosts)
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
        "include_mixed": backend == "xfs",
        "hosts": {},
    }
    for host in hosts:
        index = HOSTS.index(host)
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
            for host in hosts
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
        "--hosts",
        nargs="+",
        choices=HOSTS,
        default=list(HOSTS),
        help="Initiators for init-config; must include mmgi0 (default: all six)",
    )
    parser.add_argument(
        "--backend", choices=("xfs", "raw_posix", "raw_io_uring"), default="xfs"
    )
    parser.add_argument("--ack-raw-overwrite", action="store_true")
    parser.add_argument(
        "--stop-at", help="ISO timestamp with UTC offset; checked per cell"
    )
    args = parser.parse_args()
    if args.action == "init-config":
        initialize(args.config, args.model, args.backend, tuple(args.hosts))
        return
    cfg = json.loads(args.config.read_text())
    validate_hosts(tuple(cfg["hosts"]))
    if cfg["target"] != "kernel":
        raise ValueError("Goal5 requires the kernel target")
    cells(cfg)
    if args.stop_at and datetime.fromisoformat(args.stop_at).tzinfo is None:
        raise ValueError("--stop-at needs a UTC offset")
    if args.action == "plan":
        print(
            json.dumps(
                {
                    "run_id": cfg["run_id"],
                    "cells": cells(cfg) if cfg.get("include_read", True) else [],
                    "adapter": {h: adapter(cfg, h) for h in cfg["hosts"]},
                    "commands": {
                        h: benchmark_args(cfg, h, 1, "load") for h in cfg["hosts"]
                    },
                    "mixed_cells": cells(cfg) if cfg.get("include_mixed") else [],
                    "mixed_commands": (
                        {h: benchmark_args(cfg, h, 1, "mixed", 1) for h in cfg["hosts"]}
                        if cfg.get("include_mixed")
                        else {}
                    ),
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
        for host in cfg["hosts"]:
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
                print("All selected hosts and kernel target ready")
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
