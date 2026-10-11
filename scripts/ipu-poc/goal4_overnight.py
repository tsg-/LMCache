# SPDX-License-Identifier: Apache-2.0
"""Run the guarded Goal 4 load curves from mmgi0 without a laptop session."""

from __future__ import annotations

# Standard
import argparse
import concurrent.futures
import json
import os
import re
import shlex
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


HOSTS = ("mmgi0", "mmgi1", "mmgi2", "mmgi3")
PORTS = {"mmgi0": 9101, "mmgi1": 9102, "mmgi2": 9103, "mmgi3": 9104}
IF_FLEET = "if-fleet-20260924-2329"
BLOCKS = (
    ("A", "minimax", 128, (1, 2, 3, 4, 8, 16, 32, 64, 128, 256)),
    ("B", "mixtral", 128, (1, 2, 3, 4, 6, 8, 16, 32, 64, 128, 256)),
    ("C", "minimax", 1, (1, 2, 4, 8)),
)
BENCH_SCRIPT = """\
set -euo pipefail
repo=$1
profile=$2
corpus=$3
output=$4
in_flight=$5
metrics_port=$6
cd "$repo"
base_path="/mnt/lmcache/bench-l2/$corpus/$(hostname -s)"
[ -d "$base_path" ]
[ -n "$(find "$base_path" -maxdepth 1 -name '*.data' -print -quit)" ]
BASE_PATH="$base_path" PREFIX="$corpus" OUTPUT="$output" \
  IN_FLIGHT="$in_flight" NUM_WORKERS=32 WARMUP_SEC=90 DURATION_SEC=60 \
  METRICS_PORT="$metrics_port" \
  bash scripts/ipu-poc/run_model_geometry.sh sustained-load "$profile"
"""


class StorageRecoveryRequired(RuntimeError):
    """Storage is unhealthy and automated reconnect must stop."""


def _ssh_command(host: str, argv: list[str]) -> list[str]:
    if host == "mmgi0":
        return argv
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ConnectTimeout=5",
        "-o",
        "ServerAliveInterval=5",
        "-o",
        "ServerAliveCountMax=2",
        host,
        shlex.join(argv),
    ]


