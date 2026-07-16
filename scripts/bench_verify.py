#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Transport-neutral evidence collection for storage benchmark runs."""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import subprocess
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Iterable


class Transport(StrEnum):
    """Data-plane transport selected for a benchmark run."""

    VERBS = "verbs"
    UCX = "ucx"
    NIXL = "nixl"
    IPU_HOST_RDMA = "ipu_host_rdma"


@dataclass(frozen=True)
class CounterSnapshot:
    """Driver-supported NIC counters captured at one point in time."""

    counters: dict[str, int]


@dataclass(frozen=True)
class DmaSample:
    """One target-side DMA completion emitted by the storage server."""

    operation: str
    request_id: str
    bytes_transferred: int
    dma_ms: float


@dataclass(frozen=True)
class VerificationResult:
    """Evidence required to publish a benchmark result."""

    digest_algorithm: str
    producer_digest: str
    consumer_digest: str
    digest_matches: bool
    wire_counter_delta: dict[str, int]
    estimated_wire_bytes: dict[str, int]
    wire_bytes_total: int
    wire_bytes_within_pct: bool
    wire_bytes_observed: bool
    transport: Transport
    transport_asserted: bool
    control_median_ms: float
    dma_median_ms: float
    control_plane_dominated: bool
    data_quality: str = "publishable"
    unavailable_fields: list[str] | None = None
    unavailable_reason: str | None = None

    def as_json(self) -> str:
        """Serialize the stable verification record."""
        return json.dumps(asdict(self), sort_keys=True)


def build_diagnostic_result(
    transport: Transport,
    transport_evidence: Iterable[str],
    control_ms: list[float],
    unavailable_fields: list[str],
    unavailable_reason: str,
) -> dict[str, object]:
    """Build a diagnostic record without fabricating unavailable evidence."""
    if not control_ms:
        raise ValueError("control timing samples are required")
    if not unavailable_fields or not unavailable_reason:
        raise ValueError("diagnostic records require unavailable evidence details")
    return {
        "data_quality": "diagnostic",
        "transport": transport,
        "producer_digest": None,
        "wire_bytes_total": None,
        "wire_bytes_within_tolerance": None,
        "dma_ms_median": None,
        "control_ms_median": statistics.median(control_ms),
        "control_dominated": None,
        "transport_asserted": assert_transport(transport, transport_evidence),
        "unavailable_fields": unavailable_fields,
        "unavailable_reason": unavailable_reason,
    }


_COUNTER_RE = re.compile(r"^\s*(?P<name>[^:]+):\s*(?P<value>\d+)\s*$")
_WIRE_COUNTER_RE = re.compile(r"(?:rx|tx|xmit|rcv).*(?:byte|octet|packet|data)", re.I)
_DMA_RECORD_RE = re.compile(
    r"BENCH_RDMA_DMA operation=(?P<operation>\w+) "
    r"request_id=(?P<request_id>\S+) bytes=(?P<bytes>\d+) "
    r"dma_ms=(?P<dma_ms>\d+(?:\.\d+)?)"
)
_VERBS_TRANSPORT_RE = re.compile(
    r"TRANSPORT verbs rc_mlx5 device=\S+ qp_num=\d+ gid_index=\d+"
)
_VERBS_MR_RE = re.compile(
    r"^TRANSPORT_MR flags=(?P<flags>\S+) direction=(?P<direction>read|write) "
    r"role=(?P<role>source|storage)\b",
    re.MULTILINE,
)


def parse_verbs_mr_evidence(evidence: Iterable[str]) -> dict[tuple[str, str], set[str]]:
    """Return observed MR access-flag sets keyed by ``(direction, role)``.

    Diagnostic helper for benchmark runners: emits the observed flag set
    per endpoint so callers can compare against the direction-minimum
    plan without treating a mismatch as a transport-assertion failure.
    """
    observed: dict[tuple[str, str], set[str]] = {}
    joined = "\n".join(evidence)
    for match in _VERBS_MR_RE.finditer(joined):
        key = (match.group("direction"), match.group("role"))
        observed[key] = set(match.group("flags").split("|"))
    return observed


