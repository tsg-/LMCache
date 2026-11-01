# SPDX-License-Identifier: Apache-2.0
"""Extend the authorized restricted policy after matching fleet AIO controls."""

# Future
from __future__ import annotations

# Standard
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
import argparse
import copy
import fcntl
import hashlib
import json
import shlex
import signal
import subprocess
import time

# Third Party
from goal5_fio import fio_args, summarize_fio
from goal5_overnight import DEPLOY, config_digest, preflight, validate_host, write_json
from goal5_probe import bench, inspect
from goal5_uring_readiness import (
    GROUP,
    PERSISTENT,
    POLICY,
    check_control_rates,
    probe,
)

HOSTS = ["mmgi0", "mmgi2", "mmgi3", "mmgi4", "mmgi5"]


def validate_probes(
    before: dict[str, Any], root: dict[str, Any], unprivileged: dict[str, Any]
) -> None:
    """Require working standard AIO and exactly the approved ring permissions."""
    for row in (before, root, unprivileged):
        if row["aio_setup_rc"] or row["aio_destroy_rc"]:
            raise ValueError("Standard AIO context compatibility failed")
        if row["io_uring_group"] != "-1":
            raise ValueError("io_uring group changed")
    if before["io_uring_disabled"] != "2" or before["uring_errno"] != 1:
        raise ValueError("Before-change restricted state differs")
    if root["io_uring_disabled"] != "1" or root["uring_setup_rc"] < 0:
        raise ValueError("Restricted root ring creation failed")
    if (
        unprivileged["io_uring_disabled"] != "1"
        or unprivileged["uid"] != 65534
        or unprivileged["uring_setup_rc"] >= 0
        or unprivileged["uring_errno"] != 1
    ):
        raise ValueError("Restricted unprivileged ring denial failed")


def validate_control(
    row: dict[str, Any], host: str, digest: str, argv: list[str]
) -> float:
    """Validate command/binding, achieved depth and complete successful read bytes."""
    if (
        row["host"] != host
        or row["config_sha256"] != digest
        or row["command"] != argv
        or row["exit_code"]
        or not row["result"]
    ):
        raise ValueError("AIO control process or binding differs")
    if row["result"]["jobs"][0]["iodepth_level"]["32"] < 99:
        raise ValueError("AIO control achieved depth below 99%")
    return summarize_fio(row["result"], "read")["payload_gbps_decimal"]


