#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Aggregate a local multi-process fs_native sustained-load run.

This reports a bounded functional test: independent ``lmcache bench l2``
processes on one initiator host read the same immutable corpus through the
existing NVMe-oF controllers. It is not a shared-writer, controller, MP, QP,
or physical multi-initiator result.
"""

from __future__ import annotations

# Standard
import argparse
import json
import sys
from pathlib import Path
from typing import Any


_ERROR_COUNTERS = (
    "RetransSegs",
    "Nak Sequence Error",
    "RTO",
    "RNR received",
    "Rcvd Out of order packets",
    "InProtoErrors",
)


def _worker_start(log_path: Path) -> float | None:
    """Return the timestamped measured-window start recorded in a worker log."""
    try:
        for line in log_path.read_text(errors="replace").splitlines():
            if "Sustained window" in line:
                return float(line.split(maxsplit=1)[0])
    except (OSError, ValueError):
        return None
    return None


def _load_worker(
    run_dir: Path, worker_id: int, data_size_bytes: int
) -> tuple[dict[str, Any] | None, str | None]:
    """Load one worker's result, status, and measured-window metadata."""
    json_path = run_dir / f"initiator-{worker_id}.json"
    log_path = run_dir / f"initiator-{worker_id}.log"
    status_path = run_dir / f"initiator-{worker_id}.status"
    if not json_path.is_file():
        return None, f"initiator {worker_id}: missing {json_path.name}"
    if not status_path.is_file():
        return None, f"initiator {worker_id}: missing {status_path.name}"
    try:
        exit_code = int(status_path.read_text().strip())
        metrics = json.loads(json_path.read_text())["metrics"]
        config = metrics["config"]
        load = next(
            section
            for section in metrics.values()
            if section.get("operation") == "Load"
        )
    except (KeyError, OSError, StopIteration, ValueError, json.JSONDecodeError) as exc:
        return None, f"initiator {worker_id}: invalid result ({exc})"

    total_keys = int(load.get("total_keys") or 0)
    total_success = int(load.get("total_success") or 0)
    window_sec = float(load.get("window_sec") or 0.0)
    started_at = _worker_start(log_path)
    return {
        "initiator_id": worker_id,
        "exit_code": exit_code,
        "mode": config.get("mode"),
        "started_at_unix_sec": started_at,
        "window_sec": window_sec,
        "drain_tail_sec": float(load.get("drain_tail_sec") or 0.0),
        "total_keys": total_keys,
        "total_success": total_success,
        "timed_out": bool(load.get("timed_out")),
        "success_bytes": total_success * data_size_bytes,
        "result_path": json_path.name,
        "log_path": log_path.name,
    }, None


def _read_poll_points(poll_path: Path) -> list[tuple[float, int]]:
    """Read valid ``unix_time counter`` samples from a poll artifact."""
    points: list[tuple[float, int]] = []
    for line in poll_path.read_text(errors="replace").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            points.append((float(parts[0]), int(parts[1])))
        except ValueError:
            continue
    return points


def _counter_rate(
    poll_points: list[tuple[float, int]], start: float, end: float
) -> float | None:
    """Fit an interior counter slope in operations per second."""
    points = [(x, y) for x, y in poll_points if start <= x <= end]
    if len(points) < 8:
        return None
    count = len(points)
    mean_x = sum(x for x, _ in points) / count
    mean_y = sum(y for _, y in points) / count
    denom = sum((x - mean_x) ** 2 for x, _ in points)
    if denom <= 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in points) / denom


