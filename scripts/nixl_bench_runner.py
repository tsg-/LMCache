#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Two-node NIXL/UCX benchmark runner for LMCache L1/L2 tier testing.

Drives the full benchmark matrix from a laptop/control node:
  - Starts lmcache coordinator on bmg0
  - Starts lmcache server on bmg0 (source) and bmg1 (puller/storage)
  - Runs ``lmcache bench server`` on bmg0 (populate) then bmg1 (pull)
  - Optionally uses per-run FSConnector isolation (bench_run.sh)
  - Produces a latency table: token_count × {cold_mean, warm_mean, p50, p99}

Usage::

    # L1-only baseline (both nodes, host DRAM → DRAM via NIXL/UCX):
    python scripts/nixl_bench_runner.py --test l1 \\
        --tokens 32 64 128 256 512

    # L2 single-NVMe baseline (FSConnector on bmg1 /mnt/p2p_ext4):
    python scripts/nixl_bench_runner.py --test l2_single \\
        --fs-paths /mnt/p2p_ext4 --tokens 32 64 128 256 512

    # L2 dual-NVMe striped (FSConnector across two mounts):
    python scripts/nixl_bench_runner.py --test l2_dual \\
        --fs-paths /mnt/p2p_ext4,/mnt/nvme5 --tokens 32 64 128 256 512

    # L1+L2 tiered (LocalCPU + FSConnector eviction):
    python scripts/nixl_bench_runner.py --test tiered \\
        --fs-paths /mnt/p2p_ext4 --l1-size-gb 2 --tokens 32 64 128 256 512

Topology::

    bmg0 (source)  --  192.168.200.3  --  mlx5_1 (RoCEv2, GID 3)
    bmg1 (puller)  --  192.168.200.4  --  mlx5_1 (RoCEv2, GID 3)
    Cross-wire: bmg0:mlx5_1 ↔ bmg1:mlx5_1

SSH aliases ``bmg0`` / ``bmg1`` must resolve without a password (authorized_keys).
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Constants — match .claude/CLAUDE.md test-node config
# ---------------------------------------------------------------------------

REPO = "~/tsg/LMCache"
VENV = f"{REPO}/.venv-ipu/bin"

BMG0_IP = "192.168.200.3"
BMG1_IP = "192.168.200.4"

COORDINATOR_PORT = 9300
ZMQ_PORT = 5555
HTTP_PORT = 8080
NIXL_PORT = 5605

# pip-bundled nixl_cu12 libs/plugins (v1.3.1, already installed in venv)
_PIP_NIXL_LIBS = (
    f"{REPO}/.venv-ipu/lib/python3.12/site-packages"
    "/.nixl_cu12.mesonpy.libs"
)
_PIP_UCX_LIBS = (
    f"{REPO}/.venv-ipu/lib/python3.12/site-packages/nixl_cu12.libs"
)

# UCX device name differs by node: bmg0 uses mlx5_1:1, bmg1 uses rocep153s0f1:1
# (same physical NIC/port, different kernel naming convention on each machine)
BMG0_UCX_NET_DEV = "mlx5_1:1"
BMG1_UCX_NET_DEV = "rocep153s0f1:1"

_NO_PROXY = f"localhost,127.0.0.1,{BMG0_IP},{BMG1_IP}"

_UCX_ENV_COMMON = (
    "UCX_TLS=rc_mlx5,ud_mlx5,sm "  # rc_mlx5=RoCEv2 wire; ud_mlx5=intra-agent loopback; sm=intra-node
    "UCX_MEMTYPE_CACHE=n "
    "LMCACHE_RDMA_GID_INDEX=3 "
    "NIXL_NET_BACKEND=UCX "
    f"NIXL_PLUGIN_DIR={_PIP_NIXL_LIBS}/plugins "
    f"LD_LIBRARY_PATH={_PIP_NIXL_LIBS}:{_PIP_UCX_LIBS}:$LD_LIBRARY_PATH "
    f"no_proxy={_NO_PROXY} "
    f"NO_PROXY={_NO_PROXY}"
)