def apply_host(cfg: dict[str, Any], host: str) -> dict[str, Any]:
    """Persist one host only on compatibility success; otherwise restore policy2."""
    root = Path(cfg["policy_results_root"]) / host
    root.mkdir(parents=True, exist_ok=False)
    digest = config_digest(cfg)
    write_json(root / "config.json", cfg)
    committed = False
    changed = False
    receipt: dict[str, Any] = {
        "host": host,
        "config_sha256": digest,
        "authorization": cfg["policy_authorization"],
        "started_at": time.time(),
        "state": "running",
        "reboot_tested": False,
        "control_scope": "Two matching before/after read controls with five hosts "
        "concurrent. A 10% median regression is a conservative review trigger; "
        "two runs do not establish statistical equivalence.",
    }

    def interrupt(signum: int, _frame: Any) -> None:
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupt)
    with Path("/run/lock/lmcache-goal5-md0.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            if (
                POLICY.read_text().strip() != "2"
                or GROUP.read_text().strip() != "-1"
                or PERSISTENT.exists()
            ):
                raise ValueError("Initial policy or persistent file differs")
            for path, expected in cfg["policy_artifacts"].items():
                if hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected:
                    raise ValueError(f"Policy helper/FIO source differs: {path}")
            readiness = inspect(cfg)
            write_json(root / "preflight.json", readiness)
            validate_host(readiness, cfg, host)
            corpus = Path(f"/mnt/lmcache/{host}/fio/{cfg['fio_corpus_run_id']}")
            if corpus.is_symlink() or any(
                (corpus / f"scratch.{i}").stat().st_size != 16 * 1024**3
                for i in range(32)
            ):
                raise ValueError("Retained FIO corpus differs")
            receipt["before"] = probe()
            receipt["before_unprivileged"] = probe(True)
            if any(
                row["aio_setup_rc"] or row["aio_destroy_rc"] or row["uring_errno"] != 1
                for row in (
                    receipt["before"],
                    receipt["before_unprivileged"],
                )
            ):
                raise ValueError("Initial syscall behavior differs")
            rates: dict[str, list[float]] = {}
            control = copy.deepcopy(cfg)
            control["run_id"] = cfg["fio_corpus_run_id"]
            argv = fio_args(control, host, "64k", "read")
            for phase in ("before", "after"):
                write_json(root / "status.json", {"state": "running", "phase": phase})
                if phase == "after":
                    changed = True
                    POLICY.write_text("1\n")
                    receipt["after_root"] = probe()
                    receipt["after_unprivileged"] = probe(True)
                    validate_probes(
                        receipt["before"],
                        receipt["after_root"],
                        receipt["after_unprivileged"],
                    )
                    validate_probes(
                        receipt["before_unprivileged"],
                        receipt["after_root"],
                        receipt["after_unprivileged"],
                    )
                rates[phase] = []
                for repetition in (1, 2):
                    row = bench(
                        {
                            "cfg": cfg,
                            "host": host,
                            "cell": {"mode": f"aio-{phase}", "repetition": repetition},
                            "command": argv,
                            "artifact": f"aio-{phase}-qd32-r{repetition}",
                            "config_sha256": digest,
                            "timeout": 240,
                        }
                    )
                    write_json(root / f"libaio-{phase}-r{repetition}.json", row)
                    rates[phase].append(validate_control(row, host, digest, argv))
            receipt["rates_gbps"] = rates
            receipt["median_rate_ratio"] = check_control_rates(
                rates["before"], rates["after"]
            )
            if POLICY.read_text().strip() != "1" or GROUP.read_text().strip() != "-1":
                raise ValueError("Live policy drifted during controls")
            with PERSISTENT.open("x") as stream:
                stream.write(
                    "# Restricted io_uring for the authorized LMCache MMG tests.\n"
                    "kernel.io_uring_disabled = 1\n"
                    "kernel.io_uring_group = -1\n"
                )
            committed = True
            receipt["state"] = "complete"
        except BaseException as error:
            receipt.update(state="failed", error=str(error))
            raise
        finally:
            if changed and not committed:
                POLICY.write_text("2\n")
            receipt["live_io_uring_disabled"] = POLICY.read_text().strip()
            write_json(root / "receipt.json", receipt)
            write_json(root / "status.json", {"state": receipt["state"]})
    return receipt


def main() -> None:
    """Coordinate five gated hosts under the fleet lock, or execute one host."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--host", choices=HOSTS)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    if (
        cfg["repetitions"] != 2
        or cfg["policy_hosts"] != HOSTS
        or cfg["policy_authorization"]["answer"]
        != "Extend policy and run six-host tests"
    ):
        raise ValueError("Explicit five-host policy authorization required")
    if args.host:
        apply_host(cfg, args.host)
        return
    with (Path(DEPLOY) / "fleet.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        preflight(cfg)

        def run(host: str) -> dict[str, Any]:
            argv = [
                "python3",
                f"{DEPLOY}/goal5_fleet_uring_readiness.py",
                "--config",
                str(args.config),
                "--host",
                host,
            ]
            result = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", host, shlex.join(argv)],
                text=True,
                capture_output=True,
                timeout=1500,
            )
            return {
                "host": host,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }

        with ThreadPoolExecutor(max_workers=5) as pool:
            rows = list(pool.map(run, HOSTS))
        write_json(Path(cfg["results_dir"]) / "policy-hosts.json", rows)
        if any(row["exit_code"] for row in rows):
            raise ValueError("Fleet policy gate failed; inspect retained host receipts")
        write_json(Path(cfg["results_dir"]) / "status.json", {"state": "complete"})


if __name__ == "__main__":
    main()