def _run(
    host: str,
    argv: list[str],
    *,
    timeout: float = 20,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _ssh_command(host, argv),
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _host_result_ok(result: dict[str, Any]) -> tuple[bool, str]:
    try:
        row = result["metrics"]["op_0"]
        if row["operation"] != "Load":
            return False, "operation is not Load"
        if row["total_success"] != row["total_keys"] or row["total_keys"] <= 0:
            return False, "total_success differs from total_keys"
        if float(row["phase_start_time_seconds"]) <= 0 or float(
            row["phase_end_time_seconds"]
        ) <= float(row["phase_start_time_seconds"]):
            return False, "measured phase timestamps are absent or invalid"
    except (KeyError, TypeError, ValueError) as exc:
        return False, f"invalid bench_l2 result: {exc}"
    return True, ""


def _cells(
    block: str, model: str, qps: int, in_flights: tuple[int, ...]
) -> list[tuple[str, str, int, int, int]]:
    return [
        (block, model, qps, in_flight, rep)
        for rep in (1, 2, 3)
        for in_flight in in_flights
    ]


class Overnight:
    """Own one resumable, per-cell checked benchmark run."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.run_id = config["run_id"]
        self.root = Path(config["results_dir"])
        self.root.mkdir(parents=True, exist_ok=True)
        self.status_path = self.root / f"goal4-overnight-{self.run_id}.status.json"
        self.stop_path = self.root / f"goal4-overnight-{self.run_id}.STOP"
        self.log_path = self.root / f"goal4-overnight-{self.run_id}.log"
        self.matrix_started = False
        self.restore_attempted = False
        if self.status_path.exists():
            self.status = json.loads(self.status_path.read_text())
        else:
            now = datetime.now().astimezone()
            cutoff = now.replace(hour=7, minute=0, second=0, microsecond=0)
            if cutoff <= now:
                cutoff += timedelta(days=1)
            self.status = {
                "run_id": self.run_id,
                "start_time": now.isoformat(),
                "cutoff_time": cutoff.isoformat(),
                "current_block": "preflight",
                "current_cell": None,
                "last_success": None,
                "failures": [],
                "reconnects": [],
                "cells": {},
                "unit": "goal4-overnight.service",
                "review_files": config["review_files"],
            }
        self._save()

    def log(self, message: str) -> None:
        """Record one timestamped controller event on mmgi0 and in journald."""
        line = f"{datetime.now().astimezone().isoformat()} {message}"
        print(line, flush=True)
        with self.log_path.open("a") as output:
            output.write(line + "\n")

    def _save(self) -> None:
        self.status["heartbeat_timestamp"] = datetime.now().astimezone().isoformat()
        _atomic_json(self.status_path, self.status)

    def _cell_id(self, cell: tuple[str, str, int, int, int]) -> str:
        block, model, qps, in_flight, rep = cell
        return (
            f"goal4-overnight-{self.run_id}-{block}-{model}"
            f"-qp{qps}-if{in_flight}-rep{rep}"
        )

    def _host_path(self, cell: tuple[str, str, int, int, int], host: str) -> Path:
        return self.root / f"{self._cell_id(cell)}-{host}.json"

    def _host_command(
        self, host: str, cell: tuple[str, str, int, int, int]
    ) -> subprocess.CompletedProcess[str]:
        _, model, _, in_flight, _ = cell
        max_seconds = 324 if model == "minimax" else 408
        path = self._host_path(cell, host)
        command = [
            "timeout",
            "-k",
            "5",
            str(max_seconds),
            "bash",
            "-s",
            "--",
            self.config["repo_dir"],
            self.config["models"][model]["profile"],
            self.config["models"][model]["corpus"],
            str(path),
            str(in_flight),
            str(PORTS[host]),
        ]
        try:
            result = _run(
                host, command, input_text=BENCH_SCRIPT, timeout=max_seconds + 20
            )
        except subprocess.TimeoutExpired as exc:
            result = subprocess.CompletedProcess(command, 124, "", str(exc))
        (self.root / f"{self._cell_id(cell)}-{host}.log").write_text(
            result.stdout + result.stderr
        )
        return result

    def _coordinator(
        self, cell: tuple[str, str, int, int, int]
    ) -> subprocess.CompletedProcess[str]:
        _, _, _, in_flight, _ = cell
        cell_id = self._cell_id(cell)
        fleet_path = self.root / f"{cell_id}-fleet.json"
        template = str(self.root / f"{cell_id}-{{host}}.json")
        argv = [
            "timeout",
            "-k",
            "5",
            "240",
            "python3",
            self.config["coordinator_script"],
            "--run-id",
            cell_id,
            "--in-flight",
            str(in_flight),
            "--duration-sec",
            "60",
            "--host-result-template",
            template,
            "--output",
            str(fleet_path),
        ]
        try:
            result = _run("mmgi1", argv, timeout=260)
        except subprocess.TimeoutExpired as exc:
            result = subprocess.CompletedProcess(argv, 124, "", str(exc))
        (self.root / f"{cell_id}-fleet.log").write_text(result.stdout + result.stderr)
        return result

    def _fetch_host_result(
        self, cell: tuple[str, str, int, int, int], host: str
    ) -> tuple[bool, str, float]:
        path = self._host_path(cell, host)
        if host != "mmgi0":
            fetched = _run(host, ["cat", str(path)], timeout=15)
            if fetched.returncode:
                return False, f"{host}: missing {path}: {fetched.stderr.strip()}", 0
            path.write_text(fetched.stdout)
        try:
            result = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return False, f"{host}: unreadable {path}: {exc}", 0
        valid, reason = _host_result_ok(result)
        start = (
            result.get("metrics", {}).get("op_0", {}).get("phase_start_time_seconds")
        )
        try:
            start_value = float(start or 0)
        except (TypeError, ValueError):
            start_value = 0
        return valid, f"{host}: {reason}" if reason else "", start_value

    def _completed(self, cell: tuple[str, str, int, int, int]) -> bool:
        return all(self._fetch_host_result(cell, host)[0] for host in HOSTS)

    def _kill_own_bench(self) -> None:
        script = """\
import os
import signal
from pathlib import Path
needle = os.environ["GOAL4_NEEDLE"]
for entry in Path("/proc").iterdir():
    if not entry.name.isdigit() or int(entry.name) == os.getpid():
        continue
    try:
        args = (entry / "cmdline").read_bytes().split(b"\\0")
        text = [part.decode(errors="ignore") for part in args]
        if "lmcache.cli.main" in text and "bench" in text and "l2" in text:
            if any(needle in part for part in text):
                os.kill(int(entry.name), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
"""
        needle = f"goal4-overnight-{self.run_id}-"
        for host in HOSTS:
            argv = ["env", f"GOAL4_NEEDLE={needle}", "python3", "-c", script]
            result = _run(host, argv, timeout=15)
            if result.returncode:
                self.log(f"cleanup warning on {host}: {result.stderr.strip()}")

    def run_cell(self, cell: tuple[str, str, int, int, int]) -> bool:
        """Run all initiators concurrently and record one verified cell."""
        cell_id = self._cell_id(cell)
        self.status["current_block"] = cell[0]
        self.status["current_cell"] = cell_id
        self._save()
        if self._completed(cell):
            self.log(f"resume skip: {cell_id}")
            self.status["cells"][cell_id] = "complete"
            self._save()
            return True
        self._storage_healthy()
        self.log(f"cell start: {cell_id}")
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            coordinator = executor.submit(self._coordinator, cell)
            hosts = {
                host: executor.submit(self._host_command, host, cell) for host in HOSTS
            }
            host_results = {host: future.result() for host, future in hosts.items()}
            fleet_result = coordinator.result()
        problems = [
            f"{host}: bench exit={result.returncode}"
            for host, result in host_results.items()
            if result.returncode
        ]
        starts = []
        for host in HOSTS:
            valid, reason, start = self._fetch_host_result(cell, host)
            if not valid:
                problems.append(reason)
            else:
                starts.append(start)
        if len(starts) == 4:
            self.status["cells"][cell_id] = {
                "phase_start_skew_seconds": max(starts) - min(starts),
                "fleet_result": "missing",
            }
        if fleet_result.returncode:
            self.log(
                f"fleet telemetry advisory: {cell_id} exit={fleet_result.returncode}"
            )
        else:
            fleet_path = self.root / f"{cell_id}-fleet.json"
            try:
                fetched = _run("mmgi1", ["cat", str(fleet_path)], timeout=15)
            except subprocess.TimeoutExpired as exc:
                self.log(f"fleet telemetry fetch advisory: {cell_id}: {exc}")
            else:
                if fetched.returncode == 0:
                    fleet_path.write_text(fetched.stdout)
                    if len(starts) == 4:
                        self.status["cells"][cell_id]["fleet_result"] = "valid"
                else:
                    self.log(
                        f"fleet telemetry fetch advisory: {cell_id} "
                        f"exit={fetched.returncode}"
                    )
        if problems:
            self._kill_own_bench()
            reason = "; ".join(problems)
            self.status["cells"][cell_id] = {"failed": reason}
            self.status["failures"].append({"cell": cell_id, "reason": reason})
            self.log(f"cell failed: {cell_id}: {reason}")
            self._save()
            self._storage_healthy()
            return False
        self.status["last_success"] = cell_id
        self.log(f"cell complete: {cell_id}")
        self._save()
        return True

    def _controllers(self) -> dict[str, list[dict[str, Any]]]:
        script = """\
import json
from pathlib import Path
rows = []
for controller in Path("/sys/class/nvme").glob("nvme[0-9]*"):
    nqn_path = controller / "subsysnqn"
    if nqn_path.exists() and nqn_path.read_text().startswith("nqn.2026-08.lab.mmgt:"):
        rows.append({"controller": controller.name,
                     "queue_count": int((controller / "queue_count").read_text()),
                     "state": (controller / "state").read_text().strip()})
print(json.dumps(sorted(rows, key=lambda row: row["controller"])))
"""
        rows: dict[str, list[dict[str, Any]]] = {}
        for host in HOSTS:
            result = _run(host, ["python3", "-c", script], timeout=15)
            if result.returncode:
                raise RuntimeError(f"{host} QP read failed: {result.stderr.strip()}")
            rows[host] = json.loads(result.stdout)
        return rows

    def verify_qps(self, qps: int) -> dict[str, list[dict[str, Any]]]:
        """Require four live controllers per host at the requested queue count."""
        rows = self._controllers()
        for host, controllers in rows.items():
            if len(controllers) != 4 or any(
                row["queue_count"] != qps + 1 or row["state"] != "live"
                for row in controllers
            ):
                raise RuntimeError(
                    f"{host} QP={qps} verification failed: {controllers}"
                )
        self.log(f"verified all 16 controllers at QP={qps}")
        return rows

    def _corpus(self, model: str) -> None:
        profile = self.config["models"][model]["profile"]
        corpus = self.config["models"][model]["corpus"]
        for host in HOSTS:
            script = """\
set -euo pipefail
repo=$1
profile=$2
corpus=$3
base="/mnt/lmcache/bench-l2/$corpus/$(hostname -s)"
[ -r "$repo/$profile" ] && [ -d "$base" ]
file=$(find "$base" -maxdepth 1 -name '*.data' -print -quit)
[ -n "$file" ]
head -c 4096 "$file" >/dev/null
"""
            result = _run(
                host,
                ["bash", "-s", "--", self.config["repo_dir"], profile, corpus],
                input_text=script,
                timeout=20,
            )
            if result.returncode:
                raise RuntimeError(
                    f"{host} {model} corpus readback failed: {result.stderr.strip()}"
                )

    def _no_io(self) -> None:
        script = """\
set -euo pipefail
if pgrep -af '[l]mcache.cli.main bench l2|[f]io|[n]vme connect'; then
  exit 2
fi
for port in 9101 9102 9103 9104; do
  if ss -lnt "( sport = :$port )" | grep -q LISTEN; then
    echo "metrics port $port is occupied"
    exit 2
  fi
done
"""
        for host in HOSTS:
            result = _run(host, ["bash", "-s"], input_text=script, timeout=15)
            if result.returncode:
                raise RuntimeError(
                    f"{host} has active I/O or metrics port: "
                    f"{result.stdout} {result.stderr}"
                )

    def _storage_healthy(self) -> None:
        script = """\
set -euo pipefail
[ "$(< /sys/block/md0/md/array_state)" != broken ]
[ "$(findmnt -rn -M /mnt/lmcache -o SOURCE)" = /dev/md0 ]
members=$(find /sys/block/md0/slaves -mindepth 1 -maxdepth 1 -name 'nvme*n*')
[ "$(printf '%s\\n' "$members" | sed '/^$/d' | wc -l)" -eq 4 ]
"""
        for host in HOSTS:
            result = _run(host, ["bash", "-s"], input_text=script, timeout=15)
            if result.returncode:
                raise StorageRecoveryRequired(
                    f"{host} md0 or mount is unhealthy; leave storage for a human"
                )

    def _code_provenance(self) -> None:
        script = """\
import hashlib
import json
import subprocess
from pathlib import Path
repo = Path("/root/anthropic/LMCache")
files = (
    "lmcache/cli/commands/bench/l2_adapter_bench/command.py",
    "scripts/ipu-poc/run_model_geometry.sh",
    "scripts/ipu-poc/models/minimax_m3_bf16_tp8_odirect.yaml",
    "scripts/ipu-poc/models/mixtral_8x22b_fp8_64k.yaml",
)
def git(*args):
    return subprocess.check_output(
        ["git", "-C", str(repo), *args], text=True
    ).strip()
print(json.dumps({
    "git_sha": git("rev-parse", "HEAD"),
    "git_status": git("status", "--porcelain", "--untracked-files=no"),
    "file_sha256": {
        name: hashlib.sha256((repo / name).read_bytes()).hexdigest()
        for name in files
    },
}))
"""
        rows = {}
        for host in HOSTS:
            result = _run(host, ["python3", "-c", script], timeout=20)
            if result.returncode:
                raise RuntimeError(
                    f"{host} code provenance failed: {result.stderr.strip()}"
                )
            rows[host] = json.loads(result.stdout)
        self.status["code_provenance"] = rows
        self._save()
        expected = self.config["expected_code_sha256"]
        for host, row in rows.items():
            if row["git_sha"] != self.config["expected_git_sha"]:
                raise RuntimeError(f"{host} Git SHA differs from reviewed config")
            for name, sha in expected.items():
                if row["file_sha256"][name] != sha:
                    raise RuntimeError(f"{host} {name} differs from reviewed config")
        self.log("all four initiators match reviewed code and profile hashes")

    def reconnect(self, qps: int) -> None:
        """Apply the existing guarded reconnect procedure and verify all hosts."""
        self._no_io()
        self._storage_healthy()
        argv = [
            "timeout",
            "-k",
            "5",
            "600",
            "env",
            "APPLY=1",
            f"RUN_ID=goal4-overnight-{self.run_id}",
            "bash",
            self.config["reconnect_script"],
            "reconnect-fleet",
            str(qps),
            self.config["inventory"],
        ]
        self.log(f"reconnect start: QP={qps}")
        result = _run("mmgi0", argv, timeout=620)
        self.status["reconnects"].append(
            {
                "qps": qps,
                "exit": result.returncode,
                "output": result.stdout + result.stderr,
            }
        )
        self._save()
        if result.returncode:
            message = f"QP={qps} reconnect failed: {result.stdout} {result.stderr}"
            if any(
                phrase in message
                for phrase in (
                    "md0 members differ",
                    "no md array matches",
                    "failed to remount",
                    "failed to assemble",
                )
            ):
                raise StorageRecoveryRequired(message)
            raise RuntimeError(message)
        self.verify_qps(qps)
        self._storage_healthy()
        self._corpus("minimax")
        self._corpus("mixtral")
        self.log(f"reconnect complete: QP={qps}")

    def _prior_count(self) -> tuple[int, float]:
        count = 0
        latest = 0.0
        for host in HOSTS:
            script = """\
import json
from pathlib import Path
files = list(Path("/root").glob("if-fleet-20260924-2329-qps*-rep*-if*-mmgi?.json"))
print(json.dumps({"count": len(files),
                  "latest": max((path.stat().st_mtime for path in files), default=0)}))
"""
            result = _run(host, ["python3", "-c", script], timeout=15)
            if result.returncode:
                raise RuntimeError(
                    f"cannot inspect prior run on {host}: {result.stderr}"
                )
            row = json.loads(result.stdout)
            count += row["count"]
            latest = max(latest, row["latest"])
        return count, latest

    def _prior_bench_active(self) -> bool:
        for host in HOSTS:
            result = _run(
                host,
                ["bash", "-c", "pgrep -af '[l]mcache.cli.main bench l2' || true"],
                timeout=15,
            )
            if IF_FLEET in result.stdout:
                return True
        return False

    def wait_for_prior(self) -> None:
        """Wait for the laptop run, or record its confirmed dead or hung state."""
        started = time.monotonic()
        while True:
            count, latest = self._prior_count()
            active = self._prior_bench_active()
            self.status["prior_run"] = {
                "host_result_count": count,
                "latest_mtime": latest,
                "active": active,
            }
            self._save()
            if count >= 108 and not active:
                break
            age = time.time() - (latest or self.config["prior_run_start_epoch"])
            if age >= 900 and active:
                self.status["state"] = "prior_run_hung_wait_human"
                self._save()
                self.log(
                    "prior run hung: no new result for 15m with bench still active"
                )
                raise RuntimeError("prior run hung; human intervention required")
            if age >= 900 and not active:
                self.log(f"prior run dead or incomplete: {count}/108 host files")
                break
            self.log(f"waiting for prior run: {count}/108 host files, active={active}")
            time.sleep(60)
        self.status["prior_run"]["wait_seconds"] = round(time.monotonic() - started, 1)
        self._save()

    def _stop_requested(self) -> bool:
        cutoff = datetime.fromisoformat(self.status["cutoff_time"])
        return self.stop_path.exists() or datetime.now().astimezone() >= cutoff

    def _canary_rdma_advisory(self, cell: tuple[str, str, int, int, int]) -> None:
        output = self.root / f"{self._cell_id(cell)}-rdma-advisory.json"
        argv = [
            "python3",
            self.config["rdma_check_script"],
            "--cells",
            self._cell_id(cell),
            "--dashboard",
            self.config["dashboard_file"],
            "--json-output",
            str(output),
        ]
        try:
            result = _run("mmgi0", argv, timeout=150)
            if result.returncode:
                raise RuntimeError(
                    f"exit={result.returncode}: {result.stdout}{result.stderr}"
                )
            advisory = json.loads(output.read_text())
            classification = advisory["classification"]
        except (
            subprocess.TimeoutExpired,
            RuntimeError,
            OSError,
            ValueError,
            KeyError,
            TypeError,
        ) as exc:
            self.status["canary_rdma"] = {
                "classification": "unverified",
                "error": f"{type(exc).__name__}: {exc}",
            }
            self.log(f"canary RDMA advisory could not complete: {exc}")
        else:
            self.status["canary_rdma"] = advisory
            self.log("canary RDMA advisory: " + classification)
        self._save()

    def _restore(self) -> None:
        self.restore_attempted = True
        self.status["current_block"] = "D"
        self._save()
        try:
            self.verify_qps(128)
        except RuntimeError:
            self.reconnect(128)
        self.verify_qps(128)
        self._storage_healthy()
        self._corpus("minimax")
        self._corpus("mixtral")
        self.status["final_qps"] = 128
        self._save()

    def execute(self) -> None:
        """Run canary and A-C in order, then restore QP 128 in block D."""
        self.wait_for_prior()
        self._no_io()
        self._storage_healthy()
        self._code_provenance()
        try:
            self.verify_qps(128)
        except RuntimeError:
            self.reconnect(128)
        self._corpus("minimax")
        self._corpus("mixtral")
        self.matrix_started = True
        if self._stop_requested():
            self.log("STOP or 07:00 cutoff reached before canary")
            self._restore()
            self.status["state"] = "stopped_before_canary"
            self._save()
            return
        canary = ("canary", "minimax", 128, 2, 1)
        if not self._completed(canary) and not self.run_cell(canary):
            self.status["state"] = "canary_failed"
            self._restore()
            self._save()
            return
        self._canary_rdma_advisory(canary)
        self.log("canary complete")
        consecutive_failures = 0
        for block, model, qps, in_flights in BLOCKS:
            pending = [
                cell
                for cell in _cells(block, model, qps, in_flights)
                if not self._completed(cell)
            ]
            if not pending:
                self.log(f"resume skip complete block {block}")
                continue
            if self._stop_requested():
                self.log("stop requested before a new cell")
                break
            if qps == 1:
                try:
                    self.reconnect(1)
                except StorageRecoveryRequired:
                    raise
                except RuntimeError as exc:
                    self.status["skipped_block_C"] = str(exc)
                    self._save()
                    self.log(f"skip block C: {exc}")
                    break
            else:
                self.verify_qps(128)
            for cell in _cells(block, model, qps, in_flights):
                if self._stop_requested():
                    self.log("STOP or 07:00 cutoff reached")
                    break
                if self.run_cell(cell):
                    consecutive_failures = 0
                else:
                    consecutive_failures += 1
                    if consecutive_failures >= 2:
                        self.status["state"] = "two_consecutive_failed_cells"
                        self._save()
                        self.log("two consecutive failed cells; restoring baseline")
                        break
            if self._stop_requested() or consecutive_failures >= 2:
                break
        self._restore()
        self.status["state"] = (
            "complete" if not self.status["failures"] else "complete_with_failures"
        )
        self.status["end_time"] = datetime.now().astimezone().isoformat()
        self._save()


def main() -> None:
    """Load the reviewed config and run its detached controller."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    if not re.fullmatch(r"[0-9]{8}-[0-9]{4}", config["run_id"]):
        parser.error("run_id must be YYYYMMDD-HHMM")
    runner = Overnight(config)
    try:
        runner.execute()
    except Exception as exc:
        if (
            runner.matrix_started
            and not runner.restore_attempted
            and not isinstance(exc, StorageRecoveryRequired)
        ):
            try:
                runner._restore()
            except Exception as restore_exc:
                runner.status["restore_error"] = str(restore_exc)
        runner.status["state"] = "stopped_for_human"
        runner.status["fatal_error"] = str(exc)
        runner._save()
        runner.log(f"stopped for human: {exc}")
        raise


if __name__ == "__main__":
    main()
