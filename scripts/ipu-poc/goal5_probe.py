# SPDX-License-Identifier: Apache-2.0
"""Read-only Goal5 probes and a bounded, independently owned benchmark process."""

# Future
from __future__ import annotations

# Standard
from pathlib import Path
from typing import Any
import fcntl
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import time

# Third Party
from setup_initiator_namespace_6x8 import private_members, resolve_members
from setup_mmgt_namespace_6x8 import namespace_device, validate_manifest

BENCH = "lmcache/cli/commands/bench/l2_adapter_bench"
DEPLOY = Path(__file__).resolve().parent
FILES = [
    f"{BENCH}/{name}.py"
    for name in ("command", "runner", "metrics", "result", "geometry", "data")
] + [
    "lmcache/v1/distributed/l2_adapters/fs_native_l2_adapter.py",
    "lmcache/v1/distributed/l2_adapters/fs_l2_adapter.py",
    "lmcache/v1/distributed/l2_adapters/native_connector_l2_adapter.py",
]


def command(args: list[str], cwd: str | None = None) -> str:
    """Read one command's stdout, raising on command failure."""
    return subprocess.check_output(
        args, text=True, cwd=cwd, stderr=subprocess.PIPE
    ).strip()


def read(path: Path) -> str:
    """Read a required kernel state field."""
    return path.read_text().strip()


def unit(name: str, verb: str) -> bool:
    """Report active/enabled status without changing a unit."""
    return (
        subprocess.run(
            ["systemctl", verb, "--quiet", name], capture_output=True
        ).returncode
        == 0
    )


def provenance(repo: str, python: str, profile: str, backend: str) -> dict[str, Any]:
    """Hash source, deployed helpers and artifacts resolved by the benchmark Python.

    Args:
        repo: Benchmark checkout directory.
        python: Benchmark interpreter path.
        profile: Profile path relative to the checkout.
        backend: Selected filesystem or raw engine.

    Returns:
        Source revision, hashes, and resolved runtime artifact paths.

    Raises:
        subprocess.CalledProcessError: A runtime artifact is absent or a probe fails.
        OSError: A required source or helper cannot be read.
    """
    files = FILES + [profile]
    extension = "lmcache.lmcache_fs"
    if backend != "xfs":
        files += [
            "lmcache/v1/distributed/l2_adapters/raw_block_l2_adapter.py",
            "lmcache/v1/storage_backend/raw_block/core.py",
            "lmcache/v1/storage_backend/raw_block/key_codec.py",
        ]
        extension = "lmcache_rust_raw_block_io"
    modules = [name[:-3].replace("/", ".") for name in files if name != profile] + [
        extension
    ]
    script = f"""
import hashlib, importlib.util, json
from pathlib import Path
artifacts = {{}}
for name in {modules!r}:
    spec = importlib.util.find_spec(name)
    if spec is None or spec.origin is None:
        raise RuntimeError(f"Required runtime artifact absent: {{name}}")
    path = Path(spec.origin).resolve()
    artifacts[name] = {{
        "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()
    }}
print(json.dumps(artifacts))
"""
    return {
        "git_sha": command(["git", "rev-parse", "HEAD"], repo),
        "files": {
            name: hashlib.sha256((Path(repo) / name).read_bytes()).hexdigest()
            for name in files
        },
        "helpers": {
            name: hashlib.sha256((DEPLOY / name).read_bytes()).hexdigest()
            for name in (
                "goal5_overnight.py",
                "goal5_probe.py",
                "setup_initiator_namespace_6x8.py",
                "setup_mmgt_namespace_6x8.py",
            )
        },
        "runtime": json.loads(command([python, "-c", script], repo)),
    }


