#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Two-node raw-verbs benchmark runner for M1 CX7 baselines.

Drives ``scripts/bench_verbs.py`` on both sides of a paired RC-QP transfer
from a laptop/control node.  Storage is the poster (listens on the
bootstrap port); source is the producer (connects).  Both hosts run the
fail-closed provenance manifest first; either failure aborts the run
before any verbs process is launched.

Usage::

    # 128 KiB payload, qd=1, RDMA READ, storage-owned pull
    python scripts/verbs_bench_runner.py \\
        --bytes-per-iter 131072 --iterations 128 --qd 1 \\
        --src-numa-node 0 --dst-numa-node 0

    # M1 canonical sweep
    python scripts/verbs_bench_runner.py \\
        --bytes-per-iter 73728 147456 131072 262144 --iterations 256 \\
        --src-numa-node 0 --dst-numa-node 0

Topology::

    bmg0 (source)  --  192.168.200.3  --  mlx5_1 (RoCEv2, GID 3)
    bmg1 (storage) --  192.168.200.4  --  mlx5_1 (RoCEv2, GID 3)
    Cross-wire: bmg0:mlx5_1 ↔ bmg1:mlx5_1

SSH aliases ``bmg0`` / ``bmg1`` must resolve without a password.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from bench_verify import (
    CounterSnapshot,
    Transport,
    VerificationResult,
    build_result_from_digests,
    parse_ethtool_statistics,
)

# ---------------------------------------------------------------------------
# Constants — match .claude/CLAUDE.md test-node config
# ---------------------------------------------------------------------------

REPO = "~/tsg/LMCache"
VENV = f"{REPO}/.venv-ipu/bin"

BMG0_IP = "192.168.200.3"
BMG1_IP = "192.168.200.4"

BOOTSTRAP_PORT = 9600
RDMA_DEVICE = "mlx5_1"
RDMA_PORT = 1
GID_INDEX = 3

BMG0_ETHTOOL_IFACE = "ens1f1np1"
BMG1_ETHTOOL_IFACE = "ens1f1np1"

_MIN_MEMLOCK_BYTES = 8 * 1024**3


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class SizeResult:
    bytes_per_iter: int
    iterations: int
    qd: int
    direction: str
    dma_median_ms: Optional[float] = None
    control_median_ms: Optional[float] = None
    wire_bytes_total: Optional[int] = None
    wire_bytes_within_pct: Optional[bool] = None
    digest_matches: Optional[bool] = None
    transport_asserted: Optional[bool] = None
    verification_path: Optional[str] = None
    error: Optional[str] = None


