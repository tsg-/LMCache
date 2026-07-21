# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/verbs_bench_aggregate.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "verbs_bench_aggregate.py"
_spec = importlib.util.spec_from_file_location(
    "verbs_bench_aggregate", str(_MODULE_PATH)
)
assert _spec and _spec.loader
verbs_bench_aggregate = importlib.util.module_from_spec(_spec)
sys.modules["verbs_bench_aggregate"] = verbs_bench_aggregate
_spec.loader.exec_module(verbs_bench_aggregate)  # type: ignore[union-attr]


def _dma_line(page_idx: int, posted_ns: int, completed_ns: int, page_bytes: int) -> str:
    payload = {
        "bytes": page_bytes,
        "completed_ns": completed_ns,
        "end_ns": completed_ns,
        "operation": "read",
        "page_idx": page_idx,
        "posted_ns": posted_ns,
        "start_ns": posted_ns,
    }
    return f"BENCH_RDMA_DMA {json.dumps(payload)}"


def _make_storage_log(page_bytes: int, iterations: int, span_ns: int) -> str:
    """Fabricate a storage log whose first-post-to-last-completion is span_ns.

    The first sample is posted at 0 and completed at span_ns // iterations;
    the last sample completes at exactly span_ns so callers get exact math.
    """
    lines = ["BENCH_RDMA_CONTROL {\"phase\": \"qp_setup\"}"]
    per = max(1, span_ns // iterations)
    for i in range(iterations):
        posted = i * per if i < iterations - 1 else max(0, span_ns - per)
        completed = posted + per if i < iterations - 1 else span_ns
        lines.append(_dma_line(i, posted, completed, page_bytes))
    return "\n".join(lines)


def test_parse_dma_records_extracts_posted_and_completed():
    log = _make_storage_log(page_bytes=131072, iterations=4, span_ns=4000)
    samples = verbs_bench_aggregate.parse_dma_records(log)
    assert len(samples) == 4
    assert samples[0].posted_ns == 0
    assert samples[-1].completed_ns == 4000
    assert all(s.bytes == 131072 for s in samples)


def test_aggregate_span_and_gbps_math():
    log = _make_storage_log(page_bytes=131072, iterations=256, span_ns=1_000_000)
    samples = verbs_bench_aggregate.parse_dma_records(log)
    span = verbs_bench_aggregate.aggregate_span_ns(samples)
    assert span == 1_000_000
    total = 256 * 131072
    gbps = verbs_bench_aggregate.aggregate_gbps(total, span)
    assert gbps == total * 8 / 1_000_000


def test_aggregate_gbps_zero_span_returns_zero():
    assert verbs_bench_aggregate.aggregate_gbps(1024, 0) == 0.0
    assert verbs_bench_aggregate.aggregate_gbps(0, 1000) == 0.0


def test_summarize_medians_across_runs(tmp_path: Path):
    page_bytes = 131072
    iterations = 128
    for run in (1, 2, 3):
        for qd, span_ns in [(1, 1_000_000), (4, 500_000)]:
            outdir = tmp_path / f"run{run}_qd{qd}"
            outdir.mkdir(parents=True, exist_ok=True)
            record = {
                "eligible_for_baseline": True,
                "digest_match": True,
                "page_bytes": page_bytes,
                "iterations": iterations,
                "dma_ms_median": 0.01 + run * 0.001,
                "dma_ms_p99": 0.02 + run * 0.001,
                "logs": {
                    "storage": _make_storage_log(page_bytes, iterations, span_ns),
                },
            }
            (outdir / f"verification_{page_bytes}.json").write_text(
                json.dumps(record)
            )

    cells = verbs_bench_aggregate.load_sweep(tmp_path)
    assert len(cells) == 6

    summary = verbs_bench_aggregate.summarize(cells)
    assert [row["qd"] for row in summary] == [1, 4]

    total = iterations * page_bytes
    assert summary[0]["aggregate_gbps"] == total * 8 / 1_000_000
    assert summary[1]["aggregate_gbps"] == total * 8 / 500_000

    assert summary[0]["runs"] == 3
    assert summary[0]["dma_med_us"] == 12.0


def test_summarize_ignores_ineligible_cells(tmp_path: Path):
    outdir = tmp_path / "run1_qd1"
    outdir.mkdir()
    record = {
        "eligible_for_baseline": False,
        "digest_match": False,
        "page_bytes": 131072,
        "iterations": 32,
        "logs": {"storage": _make_storage_log(131072, 32, 1000)},
    }
    (outdir / "verification_131072.json").write_text(json.dumps(record))
    cells = verbs_bench_aggregate.load_sweep(tmp_path)
    assert len(cells) == 1
    assert verbs_bench_aggregate.summarize(cells) == []
