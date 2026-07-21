#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Aggregate-throughput post-processor for verbs_bench_runner sweeps.

Reads a directory tree of ``verification_*.json`` records emitted by
``verbs_bench_runner.py`` and, for each cell, computes the aggregate
DMA-phase throughput by re-parsing the ``BENCH_RDMA_DMA`` log entries::

    span_ns = max(completed_ns) - min(posted_ns)
    aggregate_gbps = iterations * page_bytes * 8 / (span_ns * 1e-9)

Cells are grouped by ``(page_bytes, qd)`` where ``qd`` is inferred from
the parent directory name (``run<N>_qd<Q>``). Per-cell reporting is the
median across the runs found on disk. Existing eligibility / manifest /
digest gates in the verification JSON are not touched; this tool is
strictly diagnostic.

Usage::

    python scripts/verbs_bench_aggregate.py \\
        results/dz1-sweep-1784629999 --csv dz1-aggregate.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_QD_DIR_RE = re.compile(r"run(\d+)_qd(\d+)")
_DMA_MARKER = "BENCH_RDMA_DMA"


@dataclass(frozen=True)
class DmaSample:
    posted_ns: int
    completed_ns: int
    bytes: int


@dataclass(frozen=True)
class CellRecord:
    path: str
    run: int
    qd: int
    page_bytes: int
    iterations: int
    eligible: bool
    digest_match: bool
    dma_med_ms: float | None
    dma_p99_ms: float | None
    aggregate_span_ns: int
    aggregate_gbps: float
    total_bytes: int


def parse_dma_records(storage_log: str) -> list[DmaSample]:
    """Extract BENCH_RDMA_DMA JSON records from a storage-side log blob."""
    samples: list[DmaSample] = []
    for line in storage_log.splitlines():
        idx = line.find(_DMA_MARKER)
        if idx < 0:
            continue
        payload = line[idx + len(_DMA_MARKER):].strip()
        try:
            record = json.loads(payload)
            samples.append(
                DmaSample(
                    posted_ns=int(record["posted_ns"]),
                    completed_ns=int(record["completed_ns"]),
                    bytes=int(record["bytes"]),
                )
            )
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
    return samples


def aggregate_span_ns(samples: Iterable[DmaSample]) -> int:
    """Return wall-clock ns from first post to last completion."""
    samples = list(samples)
    if not samples:
        return 0
    posted = min(s.posted_ns for s in samples)
    completed = max(s.completed_ns for s in samples)
    return max(0, completed - posted)


def aggregate_gbps(total_bytes: int, span_ns: int) -> float:
    """Bits/s (Gb) from DMA-phase span; 0.0 for a zero-length span."""
    if span_ns <= 0 or total_bytes <= 0:
        return 0.0
    return (total_bytes * 8.0) / span_ns


def _qd_from_dir(path: Path) -> tuple[int, int] | None:
    """Return (run, qd) inferred from the ``runN_qdQ`` parent directory."""
    match = _QD_DIR_RE.match(path.parent.name)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def load_cell(path: Path) -> CellRecord | None:
    """Load one verification JSON into a CellRecord, or None if unusable."""
    with path.open() as fh:
        record = json.load(fh)
    rq = _qd_from_dir(path)
    if rq is None:
        return None
    run, qd = rq
    page_bytes = int(record.get("page_bytes") or 0)
    iterations = int(record.get("iterations") or record.get("num_pages") or 0)
    if page_bytes <= 0 or iterations <= 0:
        return None
    storage_log = record.get("logs", {}).get("storage", "")
    dma = parse_dma_records(storage_log)
    span = aggregate_span_ns(dma)
    total_bytes = iterations * page_bytes
    gbps = aggregate_gbps(total_bytes, span)
    return CellRecord(
        path=str(path),
        run=run,
        qd=qd,
        page_bytes=page_bytes,
        iterations=iterations,
        eligible=bool(record.get("eligible_for_baseline")),
        digest_match=bool(record.get("digest_match")),
        dma_med_ms=record.get("dma_ms_median"),
        dma_p99_ms=record.get("dma_ms_p99"),
        aggregate_span_ns=span,
        aggregate_gbps=gbps,
        total_bytes=total_bytes,
    )


def load_sweep(root: Path) -> list[CellRecord]:
    """Load all ``verification_*.json`` under a sweep root."""
    records: list[CellRecord] = []
    for path in sorted(root.rglob("verification_*.json")):
        cell = load_cell(path)
        if cell is not None:
            records.append(cell)
    return records


def _median(values: Iterable[float | int]) -> float:
    xs = [float(v) for v in values if v is not None]
    return statistics.median(xs) if xs else float("nan")


def summarize(cells: list[CellRecord]) -> list[dict]:
    """Group by (page_bytes, qd) and report median across runs."""
    buckets: dict[tuple[int, int], list[CellRecord]] = {}
    for cell in cells:
        if not (cell.eligible and cell.digest_match):
            continue
        buckets.setdefault((cell.page_bytes, cell.qd), []).append(cell)
    summary: list[dict] = []
    for (page_bytes, qd), group in sorted(buckets.items()):
        summary.append(
            {
                "page_bytes": page_bytes,
                "qd": qd,
                "runs": len(group),
                "dma_med_us": _median((c.dma_med_ms or 0) * 1000 for c in group),
                "dma_p99_us": _median((c.dma_p99_ms or 0) * 1000 for c in group),
                "aggregate_span_ms": _median(
                    c.aggregate_span_ns / 1e6 for c in group
                ),
                "aggregate_gbps": _median(c.aggregate_gbps for c in group),
            }
        )
    return summary


def _print_table(rows: list[dict]) -> None:
    header = (
        f"{'Bytes':>7} {'QD':>3} {'runs':>4}  "
        f"{'dma_med_us':>10}  {'dma_p99_us':>10}  "
        f"{'agg_span_ms':>11}  {'agg_Gbps':>9}"
    )
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['page_bytes']:>7} {row['qd']:>3} {row['runs']:>4}  "
            f"{row['dma_med_us']:>10.3f}  {row['dma_p99_us']:>10.3f}  "
            f"{row['aggregate_span_ms']:>11.3f}  "
            f"{row['aggregate_gbps']:>9.2f}"
        )


def _write_csv(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "page_bytes",
                "qd",
                "runs",
                "dma_med_us",
                "dma_p99_us",
                "aggregate_span_ms",
                "aggregate_gbps",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "root", type=Path, help="Sweep root containing runN_qdQ/ subdirs"
    )
    parser.add_argument(
        "--csv", type=Path, default=None, help="Optional CSV output path"
    )
    args = parser.parse_args(argv)
    cells = load_sweep(args.root)
    if not cells:
        print(f"no verification_*.json under {args.root}", file=sys.stderr)
        return 1
    rows = summarize(cells)
    _print_table(rows)
    if args.csv:
        _write_csv(args.csv, rows)
        print(f"\nwrote {args.csv}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
