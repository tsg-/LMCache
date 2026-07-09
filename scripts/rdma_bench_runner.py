#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Two-node benchmark runner: lmcache server (target/bmg1) + bench client (initiator/bmg0).

Starts the server and bench client simultaneously over SSH, relays RDMA
endpoint files for QP rendezvous, and waits for the bench to complete.

Usage::

    python scripts/rdma_bench_runner.py \\
        --server-host bmg1 --client-host bmg0 \\
        --num-tokens 512 --start 100 --end 105 \\
        --nonce bench01

The server ZMQ port is 5555; the HTTP API port is 8080.
Cross-wired NIC pairing (per test_report.md):
  bmg0:mlx5_0 (192.168.100.x)  ↔  bmg1:rocep153s0f1 (192.168.200.x)
"""

from __future__ import annotations

import argparse
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path

ENDPOINT_DIR = "/tmp"
POLL_INTERVAL = 0.5
READY_MARKER = "VERBS_TRANSPORT: QP connected"
BENCH_DONE_MARKER = "Done."
SERVER_ZMQ_PORT = 5555


def endpoint_path(role: str, nonce: str) -> str:
    return f"{ENDPOINT_DIR}/lmcache_rdma_{role}_{nonce}.json"


def relay_file(src_host: str, src_path: str, dst_host: str, dst_path: str) -> bool:
    tmp = f"/tmp/.rdma_bench_relay_{secrets.token_hex(4)}.json"
    dl = subprocess.run(
        ["scp", "-q", f"{src_host}:{src_path}", tmp], capture_output=True
    )
    if dl.returncode != 0:
        return False
    ul = subprocess.run(
        ["scp", "-q", tmp, f"{dst_host}:{dst_path}"], capture_output=True
    )
    subprocess.run(["rm", "-f", tmp], capture_output=True)
    return ul.returncode == 0


def stream_output(
    proc: subprocess.Popen,
    prefix: str,
    done_event: threading.Event,
    marker: str,
) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(f"[{prefix}] {line}")
        sys.stdout.flush()
        if marker in line:
            done_event.set()
    done_event.set()  # EOF counts as done


def run_bench(
    server_host: str,
    client_host: str,
    nonce: str,
    num_tokens: int,
    start: int,
    end: int,
    timeout: float,
    l1_size_gb: int,
    interval: float,
) -> int:
    venv = "~/tsg/LMCache/.venv-ipu/bin"
    repo = "~/tsg/LMCache"

    # Server env — target role, bmg1 uses rocep153s0f1 per cross-wiring
    server_env = (
        f"LMCACHE_RDMA_TRANSPORT=verbs "
        f"LMCACHE_RDMA_ROLE=target "
        f"LMCACHE_RDMA_DEVICE=rocep153s0f1 "
        f"LMCACHE_RDMA_GID_INDEX=3 "
        f"LMCACHE_RDMA_NONCE={nonce} "
        f"LMCACHE_RDMA_ENDPOINT_FILE={endpoint_path('target', nonce)} "
        f"LMCACHE_RDMA_PEER_ENDPOINT_FILE={endpoint_path('initiator', nonce)}"
    )
    server_cmd = (
        f"cd {repo} && "
        f"env {server_env} "
        f"{venv}/lmcache server "
        f"--host 0.0.0.0 --port {SERVER_ZMQ_PORT} "
        f"--supported-transfer-mode rdma "
        f"--l1-size-gb {l1_size_gb} "
        f"--eviction-policy LRU "
        f"--http-port 8080 "
        f"2>&1"
    )

    # Client env — initiator role, bmg0 uses mlx5_0
    client_env = (
        f"LMCACHE_RDMA_TRANSPORT=verbs "
        f"LMCACHE_RDMA_ROLE=initiator "
        f"LMCACHE_RDMA_DEVICE=mlx5_0 "
        f"LMCACHE_RDMA_GID_INDEX=3 "
        f"LMCACHE_RDMA_NONCE={nonce} "
        f"LMCACHE_RDMA_ENDPOINT_FILE={endpoint_path('initiator', nonce)} "
        f"LMCACHE_RDMA_PEER_ENDPOINT_FILE={endpoint_path('target', nonce)}"
    )
    client_cmd = (
        f"cd {repo} && "
        f"env {client_env} "
        f"{venv}/lmcache bench server "
        f"--rpc-url tcp://{server_host}:{SERVER_ZMQ_PORT} "
        f"--mode rdma "
        f"--num-tokens {num_tokens} "
        f"--start {start} --end {end} "
        f"--interval {interval} "
        f"2>&1"
    )

    print(f"nonce: {nonce}")
    print(f"server ({server_host}): {server_cmd}")
    print(f"client ({client_host}): {client_cmd}")
    print()

    # Launch both sides
    server_proc = subprocess.Popen(
        ["ssh", server_host, server_cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    # Small delay so server binds ZMQ before client connects
    time.sleep(1.0)
    client_proc = subprocess.Popen(
        ["ssh", client_host, client_cmd],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    server_ready = threading.Event()
    client_done = threading.Event()

    server_thread = threading.Thread(
        target=stream_output,
        args=(server_proc, "SERVER", server_ready, READY_MARKER),
        daemon=True,
    )
    client_thread = threading.Thread(
        target=stream_output,
        args=(client_proc, "CLIENT", client_done, BENCH_DONE_MARKER),
        daemon=True,
    )
    server_thread.start()
    client_thread.start()

    # Relay endpoint files until QP is up on both sides or timeout
    deadline = time.monotonic() + timeout
    relayed_target = False
    relayed_initiator = False

    while time.monotonic() < deadline:
        if not relayed_target:
            if relay_file(
                server_host,
                endpoint_path("target", nonce),
                client_host,
                endpoint_path("target", nonce),
            ):
                print("[relay] target endpoint → client host")
                relayed_target = True

        if not relayed_initiator:
            if relay_file(
                client_host,
                endpoint_path("initiator", nonce),
                server_host,
                endpoint_path("initiator", nonce),
            ):
                print("[relay] initiator endpoint → server host")
                relayed_initiator = True

        if relayed_target and relayed_initiator:
            print("[relay] both endpoints relayed — waiting for bench completion")
            break

        time.sleep(POLL_INTERVAL)

    if not (relayed_target and relayed_initiator):
        print("ERROR: endpoint relay timed out", file=sys.stderr)
        server_proc.terminate()
        client_proc.terminate()
        return 1

    # Wait for bench client to finish
    if not client_done.wait(timeout=timeout):
        print("ERROR: bench client timed out", file=sys.stderr)
        server_proc.terminate()
        client_proc.terminate()
        return 1

    client_proc.wait(timeout=10)
    rc = client_proc.returncode

    # Terminate server (it runs indefinitely)
    server_proc.terminate()
    try:
        server_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        server_proc.kill()

    # Clean up endpoint files
    for host, role in [(server_host, "target"), (client_host, "initiator")]:
        subprocess.run(
            ["ssh", host, f"rm -f {endpoint_path(role, nonce)}"],
            capture_output=True,
        )

    return rc if rc is not None else 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-host", default="bmg1")
    parser.add_argument("--client-host", default="bmg0")
    parser.add_argument("--nonce", default=None)
    parser.add_argument("--num-tokens", type=int, default=512)
    parser.add_argument("--start", type=int, default=100)
    parser.add_argument("--end", type=int, default=105)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--l1-size-gb", type=int, default=4)
    parser.add_argument("--interval", type=float, default=0.5)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    nonce = args.nonce or secrets.token_hex(6)
    return run_bench(
        server_host=args.server_host,
        client_host=args.client_host,
        nonce=nonce,
        num_tokens=args.num_tokens,
        start=args.start,
        end=args.end,
        timeout=args.timeout,
        l1_size_gb=args.l1_size_gb,
        interval=args.interval,
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