def ucx_env(host: str) -> str:
    """Return the UCX env string with the correct UCX_NET_DEVICES for *host*.

    Matches on bmg1 IP or alias — handles both 'bmg1' and 'dev@192.168.200.4'.
    """
    is_bmg1 = host == "bmg1" or BMG1_IP in host
    dev = BMG1_UCX_NET_DEV if is_bmg1 else BMG0_UCX_NET_DEV
    return f"UCX_NET_DEVICES={dev} " + _UCX_ENV_COMMON

# Sequence range: 100–105 = 5 requests per token count
SEQ_START = 100
SEQ_END = 105


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class RunResult:
    num_tokens: int
    cold_mean_ms: Optional[float] = None
    warm_mean_ms: Optional[float] = None
    p50_ms: Optional[float] = None
    p99_ms: Optional[float] = None
    error: Optional[str] = None


@dataclass
class BenchRun:
    test: str
    l1_size_gb: float
    fs_paths: list[str] = field(default_factory=list)
    results: list[RunResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------


def ssh_cmd(host: str, cmd: str, capture: bool = True) -> subprocess.Popen:
    """Launch *cmd* on *host* via SSH.  Returns an open Popen object.

    ControlMaster/ControlPath are disabled to force a fresh TCP connection
    per invocation.  SSH multiplexing reuse caused the sequential-token pull
    bench to hang on bmg1: after one token-size run's SSH exits, its control
    socket lingers in teardown and the next run's SSH stalls waiting on it.
    """
    return subprocess.Popen(
        ["ssh", "-o", "StrictHostKeyChecking=no",
         "-o", "ControlMaster=no", "-o", "ControlPath=none",
         host, cmd],
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        text=True,
        bufsize=1,
    )


def ssh_run(host: str, cmd: str, timeout: float = 10.0) -> tuple[int, str]:
    """Run *cmd* on *host* synchronously; return (returncode, combined_output)."""
    proc = ssh_cmd(host, cmd)
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out or ""
    except subprocess.TimeoutExpired:
        proc.kill()
        return -1, "TIMEOUT"


def kill_ports(host: str, *ports: int) -> None:
    """Kill any process listening on the given ports on *host*."""
    port_str = "/tcp ".join(str(p) for p in ports) + "/tcp"
    ssh_run(host, f"fuser -k {port_str} 2>/dev/null || true", timeout=5.0)


# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


def start_coordinator(host: str) -> subprocess.Popen:
    cmd = (
        f"cd {REPO} && "
        f"env {ucx_env(host)} "
        f"{VENV}/lmcache coordinator "
        f"--host 0.0.0.0 --port {COORDINATOR_PORT} "
        f"2>&1"
    )
    return ssh_cmd(host, cmd)


# ---------------------------------------------------------------------------
# Server launcher
# ---------------------------------------------------------------------------


def _l2_adapter_json(paths: list[str]) -> str:
    """Return one or two --l2-adapter flags for the given FSConnector paths."""
    if not paths:
        return ""
    flags = []
    for p in paths:
        spec = json.dumps({"type": "fs", "base_path": p})
        flags.append(f"--l2-adapter '{spec}'")
    return " ".join(flags)


def start_server(
    host: str,
    host_ip: str,
    coordinator_ip: str,
    l1_size_gb: float,
    eviction_policy: str,
    fs_paths: list[str],
    extra_env: str = "",
) -> subprocess.Popen:
    l2_flags = _l2_adapter_json(fs_paths)
    env = f"{ucx_env(host)} {extra_env}".strip()
    cmd = (
        f"cd {REPO} && "
        f"env {env} "
        f"{VENV}/lmcache server "
        f"--host 0.0.0.0 --port {ZMQ_PORT} "
        f"--supported-transfer-mode lmcache_driven "
        f"--chunk-size 16 "
        f"--l1-size-gb {l1_size_gb} "
        f"--eviction-policy {eviction_policy} "
        f"--http-port {HTTP_PORT} "
        f"--coordinator-url http://{coordinator_ip}:{COORDINATOR_PORT} "
        f"--p2p-advertise-url nixl://{host_ip}:{NIXL_PORT} "
        f"--p2p-listen-url nixl://0.0.0.0:{NIXL_PORT} "
        f"{l2_flags} "
        f"2>&1"
    )
    return ssh_cmd(host, cmd)


# ---------------------------------------------------------------------------
# Output streaming + latency parsing
# ---------------------------------------------------------------------------

# lmcache bench output uses the CLI Metrics formatter.  Key lines (examples):
#   mean:                                            2.591
#   p50:                                             1.234
#   p99:                                             4.780
# Section headers identify which operation the latencies belong to:
#   -------------- Cold Lookup (ms) ---------------
#   -------------- Warm Retrieve (ms) -------------
_SECTION_RE = re.compile(r"-+\s*(?P<title>[^-]+?)\s*-+")
_KV_RE = re.compile(r"^(?P<label>\w[\w\s]*?):\s+(?P<value>[0-9]+(?:\.[0-9]+)?)$")


@dataclass
class _ParseState:
    """Mutable parse state shared across stream_and_collect."""
    current_section: str = ""
    cold_lookup: dict = field(default_factory=dict)
    warm_retrieve: dict = field(default_factory=dict)


def stream_and_collect(
    proc: subprocess.Popen,
    prefix: str,
    done_event: threading.Event,
    done_marker: str,
    state: _ParseState,
    verbose: bool,
) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        stripped = line.strip()
        if verbose:
            sys.stdout.write(f"[{prefix}] {line}")
            sys.stdout.flush()
        # Track current section header
        sm = _SECTION_RE.match(stripped)
        if sm:
            state.current_section = sm.group("title").strip()
            continue
        # Parse key: value lines in latency sections
        km = _KV_RE.match(stripped)
        if km:
            label = km.group("label").strip()
            value = float(km.group("value"))
            if "Cold Lookup" in state.current_section:
                state.cold_lookup[label] = value
            elif "Warm Retrieve" in state.current_section:
                state.warm_retrieve[label] = value
        if done_marker in line:
            done_event.set()
    done_event.set()  # EOF


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return float("nan")
    idx = max(0, int(len(sorted_vals) * pct / 100) - 1)
    return sorted_vals[idx]


# ---------------------------------------------------------------------------
# Run isolation via bench_run.sh
# ---------------------------------------------------------------------------


def create_run_dirs(host: str, label: str, paths: list[str]) -> list[str]:
    """Create isolated benchmark directories on a remote host.

    Args:
        host: SSH host on which to create the directories.
        label: Per-run directory label.
        paths: Base FSConnector paths to isolate.

    Returns:
        One isolated directory for each input base path.

    Raises:
        RuntimeError: If the helper fails or returns an incomplete path list.
    """
    if not paths:
        return []
    paths_csv = ",".join(paths)
    rc, out = ssh_run(
        host,
        (
            f"bash {REPO}/scripts/bench_run.sh "
            f"--label {shlex.quote(label)} --paths {shlex.quote(paths_csv)}"
        ),
        timeout=10.0,
    )
    if rc != 0:
        raise RuntimeError(f"bench_run.sh failed on {host}: {out.strip()}")
    run_dirs = [line.strip() for line in out.splitlines() if line.strip()]
    if len(run_dirs) != len(paths):
        raise RuntimeError(
            f"bench_run.sh returned {len(run_dirs)} paths for {len(paths)} base paths"
        )
    return run_dirs


def cleanup_run_dirs(host: str, run_dirs: list[str]) -> None:
    for d in run_dirs:
        ssh_run(host, f"rm -rf {d}", timeout=10.0)


def create_manifest(
    host: str,
    label: str,
    role: str,
    nic: str,
    numa_node: int,
) -> str:
    """Run the fail-closed provenance preflight on one benchmark host."""
    output = f"/tmp/lmcache_manifest_{label}_{role}.json"
    command = (
        f"cd {REPO} && env {ucx_env(host)} "
        f"{VENV}/python scripts/bench_manifest.py "
        f"--run-label {shlex.quote(label)} --role {shlex.quote(role)} "
        f"--numa-node {numa_node} --nic {shlex.quote(nic)} --gid-index 3 "
        "--require-env LMCACHE_RDMA_GID_INDEX "
        "--require-env NIXL_NET_BACKEND "
        f"--alloc-region l1_pool:4096 --output {shlex.quote(output)}"
    )
    rc, manifest = ssh_run(host, command, timeout=30.0)
    if rc != 0:
        raise RuntimeError(
            f"benchmark manifest failed on {host}; benchmark not started: "
            f"{manifest.strip()}"
        )
    return output


# ---------------------------------------------------------------------------
# Single-token bench run
# ---------------------------------------------------------------------------


def _bench_cmd(ssh_host: str, rpc_host: str, num_tokens: int, mode: str = "cpu") -> str:
    return (
        f"cd {REPO} && env {ucx_env(ssh_host)} "
        f"{VENV}/lmcache bench server "
        f"--rpc-url tcp://{rpc_host}:{ZMQ_PORT} "
        f"--url http://{rpc_host}:{HTTP_PORT} "
        f"--mode {mode} "
        f"--transfer-mode lmcache_driven "
        f"--num-tokens {num_tokens} "
        f"--start {SEQ_START} --end {SEQ_END} --interval 0 "
        f"2>&1"
    )


def run_one(
    server_host: str,
    client_host: str,
    num_tokens: int,
    timeout: float,
    verbose: bool,
) -> tuple[_ParseState, _ParseState]:
    """Run populate (bmg0) then pull (bmg1).

    Returns (populate_state, pull_state) each with parsed latency dicts.
    """
    # --- bmg0: populate (stores data into bmg0's L1) ---
    pop_state = _ParseState()
    pop_done = threading.Event()
    pop_proc = ssh_cmd(client_host, _bench_cmd(client_host, "127.0.0.1", num_tokens))
    pop_thread = threading.Thread(
        target=stream_and_collect,
        args=(pop_proc, f"POPULATE/{num_tokens}t", pop_done, "Done.", pop_state, verbose),
        daemon=True,
    )
    pop_thread.start()
    if not pop_done.wait(timeout=timeout):
        pop_proc.kill()
        raise TimeoutError(f"populate timed out for {num_tokens} tokens")
    pop_proc.wait(timeout=5)

    # --- bmg1: pull — no local copy, triggers P2P NIXL pull from bmg0 ---
    pull_state = _ParseState()
    pull_done = threading.Event()
    pull_proc = ssh_cmd(server_host, _bench_cmd(server_host, "127.0.0.1", num_tokens))
    pull_thread = threading.Thread(
        target=stream_and_collect,
        args=(pull_proc, f"PULL/{num_tokens}t", pull_done, "Done.", pull_state, verbose),
        daemon=True,
    )
    pull_thread.start()
    if not pull_done.wait(timeout=timeout):
        pull_proc.kill()
        raise TimeoutError(f"pull timed out for {num_tokens} tokens")
    pull_proc.wait(timeout=5)

    return pop_state, pull_state


# ---------------------------------------------------------------------------
# Main benchmark driver
# ---------------------------------------------------------------------------


def run_bench(args: argparse.Namespace) -> BenchRun:
    src_host = args.src_host   # bmg0 — coordinator + source server + bench populate
    dst_host = args.dst_host   # bmg1 — puller server + bench pull

    fs_paths = [p.strip() for p in args.fs_paths.split(",") if p.strip()] if args.fs_paths else []

    run = BenchRun(test=args.test, l1_size_gb=args.l1_size_gb, fs_paths=fs_paths)
    run_label = f"run_{int(time.time())}"

    # Preflight both hosts before changing port state or starting any process.
    print("==> Collecting fail-closed benchmark manifests...")
    source_manifest = create_manifest(
        src_host,
        run_label,
        "source",
        BMG0_UCX_NET_DEV.split(":")[0],
        args.src_numa_node,
    )
    storage_manifest = create_manifest(
        dst_host,
        run_label,
        "storage",
        BMG1_UCX_NET_DEV.split(":")[0],
        args.dst_numa_node,
    )
    print(f"    source manifest: {source_manifest}")
    print(f"    storage manifest: {storage_manifest}")

    # --- 1. Clear ports ---
    print("==> Clearing ports on both nodes...")
    for host in [src_host, dst_host]:
        kill_ports(host, COORDINATOR_PORT, ZMQ_PORT, HTTP_PORT, NIXL_PORT)
    time.sleep(1.0)

    # --- 2. Create run-isolation dirs on dst (NVMe tests only) ---
    run_dirs: list[str] = []
    effective_fs_paths: list[str] = fs_paths  # may be replaced with isolated subdirs

    if fs_paths:
        print(f"==> Creating run isolation dirs on {dst_host} (label={run_label})...")
        run_dirs = create_run_dirs(dst_host, run_label, fs_paths)
        effective_fs_paths = run_dirs
        print(f"    isolated paths: {run_dirs}")

    # --- 3. Start coordinator (bmg0) ---
    print(f"==> Starting coordinator on {src_host}:{COORDINATOR_PORT}...")
    coord_proc = start_coordinator(src_host)
    time.sleep(1.5)

    # --- 4. Start source server (bmg0, L1-only — just a P2P source) ---
    print(f"==> Starting source server on {src_host}...")
    src_proc = start_server(
        host=src_host,
        host_ip=BMG0_IP,
        coordinator_ip=BMG0_IP,
        l1_size_gb=args.l1_size_gb,
        eviction_policy="LRU",
        fs_paths=[],  # bmg0 is the source, no L2
    )

    # --- 5. Start destination server (bmg1, configured per test mode) ---
    print(f"==> Starting destination server on {dst_host} (test={args.test})...")
    dst_proc = start_server(
        host=dst_host,
        host_ip=BMG1_IP,
        coordinator_ip=BMG0_IP,
        l1_size_gb=args.l1_size_gb,
        eviction_policy="LRU",
        fs_paths=effective_fs_paths if args.test != "l1" else [],
    )

    # Poll ZMQ port on both nodes until ready (up to 60s).
    print("==> Waiting for servers to bootstrap (up to 60s)...")
    _bootstrap_timeout = 60.0
    _start = time.monotonic()
    for _host in [src_host, dst_host]:
        while time.monotonic() - _start < _bootstrap_timeout:
            rc, _ = ssh_run(_host, f"nc -z 127.0.0.1 {ZMQ_PORT}", timeout=3.0)
            if rc == 0:
                print(f"    {_host}:{ZMQ_PORT} ready")
                break
            time.sleep(1.0)
        else:
            print(f"WARNING: {_host}:{ZMQ_PORT} not ready after {_bootstrap_timeout}s")
    time.sleep(1.0)  # brief settle after last port comes up

    # --- 6. Run benchmark for each token count ---
    token_list: list[int] = sorted(args.tokens)
    print(f"==> Running benchmark for tokens: {token_list}")

    for num_tokens in token_list:
        print(f"\n--- {num_tokens} tokens ---")
        result = RunResult(num_tokens=num_tokens)
        try:
            _pop_state, pull_state = run_one(
                server_host=dst_host,
                client_host=src_host,
                num_tokens=num_tokens,
                timeout=args.timeout,
                verbose=args.verbose,
            )
            # Pull cold_lookup = first pass (NIXL wire transfer from bmg0)
            # Pull warm_retrieve = second pass (served from bmg1 local cache)
            cold = pull_state.cold_lookup
            warm = pull_state.warm_retrieve
            if cold:
                result.cold_mean_ms = cold.get("mean")
                result.p50_ms = cold.get("p50")
                result.p99_ms = cold.get("p99")
            if warm:
                result.warm_mean_ms = warm.get("mean")
                # override p50/p99 with warm if available (more meaningful for NVMe tests)
                if not cold:
                    result.p50_ms = warm.get("p50")
                    result.p99_ms = warm.get("p99")
            if not cold and not warm:
                result.error = "no latency sections found in bench output"
        except (TimeoutError, RuntimeError) as exc:
            result.error = str(exc)
            print(f"  ERROR: {exc}", file=sys.stderr)

        run.results.append(result)
        print(
            f"  cold_mean={result.cold_mean_ms}ms "
            f"p50={result.p50_ms}ms "
            f"p99={result.p99_ms}ms"
            + (f" ERROR={result.error}" if result.error else "")
        )

    # --- 7. Tear down ---
    print("\n==> Tearing down servers...")
    for proc in [src_proc, dst_proc, coord_proc]:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    for host in [src_host, dst_host]:
        kill_ports(host, COORDINATOR_PORT, ZMQ_PORT, HTTP_PORT, NIXL_PORT)

    # Optional cleanup of run-isolation dirs
    if run_dirs and args.cleanup:
        print(f"==> Cleaning up run dirs on {dst_host}...")
        cleanup_run_dirs(dst_host, run_dirs)

    return run


# ---------------------------------------------------------------------------
# Result table
# ---------------------------------------------------------------------------


def print_results(run: BenchRun) -> None:
    print(f"\n{'='*70}")
    print(f"Test: {run.test}  |  L1: {run.l1_size_gb}GB  |  FS paths: {run.fs_paths or '(none)'}")
    print(f"{'='*70}")
    header = f"{'Tokens':>8}  {'cold_mean(ms)':>15}  {'warm_mean(ms)':>15}  {'p50(ms)':>10}  {'p99(ms)':>10}"
    print(header)
    print("-" * len(header))
    for r in run.results:
        if r.error:
            print(f"{r.num_tokens:>8}  {'ERROR':>15}  {r.error}")
        else:
            wm = f"{r.warm_mean_ms:.3f}" if r.warm_mean_ms is not None else "n/a"
            print(
                f"{r.num_tokens:>8}  {r.cold_mean_ms or 'n/a':>15}  {wm:>15}  "
                f"{r.p50_ms or 'n/a':>10}  {r.p99_ms or 'n/a':>10}"
            )
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--test",
        choices=["l1", "l2_single", "l2_dual", "tiered"],
        default="l1",
        help="Test variant (default: l1)",
    )
    parser.add_argument(
        "--src-host", default="bmg0",
        help="SSH alias for source/coordinator node (default: bmg0)",
    )
    parser.add_argument(
        "--dst-host", default="dev@192.168.200.4",
        help=(
            "SSH target for puller/storage node; use direct fabric IP to avoid "
            "ProxyJump contention with the long-running server SSH session "
            "(default: dev@192.168.200.4)"
        ),
    )
    parser.add_argument("--src-numa-node", type=int, required=True)
    parser.add_argument("--dst-numa-node", type=int, required=True)
    parser.add_argument(
        "--tokens",
        type=int,
        nargs="+",
        default=[32, 64, 128, 256, 512],
        help="Token counts to benchmark (default: 32 64 128 256 512)",
    )
    parser.add_argument(
        "--l1-size-gb",
        type=float,
        default=8.0,
        help="L1 DRAM size in GB for each server (default: 8.0)",
    )
    parser.add_argument(
        "--fs-paths",
        type=str,
        default="",
        help="Comma-separated FSConnector base paths on dst-host (e.g. /mnt/p2p_ext4)",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Delete per-run FSConnector dirs after the benchmark completes",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Per-token-size timeout in seconds (default: 120)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Stream server/bench output to stdout",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    run = run_bench(args)
    print_results(run)
    return 0 if all(r.error is None for r in run.results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
