# SPDX-License-Identifier: Apache-2.0
"""RDMA NIC stress / throughput harness.

Exercises VerbsRdmaTransport directly (bypassing LMCache control flow)
to measure raw NIC throughput, latency, and data-integrity for RDMA Read
and RDMA Write operations.  Requires real RDMA hardware (CX7 / RoCEv2).

Two-process design — run each role on a separate host (or separate
processes on the same host for SoftRoCE smoke-tests):

  initiator (bmg0):  registers DRAM, publishes MR info, serves as
                     the passive memory target.
  target    (bmg1):  fetches MR info, posts RDMA Reads (store pull)
                     and RDMA Writes (retrieve push), measures bandwidth.

Usage (two terminals or two nodes):

  # bmg0 — initiator
  LMCACHE_RDMA_TRANSPORT=verbs \\
  LMCACHE_RDMA_ROLE=initiator \\
  LMCACHE_RDMA_DEVICE=mlx5_0 \\
  LMCACHE_RDMA_GID_INDEX=3 \\
  python rdma_stress.py --role initiator --listen 0.0.0.0:7700 \\
      --buf-size 512 --num-bufs 8

  # bmg1 — target
  LMCACHE_RDMA_TRANSPORT=verbs \\
  LMCACHE_RDMA_ROLE=target \\
  LMCACHE_RDMA_DEVICE=mlx5_1 \\
  LMCACHE_RDMA_GID_INDEX=3 \\
  python rdma_stress.py --role target --server bmg0:7700 \\
      --buf-size 512 --num-bufs 8 --iters 100 --verify

Buffer sizes are in KiB.  Both sides must agree on --buf-size and --num-bufs.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import statistics
import struct
import sys
import threading
import time
from dataclasses import dataclass
from typing import Sequence

from lmcache.logging import init_logger
from lmcache.v1.platform.rdma.rdma_transport import (
    MrInfo,
    RdmaFuture,
    RegisteredBuffer,
)
from lmcache.v1.platform.rdma.verbs_transport import VerbsRdmaTransport

logger = init_logger(__name__)

# ----------------------------------------------------------------------------
# Wire protocol for the control-plane rendezvous over TCP
# ----------------------------------------------------------------------------
# The initiator publishes one MrDescriptor per buffer.
# The target fetches all descriptors, then drives RDMA ops.

_CTRL_PORT = 7700  # overridden by --listen / --server args
_MAGIC = b"RDMA"   # 4-byte header to detect stale connections


@dataclass(frozen=True)
class MrDescriptor:
    """Initiator-side buffer advertisement sent to the target."""

    index: int      # buffer slot index
    addr: int       # virtual address in the initiator's VA space
    rkey: int       # remote key
    length: int     # byte length

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "addr": self.addr,
            "rkey": self.rkey,
            "length": self.length,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MrDescriptor":
        return cls(
            index=d["index"],
            addr=d["addr"],
            rkey=d["rkey"],
            length=d["length"],
        )


# ----------------------------------------------------------------------------
# Stats helpers
# ----------------------------------------------------------------------------

def _gbps(total_bytes: int, elapsed_s: float) -> float:
    return (total_bytes / 1e9) / elapsed_s if elapsed_s > 0 else float("inf")


def _p(samples: Sequence[float], pct: float) -> float:
    ordered = sorted(samples)
    idx = int(len(ordered) * pct / 100.0)
    idx = min(idx, len(ordered) - 1)
    return ordered[idx]


def _print_report(
    op: str,
    buf_size: int,
    num_bufs: int,
    iters: int,
    times: list[float],
) -> None:
    total_bytes = num_bufs * buf_size
    print(f"\n==== RDMA {op} throughput ====", flush=True)
    print(
        f"  buffer shape  : {num_bufs} x {buf_size // 1024} KiB"
        f"  ({total_bytes // (1024 * 1024)} MiB total per iteration)",
        flush=True,
    )
    print(f"  iterations    : {iters}", flush=True)
    for label, val in [
        ("best  ", min(times)),
        ("p50   ", _p(times, 50)),
        ("p99   ", _p(times, 99)),
        ("worst ", max(times)),
        ("mean  ", statistics.mean(times)),
    ]:
        print(
            f"  {label}: {val * 1e3:8.2f} ms   "
            f"{_gbps(total_bytes, val):7.2f} GB/s",
            flush=True,
        )


# ----------------------------------------------------------------------------
# Control-plane helpers (plain TCP, JSON lines)
# ----------------------------------------------------------------------------

def _send_json(sock: socket.socket, obj: dict | list) -> None:
    data = json.dumps(obj).encode() + b"\n"
    # 4-byte length prefix then payload
    sock.sendall(struct.pack(">I", len(data)) + data)


def _recv_json(sock: socket.socket) -> dict | list:
    hdr = _recv_exact(sock, 4)
    length = struct.unpack(">I", hdr)[0]
    return json.loads(_recv_exact(sock, length).decode())


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("control socket closed unexpectedly")
        buf.extend(chunk)
    return bytes(buf)


# ----------------------------------------------------------------------------
# Initiator role
# ----------------------------------------------------------------------------

def run_initiator(args: argparse.Namespace) -> None:
    """Allocate DRAM buffers, publish MR info, serve as passive RDMA target.

    Listens on a TCP control socket.  The target connects, receives MR
    descriptors, drives RDMA ops, sends a DONE signal, and disconnects.
    The initiator acknowledges and exits.
    """
    buf_size = args.buf_size * 1024
    num_bufs = args.num_bufs

    transport = VerbsRdmaTransport.from_env()
    try:
        buffers: list[RegisteredBuffer] = []
        try:
            for i in range(num_bufs):
                buf = transport.allocate_buffer(buf_size)
                # Write a per-buffer sentinel so the target can verify integrity.
                ctypes.memset(buf.addr, (i + 1) & 0xFF, buf.length)
                buffers.append(buf)

            descriptors = [
                MrDescriptor(
                    index=i,
                    addr=buf.mr.addr,
                    rkey=buf.mr.rkey,
                    length=buf.length,
                )
                for i, buf in enumerate(buffers)
            ]

            listen_host, listen_port = _parse_addr(args.listen)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                srv.bind((listen_host, listen_port))
                srv.listen(1)
                print(
                    f"[initiator] listening on {listen_host}:{listen_port}; "
                    f"{num_bufs} x {args.buf_size} KiB buffers registered",
                    flush=True,
                )
                conn, peer = srv.accept()
                with conn:
                    print(f"[initiator] target connected from {peer}", flush=True)
                    # Exchange RDMA endpoint info first
                    peer_ep = _recv_json(conn)
                    transport.connect(
                        remote_qpn=peer_ep["qpn"],
                        remote_psn=peer_ep["psn"],
                        remote_gid=peer_ep["gid"],
                        remote_lid=peer_ep.get("lid", 0),
                    )
                    local_ep = {
                        "qpn": transport._local_qpn,
                        "psn": transport._local_psn,
                        "gid": transport._local_gid,
                        "lid": transport._local_lid,
                    }
                    _send_json(conn, local_ep)

                    # Send MR descriptors
                    _send_json(conn, [d.to_dict() for d in descriptors])
                    print("[initiator] MR descriptors sent; waiting for DONE", flush=True)

                    # Wait for target to signal completion
                    msg = _recv_json(conn)
                    if msg.get("status") == "done":
                        print("[initiator] target reported DONE — exiting", flush=True)
                    else:
                        print(
                            f"[initiator] unexpected message from target: {msg}",
                            flush=True,
                        )
        finally:
            for buf in buffers:
                transport.free_buffer(buf)
    finally:
        transport.close()


# ----------------------------------------------------------------------------
# Target role
# ----------------------------------------------------------------------------

def run_target(args: argparse.Namespace) -> None:
    """Connect to the initiator, post RDMA ops, measure and report throughput.

    Runs --warmup + --iters RDMA Read passes, then --warmup + --iters RDMA
    Write passes.  Each pass transfers all --num-bufs buffers sequentially
    (single-QP serialized).  Optionally verifies data integrity.
    """
    buf_size = args.buf_size * 1024
    num_bufs = args.num_bufs
    warmup = args.warmup
    iters = args.iters
    verify = args.verify
    timeout_ms = args.timeout_ms

    transport = VerbsRdmaTransport.from_env()
    try:
        server_host, server_port = _parse_addr(args.server)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as ctrl:
            ctrl.connect((server_host, server_port))
            print(
                f"[target] connected to initiator at {server_host}:{server_port}",
                flush=True,
            )

            # Exchange RDMA endpoint info
            local_ep = {
                "qpn": transport._local_qpn,
                "psn": transport._local_psn,
                "gid": transport._local_gid,
                "lid": transport._local_lid,
            }
            _send_json(ctrl, local_ep)
            peer_ep = _recv_json(ctrl)
            transport.connect(
                remote_qpn=peer_ep["qpn"],
                remote_psn=peer_ep["psn"],
                remote_gid=peer_ep["gid"],
                remote_lid=peer_ep.get("lid", 0),
            )

            # Receive MR descriptors
            raw_descs = _recv_json(ctrl)
            descs = [MrDescriptor.from_dict(d) for d in raw_descs]
            print(
                f"[target] received {len(descs)} MR descriptors; "
                f"starting benchmark",
                flush=True,
            )

            # Allocate local buffers (one per slot)
            local_bufs: list[RegisteredBuffer] = []
            try:
                for _ in range(num_bufs):
                    local_bufs.append(transport.allocate_buffer(buf_size))

                # ---- RDMA READ benchmark (store pull: target reads from initiator) ----
                read_times = _bench_op(
                    "READ", transport, local_bufs, descs,
                    warmup, iters, timeout_ms, op="read",
                )
                _print_report("READ", buf_size, num_bufs, iters, read_times)

                # Fill local buffers with new data before write benchmark
                for i, buf in enumerate(local_bufs):
                    ctypes.memset(buf.addr, (i + 0x80) & 0xFF, buf.length)

                # ---- RDMA WRITE benchmark (retrieve push: target writes to initiator) ----
                write_times = _bench_op(
                    "WRITE", transport, local_bufs, descs,
                    warmup, iters, timeout_ms, op="write",
                )
                _print_report("WRITE", buf_size, num_bufs, iters, write_times)

                # ---- Data integrity check ----
                if verify:
                    _verify_read(transport, local_bufs, descs, timeout_ms)

                _send_json(ctrl, {"status": "done"})
            finally:
                for buf in local_bufs:
                    transport.free_buffer(buf)
    finally:
        transport.close()


def _bench_op(
    label: str,
    transport: VerbsRdmaTransport,
    local_bufs: list[RegisteredBuffer],
    descs: list[MrDescriptor],
    warmup: int,
    iters: int,
    timeout_ms: int,
    op: str,
) -> list[float]:
    """Run warmup + iters passes of either RDMA Read or Write over all buffers.

    Args:
        label: Display label (e.g. "READ").
        transport: Connected VerbsRdmaTransport in target role.
        local_bufs: Pre-registered local buffers (one per descriptor).
        descs: Remote MR descriptors from the initiator.
        warmup: Number of passes to discard before timing.
        iters: Number of passes to time.
        timeout_ms: Per-WR completion timeout in milliseconds.
        op: Either "read" (RDMA Read) or "write" (RDMA Write).

    Returns:
        List of per-pass elapsed times in seconds.
    """
    times: list[float] = []
    total = warmup + iters

    for i in range(total):
        t0 = time.perf_counter()
        for buf, desc in zip(local_bufs, descs):
            if op == "read":
                future = transport.post_read(
                    buf,
                    remote_addr=desc.addr,
                    rkey=desc.rkey,
                    length=desc.length,
                )
            else:
                future = transport.post_write(
                    buf,
                    remote_addr=desc.addr,
                    rkey=desc.rkey,
                    length=desc.length,
                )
            ok = transport.poll_completion(future, timeout_ms=timeout_ms)
            if not ok:
                transport.drain_on_timeout()
                raise RuntimeError(
                    f"{label} iter {i} buf {desc.index}: completion timed out "
                    f"after {timeout_ms} ms"
                )
        elapsed = time.perf_counter() - t0

        if i < warmup:
            if i == 0:
                print(f"[target] {label} warmup ({warmup} passes) ...", flush=True)
        else:
            times.append(elapsed)
            if (i - warmup + 1) % max(1, iters // 10) == 0:
                done = i - warmup + 1
                total_bytes = len(descs) * descs[0].length
                print(
                    f"[target] {label} {done}/{iters}: "
                    f"{elapsed * 1e3:.1f} ms  "
                    f"{_gbps(total_bytes, elapsed):.2f} GB/s",
                    flush=True,
                )

    return times


def _verify_read(
    transport: VerbsRdmaTransport,
    local_bufs: list[RegisteredBuffer],
    descs: list[MrDescriptor],
    timeout_ms: int,
) -> None:
    """RDMA-Read each buffer and verify the sentinel byte written by initiator.

    The initiator fills buffer i with byte value ``(i + 1) & 0xFF``.
    After a read we check all bytes in the local buffer match.

    Raises:
        RuntimeError: If any buffer does not match the expected pattern.
    """
    print("[target] running integrity check (RDMA Read + compare) ...", flush=True)
    for buf, desc in zip(local_bufs, descs):
        future = transport.post_read(
            buf, remote_addr=desc.addr, rkey=desc.rkey, length=desc.length
        )
        ok = transport.poll_completion(future, timeout_ms=timeout_ms)
        if not ok:
            transport.drain_on_timeout()
            raise RuntimeError(
                f"verify: RDMA Read buf {desc.index} timed out after {timeout_ms} ms"
            )
        expected = (desc.index + 1) & 0xFF
        raw = (ctypes.c_uint8 * buf.length).from_address(buf.addr)
        for byte_idx in range(buf.length):
            if raw[byte_idx] != expected:
                raise RuntimeError(
                    f"verify FAILED buf {desc.index} offset {byte_idx}: "
                    f"got 0x{raw[byte_idx]:02x}, expected 0x{expected:02x}"
                )
    print(
        f"[target] verify OK: {len(local_bufs)} buffers match expected pattern",
        flush=True,
    )


# ----------------------------------------------------------------------------
# Argument parsing
# ----------------------------------------------------------------------------

def _parse_addr(s: str) -> tuple[str, int]:
    """Parse 'host:port' into (host, port)."""
    host, _, port = s.rpartition(":")
    if not host:
        raise argparse.ArgumentTypeError(
            f"invalid address {s!r}: expected 'host:port'"
        )
    return host, int(port)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--role",
        choices=["initiator", "target"],
        required=True,
        help="Process role",
    )
    p.add_argument(
        "--listen",
        default="0.0.0.0:7700",
        metavar="HOST:PORT",
        help="[initiator] control socket bind address (default: 0.0.0.0:7700)",
    )
    p.add_argument(
        "--server",
        default="localhost:7700",
        metavar="HOST:PORT",
        help="[target] initiator control address (default: localhost:7700)",
    )
    p.add_argument(
        "--buf-size",
        type=int,
        default=512,
        metavar="KiB",
        help="Single-buffer size in KiB (default: 512)",
    )
    p.add_argument(
        "--num-bufs",
        type=int,
        default=8,
        metavar="N",
        help="Number of registered buffers (default: 8)",
    )
    p.add_argument(
        "--iters",
        type=int,
        default=50,
        metavar="N",
        help="[target] timed iterations per direction (default: 50)",
    )
    p.add_argument(
        "--warmup",
        type=int,
        default=5,
        metavar="N",
        help="[target] warmup iterations (default: 5)",
    )
    p.add_argument(
        "--timeout-ms",
        type=int,
        default=5000,
        dest="timeout_ms",
        metavar="MS",
        help="[target] per-WR completion timeout in ms (default: 5000)",
    )
    p.add_argument(
        "--verify",
        action="store_true",
        help="[target] verify data integrity after benchmark (RDMA Read + compare)",
    )
    return p


# ----------------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------------

def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.role == "initiator":
        run_initiator(args)
    else:
        run_target(args)


if __name__ == "__main__":
    main()