def inspect(cfg: dict[str, Any]) -> dict[str, Any]:
    """Capture private ownership, array, CPU, source, mount and raw readiness."""
    host_nqn = read(Path("/etc/nvme/hostnqn"))
    host = next(
        h for h, r in cfg["manifest"]["hosts"].items() if r["host_nqn"] == host_nqn
    )
    expected = cfg["hosts"][host]
    members = private_members(cfg["manifest"], host)
    devices = resolve_members(members, Path("/sys/class/block"))
    controllers = []
    addresses = []
    for path in Path("/sys/class/nvme").glob("nvme[0-9]*"):
        if read(path / "subsysnqn") in {m["nqn"] for m in members}:
            controllers.append(
                {
                    "state": read(path / "state"),
                    "queue_count": int(read(path / "queue_count")),
                    "sqsize": int(read(path / "sqsize")),
                }
            )
            addresses.append(read(path / "address"))
    numa = set()
    for sub in cfg["manifest"]["hosts"][host]["subsystems"]:
        if not any(f"traddr={sub['address']}," in a + "," for a in addresses):
            raise ValueError("Controller address differs from private subsystem")
        route = json.loads(command(["ip", "-j", "route", "get", sub["address"]]))[0]
        numa.add(int(read(Path(f"/sys/class/net/{route['dev']}/device/numa_node"))))
    if len(numa) != 1:
        raise ValueError("Host fabric NUMA nodes differ")
    md = Path("/sys/class/block/md0/md")
    md_members = [
        read(Path("/sys/class/block") / (p / "block").resolve().name / "uuid")
        for p in md.glob("dev-*")
    ]
    mounted = subprocess.run(
        ["findmnt", "-J", "-S", "/dev/md0", "-o", "SOURCE,TARGET,FSTYPE,OPTIONS,UUID"],
        capture_output=True,
        text=True,
    )
    mounts = (
        json.loads(mounted.stdout)["filesystems"] if mounted.returncode == 0 else []
    )
    saved = json.loads(
        Path(f"/root/lmcache-namespace-6x8-20261018/{host}-array.json").read_text()
    )
    mount = None
    if mounts:
        if (
            len(mounts) != 1
            or mounts[0]["target"] != "/mnt/lmcache"
            or mounts[0]["fstype"] != "xfs"
        ):
            raise ValueError("Unexpected array mount")
        mount = {
            "source": mounts[0]["source"],
            "uuid": mounts[0]["uuid"],
            "noatime": "noatime" in mounts[0]["options"].split(","),
        }
        cache = Path(f"/mnt/lmcache/{host}/goal5/{cfg['run_id']}")
        if cache.resolve() != cache or any(
            p.is_symlink() for p in (cache, *cache.parents)
        ):
            raise ValueError("Cache path contains a symlink")
    repo = expected["repo"]
    artifacts = provenance(repo, expected["python"], cfg["profile"], cfg["backend"])
    probe = (
        "import json,importlib.util; "
        "from lmcache.cli.commands.bench.l2_adapter_bench.geometry "
        "import resolve_submit_geometry; "
        f"g=resolve_submit_geometry({cfg['profile']!r}); "
        "print(json.dumps({'geometry':{'profile_sha256':g.sha256,"
        "'objects_per_submit':g.objects_per_submit,"
        "'max_object_bytes':max(g.object_sizes_bytes),'task_size_bytes':g.task_size_bytes},"
        "'rust_available':importlib.util.find_spec('lmcache_rust_raw_block_io') "
        "is not None}))"
    )
    extra = json.loads(command([expected["python"], "-c", probe], repo))
    active = []
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            argv = (proc / "cmdline").read_bytes().split(b"\0")
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if (
            (b"lmcache.cli.main" in argv and b"bench" in argv)
            or (argv and Path(os.fsdecode(argv[0])).name == "fio")
            or any(
                Path(os.fsdecode(a)).name
                in (
                    "goal4_overnight.py",
                    "goal442222_overnight.py",
                    "goal6x2_overnight.py",
                )
                for a in argv[:3]
            )
        ):
            active.append(proc.name)
    busy = False
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", expected["metrics_port"]))
        except OSError:
            busy = True
    return {
        "namespace_uuids": [m["uuid"] for m in members],
        "devices": devices,
        "controllers": controllers,
        "md_members": md_members,
        "raid_disks": int(read(md / "raid_disks")),
        "chunk_bytes": int(read(md / "chunk_size")),
        "md_level": read(md / "level"),
        "md_state": read(md / "array_state"),
        "numa": numa.pop(),
        "governors": sorted(
            {
                read(p)
                for p in Path("/sys/devices/system/cpu").glob(
                    "cpu[0-9]*/cpufreq/scaling_governor"
                )
            }
        ),
        "min_perf_pct": int(
            read(Path("/sys/devices/system/cpu/intel_pstate/min_perf_pct"))
        ),
        "epb": sorted(
            {
                read(p)
                for p in Path("/sys/devices/system/cpu").glob(
                    "cpu[0-9]*/power/energy_perf_bias"
                )
            }
        ),
        "ntp_synchronized": command(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"]
        )
        == "yes",
        "provenance": artifacts,
        "git_dirty": command(
            ["git", "status", "--porcelain", "--untracked-files=no"], repo
        ),
        "kernel": command(["uname", "-r"]),
        "mount": mount,
        "filesystem_uuid": saved["filesystem_uuid"],
        "restore_enabled": unit("lmcache-ns6x8.service", "is-enabled"),
        "restore_active": unit("lmcache-ns6x8.service", "is-active"),
        "io_uring_disabled": int(read(Path("/proc/sys/kernel/io_uring_disabled"))),
        "active_io": active,
        "metrics_busy": busy,
        "results_free_bytes": (
            os.statvfs(DEPLOY).f_bavail * os.statvfs(DEPLOY).f_frsize
        ),
        **extra,
    }