@dataclass
class BenchRun:
    direction: str
    iterations: int
    qd: int
    results: list[SizeResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# SSH helpers (kept in sync with nixl_bench_runner.py)
# ---------------------------------------------------------------------------


def ssh_cmd(host: str, cmd: str) -> subprocess.Popen:
    """Launch *cmd* on *host* over a fresh SSH connection.

    ControlMaster is disabled to avoid the multiplexed-teardown hang seen
    with the sequential NIXL bench runs.
    """
    return subprocess.Popen(
        [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ControlMaster=no",
            "-o", "ControlPath=none",
            host, cmd,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
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


def kill_port(host: str, port: int) -> None:
    ssh_run(host, f"fuser -k {port}/tcp 2>/dev/null || true", timeout=5.0)


# ---------------------------------------------------------------------------
# Fail-closed manifest preflight
# ---------------------------------------------------------------------------


def create_manifest(
    host: str,
    label: str,
    role: str,
    nic: str,
    numa_node: int,
    total_bytes: int,
) -> str:
    """Run the fail-closed provenance preflight on one benchmark host.

    Raises RuntimeError if the manifest reports overall_gate_failed, so
    the caller aborts before any verbs process is launched.
    """
    output = f"/tmp/lmcache_verbs_manifest_{label}_{role}.json"
    command = (
        f"cd {REPO} && "
        f"{VENV}/python scripts/bench_manifest.py "
        f"--run-label {shlex.quote(label)} --role {shlex.quote(role)} "
        f"--numa-node {numa_node} --nic {shlex.quote(nic)} "
        f"--gid-index {GID_INDEX} "
        f"--min-memlock-bytes {_MIN_MEMLOCK_BYTES} "
        f"--alloc-region verbs_buffer:{total_bytes} "
        f"--output {shlex.quote(output)}"
    )
    rc, manifest_json = ssh_run(host, command, timeout=30.0)
    if rc != 0:
        raise RuntimeError(
            f"benchmark manifest failed on {host} (rc={rc}); benchmark not started:\n"
            f"{manifest_json.strip()}"
        )
    return output


# ---------------------------------------------------------------------------
# ethtool counters
# ---------------------------------------------------------------------------


def snapshot_nic(host: str, interface: str) -> CounterSnapshot:
    rc, output = ssh_run(host, f"ethtool -S {shlex.quote(interface)}", timeout=10.0)
    if rc != 0:
        raise RuntimeError(f"ethtool -S {interface} failed on {host}: {output.strip()}")
    return parse_ethtool_statistics(output)


# ---------------------------------------------------------------------------
# bench_verbs.py process orchestration
# ---------------------------------------------------------------------------


def _verbs_cmd(
    role: str,
    direction: str,
    iterations: int,
    bytes_per_iter: int,
    qd: int,
    nonce: str,
    bootstrap_flag: str,
) -> str:
    return (
        f"cd {REPO} && "
        f"{VENV}/python scripts/bench_verbs.py "
        f"--role {shlex.quote(role)} --direction {shlex.quote(direction)} "
        f"--device {RDMA_DEVICE} --port {RDMA_PORT} --gid-index {GID_INDEX} "
        f"--iterations {iterations} --bytes-per-iter {bytes_per_iter} --qd {qd} "
        f"--nonce {shlex.quote(nonce)} {bootstrap_flag} "
        f"2>&1"
    )


def _stream_output(
    proc: subprocess.Popen,
    prefix: str,
    lines: list[str],
    verbose: bool,
    done: threading.Event,
) -> None:
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line)
        if verbose:
            sys.stdout.write(f"[{prefix}] {line}")
            sys.stdout.flush()
    done.set()


# ---------------------------------------------------------------------------
# Structured record parsing
# ---------------------------------------------------------------------------


def _parse_records(lines: list[str], prefix: str) -> list[dict]:
    records: list[dict] = []
    for line in lines:
        if not line.startswith(prefix + " "):
            continue
        try:
            records.append(json.loads(line[len(prefix) + 1:]))
        except (ValueError, json.JSONDecodeError):
            continue
    return records


def _dma_ms_samples(dma_records: list[dict]) -> list[float]:
    samples: list[float] = []
    for record in dma_records:
        try:
            start_ns = int(record["start_ns"])
            end_ns = int(record["end_ns"])
        except (KeyError, TypeError, ValueError):
            continue
        samples.append((end_ns - start_ns) / 1_000_000)
    return samples


def _digest_from_records(records: list[dict]) -> Optional[dict]:
    if not records:
        return None
    return records[-1]


def _p99(samples: list[float]) -> float:
    """Return the nearest-rank 99th percentile of a non-empty sample set."""
    if not samples:
        raise ValueError("cannot compute p99 of empty samples")
    return sorted(samples)[math.ceil(len(samples) * 0.99) - 1]


def _publishable_gate_failures(result: VerificationResult) -> list[str]:
    """Return unmet publishable-verification requirements from a result."""
    required = {
        "digest_match": result.digest_matches,
        "wire_bytes_within_tolerance": result.wire_bytes_within_pct,
        "transport_asserted": result.transport_asserted,
    }
    return [name for name, passed in required.items() if not passed]


# ---------------------------------------------------------------------------
# Single-size bench run
# ---------------------------------------------------------------------------


def run_one_size(
    src_host: str,
    dst_host: str,
    dst_bootstrap_ip: str,
    direction: str,
    iterations: int,
    bytes_per_iter: int,
    qd: int,
    nonce: str,
    launch_timeout: float,
    verbose: bool,
) -> tuple[list[str], list[str]]:
    """Launch storage (listener) + source (connector) and return both logs."""
    # Storage side is the poster. It listens; source connects.
    kill_port(dst_host, BOOTSTRAP_PORT)

    storage_cmd = _verbs_cmd(
        role="storage",
        direction=direction,
        iterations=iterations,
        bytes_per_iter=bytes_per_iter,
        qd=qd,
        nonce=nonce,
        bootstrap_flag=f"--bootstrap-listen 0.0.0.0:{BOOTSTRAP_PORT}",
    )
    source_cmd = _verbs_cmd(
        role="source",
        direction=direction,
        iterations=iterations,
        bytes_per_iter=bytes_per_iter,
        qd=qd,
        nonce=nonce,
        bootstrap_flag=f"--bootstrap-connect {dst_bootstrap_ip}:{BOOTSTRAP_PORT}",
    )

    storage_proc = ssh_cmd(dst_host, storage_cmd)
    # Small delay so the listener is bound before the connector attempts
    # to reach it; bench_verbs itself retries for up to 30s but this
    # avoids expected retry churn in the logs.
    time.sleep(1.0)
    source_proc = ssh_cmd(src_host, source_cmd)

    storage_lines: list[str] = []
    source_lines: list[str] = []
    storage_done = threading.Event()
    source_done = threading.Event()
    storage_thread = threading.Thread(
        target=_stream_output,
        args=(storage_proc, "storage", storage_lines, verbose, storage_done),
        daemon=True,
    )
    source_thread = threading.Thread(
        target=_stream_output,
        args=(source_proc, "source", source_lines, verbose, source_done),
        daemon=True,
    )
    storage_thread.start()
    source_thread.start()

    try:
        storage_proc.wait(timeout=launch_timeout)
        source_proc.wait(timeout=launch_timeout)
    except subprocess.TimeoutExpired as exc:
        storage_proc.kill()
        source_proc.kill()
        raise TimeoutError(
            f"bench_verbs did not complete within {launch_timeout}s"
        ) from exc
    finally:
        storage_done.wait(timeout=5.0)
        source_done.wait(timeout=5.0)

    if storage_proc.returncode != 0 or source_proc.returncode != 0:
        raise RuntimeError(
            f"bench_verbs failed (storage rc={storage_proc.returncode}, "
            f"source rc={source_proc.returncode}); check logs above"
        )
    return storage_lines, source_lines


def write_verification(
    output_dir: str,
    label: str,
    bytes_per_iter: int,
    result_dict: dict,
) -> str:
    os.makedirs(output_dir, exist_ok=True)
    path = f"{output_dir}/verification_{label}_{bytes_per_iter}.json"
    with open(path, "w", encoding="ascii") as handle:
        json.dump(result_dict, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


# ---------------------------------------------------------------------------
# Verification synthesis
# ---------------------------------------------------------------------------


def _control_ms_from_records(control_records: list[dict]) -> list[float]:
    """Return per-record elapsed control-plane milliseconds.

    Each BENCH_RDMA_CONTROL record covers the QP-setup exchange only
    (bootstrap send/recv of QP info + connect() + READY handshake). It is
    emitted once per run by the poster, so this list will typically have
    length 1.
    """
    samples: list[float] = []
    for record in control_records:
        try:
            start_ns = int(record["start_ns"])
            end_ns = int(record["end_ns"])
        except (KeyError, TypeError, ValueError):
            continue
        samples.append((end_ns - start_ns) / 1_000_000)
    return samples


def build_size_result(
    size_result: SizeResult,
    storage_lines: list[str],
    source_lines: list[str],
    source_before: CounterSnapshot,
    source_after: CounterSnapshot,
    storage_before: CounterSnapshot,
    storage_after: CounterSnapshot,
    verification_dir: str,
    run_label: str,
    direction: str,
    manifest_ref: dict[str, str],
) -> None:
    dma_records = _parse_records(storage_lines, "BENCH_RDMA_DMA")
    dma_ms = _dma_ms_samples(dma_records)
    if not dma_ms:
        raise RuntimeError("no BENCH_RDMA_DMA records emitted by storage side")

    control_records = _parse_records(storage_lines, "BENCH_RDMA_CONTROL")
    control_ms = _control_ms_from_records(control_records)
    if not control_ms:
        raise RuntimeError(
            "no BENCH_RDMA_CONTROL record emitted by storage side; "
            "verifier cannot compute control-plane median"
        )

    # Producer digest lives on the side that filled its buffer with the
    # deterministic pattern; consumer digest lives on the other side.
    if direction == "read":
        producer_records = _parse_records(source_lines, "BENCH_RDMA_PRODUCER")
        consumer_records = _parse_records(storage_lines, "BENCH_RDMA_CONSUMER")
    else:
        producer_records = _parse_records(storage_lines, "BENCH_RDMA_PRODUCER")
        consumer_records = _parse_records(source_lines, "BENCH_RDMA_CONSUMER")

    producer = _digest_from_records(producer_records)
    consumer = _digest_from_records(consumer_records)
    if producer is None or consumer is None:
        raise RuntimeError(
            "missing producer or consumer digest record; cannot verify run"
        )
    if producer.get("digest_algorithm") != consumer.get("digest_algorithm"):
        raise RuntimeError("producer and consumer digest algorithms do not match")
    if producer.get("bytes_total") != consumer.get("bytes_total"):
        raise RuntimeError("producer and consumer bytes_total disagree")

    # For "read" the wire counters of interest are storage RX; for "write"
    # source RX. build_result_from_digests takes one (before, after) pair,
    # so we hand it the pair matching the receiving NIC and keep both
    # pairs in the persisted verification JSON.
    if direction == "read":
        rx_before, rx_after = storage_before, storage_after
    else:
        rx_before, rx_after = source_before, source_after

    result = build_result_from_digests(
        digest_algorithm=str(producer["digest_algorithm"]),
        producer_digest=str(producer["digest"]),
        consumer_digest=str(consumer["digest"]),
        expected_bytes=int(producer["bytes_total"]),
        before=rx_before,
        after=rx_after,
        transport=Transport.VERBS,
        transport_evidence=storage_lines + source_lines,
        control_ms=control_ms,
        dma_ms=dma_ms,
    )

    expected_bytes = int(producer["bytes_total"])
    record = {
        "data_quality": "publishable",
        "transport": result.transport,
        "run_label": run_label,
        "direction": direction,
        "producer_digest": {
            "algo": result.digest_algorithm,
            "hex": result.producer_digest,
            "bytes": expected_bytes,
        },
        "consumer_digest": {
            "algo": result.digest_algorithm,
            "hex": result.consumer_digest,
            "bytes": expected_bytes,
        },
        "digest_match": result.digest_matches,
        "wire_bytes_total": result.wire_bytes_total,
        "wire_bytes_within_tolerance": result.wire_bytes_within_pct,
        "dma_ms_median": result.dma_median_ms,
        "dma_ms_p50": result.dma_median_ms,
        "dma_ms_p99": _p99(dma_ms),
        "control_ms_median": result.control_median_ms,
        "control_dominated": result.control_plane_dominated,
        "transport_asserted": result.transport_asserted,
        "unavailable_fields": [],
        "unavailable_reason": None,
        "page_bytes": size_result.bytes_per_iter,
        "num_pages": size_result.iterations,
        "iterations": size_result.iterations,
        "manifest_ref": dict(manifest_ref),
        "verification_evidence": json.loads(result.as_json()),
        "nic_counters": {
            "source": {
                "before": source_before.counters,
                "after": source_after.counters,
            },
            "storage": {
                "before": storage_before.counters,
                "after": storage_after.counters,
            },
        },
        "logs": {
            "storage": "".join(storage_lines),
            "source": "".join(source_lines),
        },
    }
    path = write_verification(
        verification_dir, run_label, size_result.bytes_per_iter, record
    )
    size_result.dma_median_ms = result.dma_median_ms
    size_result.control_median_ms = result.control_median_ms
    size_result.wire_bytes_total = result.wire_bytes_total
    size_result.wire_bytes_within_pct = result.wire_bytes_within_pct
    size_result.digest_matches = result.digest_matches
    size_result.transport_asserted = result.transport_asserted
    size_result.verification_path = path
    failures = _publishable_gate_failures(result)
    if failures:
        raise RuntimeError(
            "publishable verification gate failed: " + ", ".join(failures)
        )


# ---------------------------------------------------------------------------
# Main benchmark driver
# ---------------------------------------------------------------------------


def run_bench(args: argparse.Namespace) -> BenchRun:
    src_host = args.src_host
    dst_host = args.dst_host
    dst_bootstrap_ip = args.dst_bootstrap_ip
    run_label = f"verbs_{int(time.time())}"

    sizes: list[int] = sorted(set(args.bytes_per_iter))
    largest = sizes[-1]
    total_bytes = largest * args.iterations

    run = BenchRun(direction=args.direction, iterations=args.iterations, qd=args.qd)

    # --- 1. Fail-closed manifest on both hosts, aborts on any failure ---
    print("==> Collecting fail-closed benchmark manifests (both hosts)...")
    source_manifest = create_manifest(
        src_host, run_label, "source", RDMA_DEVICE, args.src_numa_node, total_bytes
    )
    storage_manifest = create_manifest(
        dst_host, run_label, "storage", RDMA_DEVICE, args.dst_numa_node, total_bytes
    )
    print(f"    source manifest:  {source_manifest}")
    print(f"    storage manifest: {storage_manifest}")
    manifest_ref = {"source": source_manifest, "storage": storage_manifest}

    # --- 2. Sweep sizes ---
    print(
        f"==> Running verbs bench (direction={args.direction}, "
        f"iterations={args.iterations}, qd={args.qd}) for sizes: {sizes}"
    )
    for bytes_per_iter in sizes:
        print(f"\n--- {bytes_per_iter} bytes/iter ---")
        result = SizeResult(
            bytes_per_iter=bytes_per_iter,
            iterations=args.iterations,
            qd=args.qd,
            direction=args.direction,
        )
        try:
            source_before = snapshot_nic(src_host, BMG0_ETHTOOL_IFACE)
            storage_before = snapshot_nic(dst_host, BMG1_ETHTOOL_IFACE)
            storage_lines, source_lines = run_one_size(
                src_host=src_host,
                dst_host=dst_host,
                dst_bootstrap_ip=dst_bootstrap_ip,
                direction=args.direction,
                iterations=args.iterations,
                bytes_per_iter=bytes_per_iter,
                qd=args.qd,
                nonce=f"{run_label}_{bytes_per_iter}",
                launch_timeout=args.timeout,
                verbose=args.verbose,
            )
            source_after = snapshot_nic(src_host, BMG0_ETHTOOL_IFACE)
            storage_after = snapshot_nic(dst_host, BMG1_ETHTOOL_IFACE)
            build_size_result(
                size_result=result,
                storage_lines=storage_lines,
                source_lines=source_lines,
                source_before=source_before,
                source_after=source_after,
                storage_before=storage_before,
                storage_after=storage_after,
                verification_dir=args.verification_dir,
                run_label=run_label,
                direction=args.direction,
                manifest_ref=manifest_ref,
            )
            print(
                f"  dma_median={result.dma_median_ms:.3f}ms  "
                f"wire={result.wire_bytes_total}B  "
                f"digest_matches={result.digest_matches}  "
                f"transport_asserted={result.transport_asserted}\n"
                f"  verification={result.verification_path}"
            )
        except (TimeoutError, RuntimeError) as exc:
            result.error = str(exc)
            print(f"  ERROR: {exc}", file=sys.stderr)
        run.results.append(result)

    kill_port(dst_host, BOOTSTRAP_PORT)
    return run


# ---------------------------------------------------------------------------
# Result table
# ---------------------------------------------------------------------------


def print_results(run: BenchRun) -> None:
    print(f"\n{'=' * 90}")
    print(
        f"Direction: {run.direction}  |  iterations: {run.iterations}  |  qd: {run.qd}"
    )
    print(f"{'=' * 90}")
    header = (
        f"{'Bytes':>10}  {'dma_med(ms)':>12}  {'wire_bytes':>12}  "
        f"{'wire_ok':>8}  {'digest_ok':>10}  {'transport_ok':>13}"
    )
    print(header)
    print("-" * len(header))
    for r in run.results:
        if r.error:
            print(f"{r.bytes_per_iter:>10}  ERROR: {r.error}")
            continue
        dm = f"{r.dma_median_ms:.3f}" if r.dma_median_ms is not None else "n/a"
        wb = str(r.wire_bytes_total) if r.wire_bytes_total is not None else "n/a"
        print(
            f"{r.bytes_per_iter:>10}  {dm:>12}  {wb:>12}  "
            f"{str(r.wire_bytes_within_pct):>8}  "
            f"{str(r.digest_matches):>10}  "
            f"{str(r.transport_asserted):>13}"
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
        "--src-host", default="bmg0",
        help="SSH alias for source node (default: bmg0)",
    )
    parser.add_argument(
        "--dst-host", default="dev@192.168.200.4",
        help=(
            "SSH target for storage node; use direct fabric IP to avoid "
            "ProxyJump contention (default: dev@192.168.200.4)"
        ),
    )
    parser.add_argument(
        "--dst-bootstrap-ip", default=BMG1_IP,
        help=(
            f"Storage-side IP the source connects to for QP bootstrap "
            f"(default: {BMG1_IP})"
        ),
    )
    parser.add_argument("--src-numa-node", type=int, required=True)
    parser.add_argument("--dst-numa-node", type=int, required=True)
    parser.add_argument(
        "--direction", choices=("read", "write"), default="read",
        help="Storage-side operation (default: read = pull from source)",
    )
    parser.add_argument(
        "--iterations", type=int, default=256,
        help="Number of RDMA operations per size (default: 256)",
    )
    parser.add_argument(
        "--bytes-per-iter", type=int, nargs="+",
        default=[73728, 147456, 131072, 262144],
        help=(
            "Payload sizes to sweep in bytes "
            "(default: 72 KiB, 144 KiB, 128 KiB, 256 KiB)"
        ),
    )
    parser.add_argument(
        "--qd", type=int, default=1,
        help="Outstanding-request depth per size (default: 1)",
    )
    parser.add_argument(
        "--timeout", type=float, default=120.0,
        help="Per-size bench_verbs timeout in seconds (default: 120)",
    )
    parser.add_argument(
        "--verification-dir", default=".",
        help="Local directory for per-size verification JSON records",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Stream bench_verbs output to stdout",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    run = run_bench(args)
    print_results(run)
    return 0 if all(r.error is None for r in run.results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