def verbs_mr_evidence_matches_minimum(
    observed: dict[tuple[str, str], set[str]],
) -> bool:
    """Return whether both endpoints used direction-minimum MR flags.

    Diagnostic only for M1 (not a hard publication gate). Both endpoints
    for a single direction must be present, source must be
    ``LOCAL_WRITE + REMOTE_{READ,WRITE}`` for its direction, and storage
    must be ``LOCAL_WRITE`` alone.
    """
    expected_source_flag = {
        "read": "IBV_ACCESS_REMOTE_READ",
        "write": "IBV_ACCESS_REMOTE_WRITE",
    }
    for direction, source_flag in expected_source_flag.items():
        source_flags = observed.get((direction, "source"))
        storage_flags = observed.get((direction, "storage"))
        if source_flags is None or storage_flags is None:
            continue
        return source_flags == {"IBV_ACCESS_LOCAL_WRITE", source_flag} and (
            storage_flags == {"IBV_ACCESS_LOCAL_WRITE"}
        )
    return False


def producer_digest(payload: bytes) -> str:
    """Return a digest of payload bytes and expose its algorithm separately.

    ``digest_algorithm()`` identifies whether this host used BLAKE3 or the
    BLAKE2b fallback. Consumers must reject records whose algorithms differ.
    """
    try:
        import blake3
    except ImportError:
        return hashlib.blake2b(payload, digest_size=32).hexdigest()
    return blake3.blake3(payload).hexdigest()


def digest_algorithm() -> str:
    """Return the algorithm used for producer and consumer digests."""
    try:
        import blake3
    except ImportError:
        return "blake2b-256"
    return f"blake3-{blake3.__version__}"


def parse_ethtool_statistics(output: str) -> CounterSnapshot:
    """Parse numeric counters from ``ethtool -S`` output."""
    counters: dict[str, int] = {}
    for line in output.splitlines():
        match = _COUNTER_RE.match(line)
        if match:
            counters[match.group("name").strip()] = int(match.group("value"))
    return CounterSnapshot(counters=counters)


