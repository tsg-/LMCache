# SPDX-License-Identifier: Apache-2.0
"""Persist restricted mmgi1 io_uring only after diagnostic and libaio gates."""

# Future
from __future__ import annotations

# Standard
from pathlib import Path
from typing import Any
import argparse
import copy
import fcntl
import hashlib
import json
import signal
import statistics
import subprocess
import time

# Third Party
from goal5_fio import summarize_fio
from goal5_onehost_diagnostics import commands, sample_interval, validate_load
from goal5_overnight import DEPLOY, config_digest, validate_host, write_json
from goal5_probe import bench, inspect

POLICY = Path("/proc/sys/kernel/io_uring_disabled")
GROUP = Path("/proc/sys/kernel/io_uring_group")
PERSISTENT = Path("/etc/sysctl.d/99-zz-lmcache-io-uring.conf")
PROBE = """
from pathlib import Path
import ctypes,ctypes.util,json,os
assert os.uname().machine == 'x86_64'
aio=ctypes.CDLL(ctypes.util.find_library('aio'))
aio.io_setup.argtypes=[ctypes.c_uint,ctypes.POINTER(ctypes.c_ulong)]
aio.io_setup.restype=ctypes.c_int
aio.io_destroy.argtypes=[ctypes.c_ulong]
aio.io_destroy.restype=ctypes.c_int
ctx=ctypes.c_ulong(0)
setup=aio.io_setup(64,ctypes.byref(ctx))
destroy=aio.io_destroy(ctx.value) if setup==0 else None
libc=ctypes.CDLL(None,use_errno=True)
libc.syscall.restype=ctypes.c_long
# x86_64 syscall 425; zeroed io_uring_params is 120 bytes in this ABI.
params=ctypes.create_string_buffer(256)
fd=libc.syscall(ctypes.c_long(425),ctypes.c_uint(32),
                ctypes.c_void_p(ctypes.addressof(params)))
error=ctypes.get_errno() if fd<0 else 0
if fd>=0:os.close(fd)
print(json.dumps({'aio_setup_rc':setup,'aio_destroy_rc':destroy,
                 'uring_setup_rc':int(fd),'uring_errno':error,
                 'uid':os.geteuid(),'io_uring_disabled':
                 Path('/proc/sys/kernel/io_uring_disabled').read_text().strip(),
                 'io_uring_group':
                 Path('/proc/sys/kernel/io_uring_group').read_text().strip()}))
"""


def diagnostic_gate(cfg: dict[str, Any], directory: Path) -> list[float]:
    """Revalidate the complete bound matrix; return the two libaio QD32 rates."""
    status = json.loads((directory / "status.json").read_text())
    if status.get("state") != "complete" or status.get("cells") != 12:
        raise ValueError("Completed diagnostics required")
    summaries = json.loads((directory / "summary.json").read_text())
    expected = [c["identity"] for c in cfg["diagnostic_cells"]]
    if (
        len(expected) != 12
        or len(set(expected)) != 12
        or [r["cell"]["identity"] for r in summaries] != expected
    ):
        raise ValueError("Diagnostic cell identities differ")
    rates = []
    processes = 0
    for index, saved in enumerate(summaries):
        cell = cfg["diagnostic_cells"][index]
        identity = cell["identity"]
        rows = json.loads((directory / f"{identity}-hosts.json").read_text())
        argv_list = commands(cfg, cell)
        if len(rows) != len(argv_list):
            raise ValueError("Diagnostic process evidence missing")
        samples = [
            json.loads(line)
            for line in (directory / f"{identity}-samples.jsonl")
            .read_text()
            .splitlines()
        ]
        total = 0.0
        for i, row in enumerate(rows):
            if (
                row["exit_code"]
                or row["command"] != argv_list[i]
                or row["config_sha256"] != config_digest(cfg)
            ):
                raise ValueError("Diagnostic command, exit, or binding differs")
            if cell["kind"] == "fio":
                result = summarize_fio(row["result"], "read")
                end = row["result"]["timestamp_ms"] / 1000
                start = end - result["read"]["runtime_ms"] / 1000
                if cell["depth"] == 32:
                    rates.append(result["payload_gbps_decimal"])
            else:
                result = validate_load(row, cfg, argv_list[i])
                start, end = result["start"], result["end"]
            sample_interval(
                [s for s in samples if f"{identity}-p{i}" in s["processes"]],
                start,
                end,
            )
            total += result["payload_gbps_decimal"]
            processes += 1
        if abs(total - saved["payload_gbps_decimal"]) > 1e-8:
            raise ValueError("Diagnostic saved payload differs")
    if processes != 18 or len(rates) != 2:
        raise ValueError("Diagnostic matrix process count differs")
    return rates