def target(cfg: dict[str, Any]) -> dict[str, Any]:
    """Verify all 48 native namespace backings, private ACLs and NUMA placement."""
    manifest = cfg["manifest"]
    validate_manifest(manifest)
    base = Path("/sys/kernel/config/nvmet")
    checks = [unit("lmcache-ns6x8-target.service", "is-active")]
    wanted = {s["nqn"] for h in manifest["hosts"].values() for s in h["subsystems"]}
    checks.append({p.name for p in (base / "subsystems").iterdir()} == wanted)
    for host, h in manifest["hosts"].items():
        for sub in h["subsystems"]:
            path = base / "subsystems" / sub["nqn"]
            checks.extend(
                [
                    read(path / "attr_allow_any_host") == "0",
                    {p.name for p in (path / "allowed_hosts").iterdir()}
                    == {h["host_nqn"]},
                    {p.name for p in (path / "namespaces").iterdir()}
                    == {str(m["nsid"]) for m in sub["members"]},
                    (
                        base / "ports" / str(sub["port"]) / "subsystems" / sub["nqn"]
                    ).is_dir(),
                    read(base / "ports" / str(sub["port"]) / "addr_traddr")
                    == sub["address"],
                ]
            )
            for member in private_members(manifest, host):
                if member["nqn"] != sub["nqn"]:
                    continue
                drive = next(
                    d for d in manifest["drives"] if d["serial"] == member["serial"]
                )
                controller = Path("/sys/class/nvme") / Path(drive["controller"]).name
                if read(controller / "serial") != drive["serial"]:
                    raise ValueError("Target controller serial changed")
                device = namespace_device(controller, drive["namespace_ids"][host])
                ns = path / "namespaces" / str(member["nsid"])
                checks.extend(
                    [
                        read(ns / "enable") == "1",
                        read(ns / "device_path") == str(device),
                        read(ns / "device_uuid") == member["uuid"],
                        int(read(controller / "device/numa_node")) == sub["numa"],
                    ]
                )
    return {"ready": all(checks), "checks": len(checks), "target": "kernel"}


def stop_owned(pid: int, token: str) -> bool:
    """Stop a token-owned group; refuse reused or foreign PIDs."""
    sent = False
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        if f"GOAL5_TOKEN={token}".encode() not in env or os.getpgid(pid) != pid:
            raise ValueError(f"PID {pid} is not owned by this configuration")
        os.killpg(pid, signal.SIGTERM)
        sent = True
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            # The wrapper reaps the leader. A zombie has already stopped executing.
            status = read(Path(f"/proc/{pid}/stat")).rsplit(")", 1)[1].split()[0]
            if status == "Z":
                break
            time.sleep(0.1)
        else:
            env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
            if f"GOAL5_TOKEN={token}".encode() not in env or os.getpgid(pid) != pid:
                raise ValueError(f"PID {pid} changed ownership during cleanup")
            os.killpg(pid, signal.SIGKILL)
        return True
    except (FileNotFoundError, ProcessLookupError):
        return sent


def cleanup(cfg: dict[str, Any]) -> list[int]:
    """Stop this run's saved groups without deleting corpus or result artifacts."""
    token = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode()).hexdigest()
    stopped = []
    directory = DEPLOY / "host-results" / cfg["run_id"]
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "launch.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        (directory / "STOP").touch()
        records = list(directory.glob("*.pid.json"))
    for record in records:
        pid = json.loads(record.read_text())["pid"]
        if stop_owned(pid, token):
            stopped.append(pid)
    return stopped


def bench(request: dict[str, Any]) -> dict[str, Any]:
    """Launch one bounded process group, retaining logs and original benchmark JSON."""
    cfg = request["cfg"]
    host = request["host"]
    directory = DEPLOY / "host-results" / cfg["run_id"]
    directory.mkdir(parents=True, exist_ok=True)
    prefix = directory / request["artifact"]
    output = prefix.with_suffix(".bench.json")
    argv = request["command"] + ["--output", str(output)]
    with (directory / "launch.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (directory / "STOP").exists():
            raise ValueError("Run stopped; refusing delayed benchmark launch")
        if output.exists() or prefix.with_suffix(".log").exists():
            raise ValueError("Host artifact already exists")
        with prefix.with_suffix(".log").open("w") as log:
            process = subprocess.Popen(
                argv,
                cwd=cfg["hosts"][host]["repo"],
                stdout=log,
                stderr=log,
                start_new_session=True,
                env={**os.environ, "GOAL5_TOKEN": request["config_sha256"]},
            )
            prefix.with_suffix(".pid.json").write_text(json.dumps({"pid": process.pid}))
    try:
        process.wait(timeout=request["timeout"])
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    row = {
        "config_sha256": request["config_sha256"],
        "host": host,
        "cell": request["cell"],
        "command": request["command"],
        "exit_code": process.returncode,
        "completed_at": time.time(),
        "result": json.loads(output.read_text()) if output.exists() else None,
        "host_artifacts": str(prefix),
    }
    prefix.with_suffix(".json").write_text(json.dumps(row, indent=2))
    return row


def main() -> None:
    """Read one JSON request from stdin and return one JSON response."""
    request = json.load(sys.stdin)
    action = request["action"]
    if action == "bench":
        with Path("/run/lock/lmcache-goal5-md0.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result: Any = bench(request)
    elif action == "inspect":
        result = inspect(request["cfg"])
    elif action == "target":
        result = target(request["cfg"])
    elif action == "cleanup":
        result = cleanup(request["cfg"])
    else:
        raise ValueError("Unknown action")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