def collect_ethtool_statistics(interface: str) -> CounterSnapshot:
    """Collect counters without assuming driver-specific counter names."""
    result = subprocess.run(
        ["ethtool", "-S", interface],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ethtool -S {interface} failed: {result.stderr.strip()}")
    return parse_ethtool_statistics(result.stdout)


def parse_dma_samples_text(output: str) -> list[DmaSample]:
    """Parse legacy text DMA records for diagnostic callers only."""
    return [
        DmaSample(
            operation=match.group("operation"),
            request_id=match.group("request_id"),
            bytes_transferred=int(match.group("bytes")),
            dma_ms=float(match.group("dma_ms")),
        )
        for match in _DMA_RECORD_RE.finditer(output)
    ]


def parse_dma_samples(output: str) -> list[DmaSample]:
    """Parse structured target-side DMA completion records."""
    samples: list[DmaSample] = []
    for line in output.splitlines():
        if not line.startswith("BENCH_RDMA_DMA "):
            continue
        try:
            record = json.loads(line.removeprefix("BENCH_RDMA_DMA "))
            elapsed_ms = (int(record["end_ns"]) - int(record["start_ns"])) / 1_000_000
            samples.append(
                DmaSample(
                    operation=str(record.get("operation", "unknown")),
                    request_id=str(record.get("page_idx", "unknown")),
                    bytes_transferred=int(record["bytes"]),
                    dma_ms=elapsed_ms,
                )
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return samples


def wire_counter_delta(
    before: CounterSnapshot, after: CounterSnapshot
) -> dict[str, int]:
    """Return positive deltas for dynamically discovered wire counters."""
    return {
        name: after_value - before.counters.get(name, 0)
        for name, after_value in after.counters.items()
        if _WIRE_COUNTER_RE.search(name) and after_value > before.counters.get(name, 0)
    }


def estimate_wire_bytes(counter_delta: dict[str, int]) -> dict[str, int]:
    """Convert recognized hardware counters to estimated bytes.

    Mellanox ``port_*_data`` counters are InfiniBand PMA four-byte lanes.
    Generic ``*_bytes`` and ``*_octets`` counters already report bytes.
    Packet counters intentionally remain excluded because their byte size is
    workload-dependent.
    """
    estimates: dict[str, int] = {}
    for name, value in counter_delta.items():
        lowered = name.lower()
        if "data" in lowered:
            estimates[name] = value * 4
        elif "byte" in lowered or "octet" in lowered:
            estimates[name] = value
    return estimates


def wire_bytes_within_tolerance(
    estimates: dict[str, int], expected_bytes: int, tolerance_pct: float
) -> bool:
    """Return whether the largest byte estimate is plausible.

    Driver counters can report the same transfer at multiple layers. The
    largest estimate is retained instead of summing those overlapping views.
    """
    if expected_bytes <= 0 or tolerance_pct < 0:
        raise ValueError("expected bytes must be positive and tolerance non-negative")
    permitted = expected_bytes * tolerance_pct / 100
    observed = max(estimates.values(), default=0)
    return abs(observed - expected_bytes) <= permitted


def assert_transport(transport: Transport, evidence: Iterable[str]) -> bool:
    """Verify transport-specific evidence from logs or diagnostics.

    For :class:`Transport.VERBS` this checks the RC-QP transport line only.
    MR-flag evidence is retained as a separate diagnostic surface
    (:func:`parse_verbs_mr_evidence`) and is not a M1 publication gate.
    """
    joined = "\n".join(evidence)
    if transport is Transport.VERBS:
        return _VERBS_TRANSPORT_RE.search(joined) is not None
    if transport is Transport.UCX:
        return "rc_mlx5" in joined
    if transport is Transport.NIXL:
        return "NIXL_NET_BACKEND=" in joined and "nixl://" in joined
    return (
        "submitted=" in joined and "completed=" in joined and "queue_depth=" in joined
    )


def build_result(
    producer_payload: bytes,
    consumer_payload: bytes,
    before: CounterSnapshot,
    after: CounterSnapshot,
    transport: Transport,
    transport_evidence: Iterable[str],
    control_ms: list[float],
    dma_ms: list[float],
    wire_tolerance_pct: float = 10.0,
) -> VerificationResult:
    """Build a publishable verification record from benchmark evidence."""
    return build_result_from_digests(
        digest_algorithm=digest_algorithm(),
        producer_digest=producer_digest(producer_payload),
        consumer_digest=producer_digest(consumer_payload),
        expected_bytes=len(producer_payload),
        before=before,
        after=after,
        transport=transport,
        transport_evidence=transport_evidence,
        control_ms=control_ms,
        dma_ms=dma_ms,
        wire_tolerance_pct=wire_tolerance_pct,
    )


def build_result_from_digests(
    digest_algorithm: str,
    producer_digest: str,
    consumer_digest: str,
    expected_bytes: int,
    before: CounterSnapshot,
    after: CounterSnapshot,
    transport: Transport,
    transport_evidence: Iterable[str],
    control_ms: list[float],
    dma_ms: list[float],
    wire_tolerance_pct: float = 10.0,
) -> VerificationResult:
    """Build a publishable record from endpoint digest records and evidence."""
    if not control_ms or not dma_ms:
        raise ValueError("control and DMA timing samples are required")
    if expected_bytes <= 0:
        raise ValueError("expected bytes must be positive")
    delta = wire_counter_delta(before, after)
    estimates = estimate_wire_bytes(delta)
    control_median = statistics.median(control_ms)
    dma_median = statistics.median(dma_ms)
    return VerificationResult(
        digest_algorithm=digest_algorithm,
        producer_digest=producer_digest,
        consumer_digest=consumer_digest,
        digest_matches=producer_digest == consumer_digest,
        wire_counter_delta=delta,
        estimated_wire_bytes=estimates,
        wire_bytes_total=max(estimates.values(), default=0),
        wire_bytes_within_pct=wire_bytes_within_tolerance(
            estimates, expected_bytes, wire_tolerance_pct
        ),
        wire_bytes_observed=bool(estimates),
        transport=transport,
        transport_asserted=assert_transport(transport, transport_evidence),
        control_median_ms=control_median,
        dma_median_ms=dma_median,
        control_plane_dominated=control_median >= dma_median,
    )