def check_control_rates(before: list[float], after: list[float]) -> float:
    """Flag a >10% median control regression; two runs cannot prove equivalence."""
    if len(before) != 2 or len(after) != 2 or min(before + after) <= 0:
        raise ValueError("Exactly two positive control rates required")
    ratio = statistics.median(after) / statistics.median(before)
    if ratio < 0.9:
        raise ValueError(f"Clear libaio control regression: ratio={ratio:.6f}")
    return ratio


def probe(unprivileged: bool = False) -> dict[str, Any]:
    """Test independent AIO context and io_uring creation without issuing I/O."""
    argv = ["python3", "-c", PROBE]
    if unprivileged:
        argv = [
            "setpriv",
            "--reuid=65534",
            "--regid=65534",
            "--clear-groups",
            "--bounding-set=-all",
        ] + argv
    result = subprocess.run(
        argv, capture_output=True, text=True, check=True, timeout=30
    )
    return json.loads(result.stdout)


def apply(cfg: dict[str, Any]) -> None:
    """Change only mmgi1 policy, validate compatibility, then persist or roll back."""
    baseline = diagnostic_gate(cfg, Path(cfg["results_dir"]))
    output = Path("/home/goal5-overnight/async-fs-pilot-20261019/policy-readiness")
    if output.exists() or PERSISTENT.exists():
        raise ValueError("Readiness results or persistent policy already exist")
    if POLICY.read_text().strip() != "2" or GROUP.read_text().strip() != "-1":
        raise ValueError("Initial io_uring policy differs")
    output.mkdir()
    receipt: dict[str, Any] = {
        "authorization": "Human approved permanent restricted mmgi1 change at "
        "10:42 MST 2026-10-19, conditional on standard AIO compatibility.",
        "diagnostic_config_sha256": config_digest(cfg),
        "readiness_script_sha256": hashlib.sha256(
            Path(__file__).read_bytes()
        ).hexdigest(),
        "before_control_gbps": baseline,
        "started_at": time.time(),
        "persistent_file": str(PERSISTENT),
    }
    committed = False

    def interrupt(signum: int, _frame: Any) -> None:
        raise SystemExit(128 + signum)

    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, interrupt)
    with Path("/run/lock/lmcache-goal5-md0.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            readiness = inspect(cfg)
            write_json(output / "preflight.json", readiness)
            validate_host(readiness, cfg, "mmgi1")
            receipt["before"] = probe()
            if (
                receipt["before"]["aio_setup_rc"]
                or receipt["before"]["aio_destroy_rc"]
                or receipt["before"]["uring_errno"] != 1
            ):
                raise ValueError("Before-change syscall behavior differs")
            POLICY.write_text("1\n")
            receipt["after_root"] = probe()
            receipt["after_unprivileged"] = probe(True)
            for row in (receipt["after_root"], receipt["after_unprivileged"]):
                if (
                    row["aio_setup_rc"]
                    or row["aio_destroy_rc"]
                    or row["io_uring_disabled"] != "1"
                    or row["io_uring_group"] != "-1"
                ):
                    raise ValueError("After-change AIO or group policy differs")
            if (
                receipt["after_root"]["uring_setup_rc"] < 0
                or receipt["after_unprivileged"]["uring_errno"] != 1
            ):
                raise ValueError("Restricted ring creation behavior differs")
            control = copy.deepcopy(cfg)
            control["run_id"] += "-aio-policy"
            rows = []
            for repetition in (1, 2):
                cell = {"kind": "fio", "depth": 32}
                row = bench(
                    {
                        "cfg": control,
                        "host": "mmgi1",
                        "cell": {"mode": "fio-read", "repetition": repetition},
                        "command": commands(control, cell)[0],
                        "artifact": f"aio-policy-qd32-r{repetition}",
                        "config_sha256": config_digest(control),
                        "timeout": 750,
                    }
                )
                write_json(output / f"libaio-r{repetition}.json", row)
                if row["exit_code"]:
                    raise ValueError("Post-change libaio control failed")
                if row["result"]["jobs"][0]["iodepth_level"]["32"] < 99:
                    raise ValueError("Post-change libaio achieved depth differs")
                rows.append(summarize_fio(row["result"], "read"))
            rates = [r["payload_gbps_decimal"] for r in rows]
            receipt["after_control_gbps"] = rates
            receipt["median_rate_ratio"] = check_control_rates(baseline, rates)
            receipt["comparison_scope"] = (
                "Two matching libaio read controls, not statistical equivalence; "
                "10% regression threshold is a conservative review trigger."
            )
            if POLICY.read_text().strip() != "1" or GROUP.read_text().strip() != "-1":
                raise ValueError("Live policy changed during the control")
            with PERSISTENT.open("x") as stream:
                stream.write(
                    "# Restricted io_uring for the authorized LMCache MMG pilot.\n"
                    "kernel.io_uring_disabled = 1\n"
                    "kernel.io_uring_group = -1\n"
                )
            committed = True
            receipt["state"] = "complete"
            receipt["reboot_tested"] = False
        except BaseException as error:
            receipt.update(state="failed", error=str(error))
            raise
        finally:
            if not committed:
                POLICY.write_text("2\n")
            receipt["live_io_uring_disabled"] = POLICY.read_text().strip()
            write_json(output / "receipt.json", receipt)


def main() -> None:
    """Wait and coordinate on mmgi0, or apply the guarded policy on mmgi1."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--coordinator", action="store_true")
    parser.add_argument("--apply-authorized-policy", action="store_true", required=True)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    if any(
        hashlib.sha256(Path(path).read_bytes()).hexdigest() != expected
        for path, expected in cfg["fio_artifacts"].items()
    ) or (
        hashlib.sha256(
            Path(f"{DEPLOY}/goal5_onehost_diagnostics.py").read_bytes()
        ).hexdigest()
        != cfg["driver_sha256"]
    ):
        raise ValueError("Bound diagnostic or FIO control source differs")
    host = "mmgi0" if args.coordinator else "mmgi1"
    if (
        Path("/etc/nvme/hostnqn").read_text().strip()
        != cfg["manifest"]["hosts"][host]["host_nqn"]
    ):
        raise ValueError(f"Execute only on {host}")
    if args.coordinator:
        identity = "-".join(cfg["run_id"].split("-")[1:3])
        unit = f"goal5-mmgi1-diagnostics-{identity}.service"
        deadline = time.monotonic() + 3600
        while True:
            state = subprocess.check_output(
                ["systemctl", "show", unit, "-p", "ActiveState", "-p", "Result"],
                text=True,
            )
            fields = dict(line.split("=", 1) for line in state.splitlines())
            if fields["ActiveState"] not in ("active", "activating", "deactivating"):
                if fields["ActiveState"] != "inactive" or fields["Result"] != "success":
                    raise ValueError("Diagnostic unit did not finish successfully")
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Diagnostic wait exceeded one hour")
            time.sleep(10)
        with (Path(DEPLOY) / "fleet.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "mmgi1",
                    "python3",
                    f"{DEPLOY}/goal5_uring_readiness.py",
                    "--config",
                    str(args.config),
                    "--apply-authorized-policy",
                ],
                check=True,
                timeout=1800,
            )
        return
    apply(cfg)


if __name__ == "__main__":
    main()