def build_report(
    run_dir: Path,
    expected_initiators: int,
    data_size_kb: int,
    poll_path: Path,
    trim_sec: float,
    max_start_skew_sec: float,
    counter_segment_bytes: float,
    counter_tolerance: float,
    counters: dict[str, int],
    corpus_before: int,
    corpus_after: int,
) -> dict[str, Any]:
    """Build an aggregate result and its acceptance decision.

    The application side is the sum of each worker's successful-byte rate.
    The RDMA side is a least-squares slope over only the time all workers'
    measured windows overlap. This avoids attributing setup or discarded
    warmup traffic to the aggregate result.
    """
    if expected_initiators <= 0:
        raise ValueError("expected_initiators must be positive")
    if data_size_kb <= 0:
        raise ValueError("data_size_kb must be positive")
    if trim_sec < 0:
        raise ValueError("trim_sec must not be negative")
    if counter_segment_bytes <= 0:
        raise ValueError("counter_segment_bytes must be positive")

    data_size_bytes = data_size_kb * 1024
    failures: list[str] = []
    workers: list[dict[str, Any]] = []
    for worker_id in range(expected_initiators):
        worker, failure = _load_worker(run_dir, worker_id, data_size_bytes)
        if failure is not None:
            failures.append(failure)
        elif worker is not None:
            workers.append(worker)

    for worker in workers:
        worker_id = worker["initiator_id"]
        if worker["exit_code"] != 0:
            failures.append(f"initiator {worker_id}: exit {worker['exit_code']}")
        if worker["mode"] != "sustained":
            failures.append(f"initiator {worker_id}: mode is not sustained")
        if worker["window_sec"] <= 0:
            failures.append(f"initiator {worker_id}: invalid measured window")
        if worker["total_keys"] == 0:
            failures.append(f"initiator {worker_id}: no completed keys")
        if worker["total_success"] != worker["total_keys"]:
            failures.append(
                f"initiator {worker_id}: successful keys "
                f"{worker['total_success']}/{worker['total_keys']}"
            )
        if worker["timed_out"]:
            failures.append(f"initiator {worker_id}: timed out")
        if worker["started_at_unix_sec"] is None:
            failures.append(f"initiator {worker_id}: missing window marker")

    if corpus_after != corpus_before:
        failures.append(f"corpus changed: {corpus_before} -> {corpus_after}")
    for counter in _ERROR_COUNTERS:
        if counters.get(counter, 0) != 0:
            failures.append(f"{counter} advanced by {counters[counter]}")

    report: dict[str, Any] = {
        "classification": (
            "existing-controller local multi-process fs_native read-only "
            "functional evidence; not physical multi-initiator, MP, "
            "controller, QP, R2, 400 GbE, or Falcon-offload evidence"
        ),
        "expected_initiators": expected_initiators,
        "data_size_kb": data_size_kb,
        "workers": workers,
        "counters": counters,
        "corpus_files_before": corpus_before,
        "corpus_files_after": corpus_after,
        "acceptance_failures": failures,
    }

    if len(workers) != expected_initiators or any(
        worker["started_at_unix_sec"] is None or worker["window_sec"] <= 0
        for worker in workers
    ):
        report["accepted"] = False
        return report

    starts = [float(worker["started_at_unix_sec"]) for worker in workers]
    ends = [
        float(worker["started_at_unix_sec"]) + float(worker["window_sec"])
        for worker in workers
    ]
    first_start = min(starts)
    last_start = max(starts)
    common_start = last_start
    common_end = min(ends)
    common_window_sec = common_end - common_start
    start_skew_sec = last_start - first_start
    report["first_window_start_unix_sec"] = first_start
    report["last_window_start_unix_sec"] = last_start
    report["window_start_skew_sec"] = start_skew_sec
    report["common_window_sec"] = common_window_sec
    if start_skew_sec > max_start_skew_sec:
        failures.append(
            f"window-start skew {start_skew_sec:.3f}s exceeds {max_start_skew_sec:.3f}s"
        )

    app_success_bytes = sum(int(worker["success_bytes"]) for worker in workers)
    app_rate_bytes_sec = sum(
        int(worker["success_bytes"]) / float(worker["window_sec"]) for worker in workers
    )
    report["aggregate_success_bytes"] = app_success_bytes
    report["aggregate_success_gbps"] = app_rate_bytes_sec * 8 / 1e9

    interior_start = common_start + trim_sec
    interior_end = common_end - trim_sec
    report["counter_interior_start_unix_sec"] = interior_start
    report["counter_interior_end_unix_sec"] = interior_end
    if interior_end <= interior_start:
        failures.append("overlapping measured window is too short after trim")
        report["accepted"] = False
        return report

    try:
        poll_points = _read_poll_points(poll_path)
    except OSError as exc:
        failures.append(f"cannot read counter poll: {exc}")
        report["accepted"] = False
        return report
    counter_ops_per_sec = _counter_rate(poll_points, interior_start, interior_end)
    if counter_ops_per_sec is None:
        failures.append("fewer than eight usable interior counter samples")
        report["accepted"] = False
        return report

    expected_ops_per_sec = app_rate_bytes_sec / counter_segment_bytes
    ratio = counter_ops_per_sec / expected_ops_per_sec if expected_ops_per_sec else 0.0
    report["counter_ops_per_sec"] = counter_ops_per_sec
    report["expected_counter_ops_per_sec"] = expected_ops_per_sec
    report["counter_rate_gbps"] = counter_ops_per_sec * counter_segment_bytes * 8 / 1e9
    report["counter_app_rate_ratio"] = ratio
    if not (1 - counter_tolerance) <= ratio <= (1 + counter_tolerance):
        failures.append(
            f"counter/app-rate ratio {ratio:.4f} outside +/-{counter_tolerance:.0%}"
        )

    report["accepted"] = not failures
    return report


def _parse_counter(value: str) -> tuple[str, int]:
    """Parse one ``NAME=DELTA`` command-line counter value."""
    name, sep, raw = value.partition("=")
    if not sep or not name:
        raise argparse.ArgumentTypeError("counter must use NAME=INTEGER")
    try:
        return name, int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("counter delta must be an integer") from exc


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse the aggregate report command line."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--initiators", type=int, required=True)
    parser.add_argument("--data-size-kb", type=int, required=True)
    parser.add_argument("--poll", type=Path, required=True)
    parser.add_argument("--trim-sec", type=float, default=3.0)
    parser.add_argument("--max-start-skew-sec", type=float, default=2.0)
    parser.add_argument("--counter-segment-bytes", type=float, default=52428.0)
    parser.add_argument("--counter-tolerance", type=float, default=0.05)
    parser.add_argument("--counter", action="append", type=_parse_counter, default=[])
    parser.add_argument("--corpus-before", type=int, required=True)
    parser.add_argument("--corpus-after", type=int, required=True)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Write the aggregate report and return nonzero for a rejected run."""
    args = parse_args(argv)
    report = build_report(
        run_dir=args.run_dir,
        expected_initiators=args.initiators,
        data_size_kb=args.data_size_kb,
        poll_path=args.poll,
        trim_sec=args.trim_sec,
        max_start_skew_sec=args.max_start_skew_sec,
        counter_segment_bytes=args.counter_segment_bytes,
        counter_tolerance=args.counter_tolerance,
        counters=dict(args.counter),
        corpus_before=args.corpus_before,
        corpus_after=args.corpus_after,
    )
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    aggregate_gbps = report.get("aggregate_success_gbps", 0.0)
    print(f"aggregate success rate: {aggregate_gbps:.2f} Gbps")
    print(f"window-start skew: {report.get('window_start_skew_sec', 0.0):.3f} s")
    print(f"counter/app ratio: {report.get('counter_app_rate_ratio', 0.0):.4f}")
    if report["accepted"]:
        print("RESULT: ACCEPTED — existing-controller local multi-process read")
        return 0
    print("RESULT: REJECTED")
    for failure in report["acceptance_failures"]:
        print(f"  - {failure}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
