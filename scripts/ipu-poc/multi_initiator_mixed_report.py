#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Aggregate a local multi-process fs_native sustained mixed read/write run."""

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
_COUNTER_SEGMENT_BYTES = {
    "InRdmaWrites": 52428.0,
    "InRdmaReads": 4096.0,
}


def _parse_ratio(value: str) -> float:
    """Parse a ``READ:WRITE`` ratio into its read-per-write byte factor.

    Args:
        value: A ratio such as ``5:1`` or ``9:1``, matching the spelling
            ``bench l2 --read-write-ratio`` accepts.

    Returns:
        Read bytes per write byte, e.g. ``5.0`` for ``5:1``.

    Raises:
        ValueError: If the value is not two positive numbers separated by a
            colon.
    """
    read_part, sep, write_part = value.partition(":")
    if not sep:
        raise ValueError(f"ratio must be READ:WRITE, got {value!r}")
    read = float(read_part)
    write = float(write_part)
    if read <= 0 or write <= 0:
        raise ValueError(f"ratio parts must be positive, got {value!r}")
    return read / write


def _worker_start(log_path: Path) -> float | None:
    """Return the timestamped mixed measured-window start from *log_path*."""
    try:
        for line in log_path.read_text(errors="replace").splitlines():
            if "[Mixed] Sustained window" in line:
                return float(line.split(maxsplit=1)[0])
    except (OSError, ValueError):
        return None
    return None


def _operation(metrics: dict[str, Any], name: str) -> dict[str, Any]:
    """Return the sole operation section named *name*."""
    return next(
        section
        for section in metrics.values()
        if isinstance(section, dict) and section.get("operation") == name
    )


def _load_worker(
    run_dir: Path, worker_id: int, data_size_bytes: int
) -> tuple[dict[str, Any] | None, str | None]:
    """Load one mixed worker's JSON result and timing metadata."""
    json_path = run_dir / f"initiator-{worker_id}.json"
    log_path = run_dir / f"initiator-{worker_id}.log"
    status_path = run_dir / f"initiator-{worker_id}.status"
    if not json_path.is_file() or not status_path.is_file():
        return None, f"initiator {worker_id}: missing result or status"
    try:
        result = json.loads(json_path.read_text())
        metrics = result["metrics"]
        config = metrics["config"]
        mixed = metrics["mixed"]
        load = _operation(metrics, "Load")
        store = _operation(metrics, "Store")
        return {
            "initiator_id": worker_id,
            "exit_code": int(status_path.read_text().strip()),
            "mode": config["mode"],
            "ratio_requested": config["read_write_ratio_requested"],
            "write_key_prefix": config["write_key_prefix"],
            "started_at_unix_sec": _worker_start(log_path),
            "read_window_sec": float(load["window_sec"]),
            "write_window_sec": float(store["window_sec"]),
            "read_keys": int(load["total_keys"]),
            "read_success": int(load["total_success"]),
            "write_keys": int(store["total_keys"]),
            "write_success": int(store["total_success"]),
            "read_success_bytes": int(mixed["read_success_bytes"]),
            "write_success_bytes": int(mixed["write_success_bytes"]),
            "ratio_achieved": float(mixed["read_write_ratio_achieved"]),
            "timed_out": bool(load.get("timed_out")) or bool(store.get("timed_out")),
            "result_path": json_path.name,
            "log_path": log_path.name,
        }, None
    except (
        KeyError,
        OSError,
        StopIteration,
        ValueError,
        TypeError,
        json.JSONDecodeError,
    ) as exc:
        return None, f"initiator {worker_id}: invalid result ({exc})"


def _read_poll(path: Path) -> list[tuple[float, int, int]]:
    """Read valid ``time InRdmaWrites InRdmaReads`` poll samples."""
    points: list[tuple[float, int, int]] = []
    for line in path.read_text(errors="replace").splitlines():
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            points.append((float(parts[0]), int(parts[1]), int(parts[2])))
        except ValueError:
            continue
    return points


def _counter_rate(
    points: list[tuple[float, int, int]],
    start: float,
    end: float,
    index: int,
) -> float | None:
    """Fit one RDMA counter's interior slope in operations per second."""
    interior = [(row[0], row[index]) for row in points if start <= row[0] <= end]
    if len(interior) < 8:
        return None
    count = len(interior)
    mean_x = sum(x for x, _ in interior) / count
    mean_y = sum(y for _, y in interior) / count
    denom = sum((x - mean_x) ** 2 for x, _ in interior)
    if denom <= 0:
        return None
    return sum((x - mean_x) * (y - mean_y) for x, y in interior) / denom


def build_report(
    run_dir: Path,
    expected_initiators: int,
    data_size_kb: int,
    poll_path: Path,
    trim_sec: float,
    max_start_skew_sec: float,
    requested_ratio: str,
    ratio_tolerance: float,
    counter_tolerance: float,
    counters: dict[str, int],
    corpus_before: int,
    corpus_after: int,
) -> dict[str, Any]:
    """Build the mixed aggregate report and its acceptance decision."""
    if expected_initiators <= 0 or data_size_kb <= 0:
        raise ValueError("initiators and data_size_kb must be positive")
    if trim_sec < 0 or ratio_tolerance < 0 or counter_tolerance < 0:
        raise ValueError("tolerances and trim_sec must not be negative")
    expected_ratio = _parse_ratio(requested_ratio)

    data_size_bytes = data_size_kb * 1024
    workers: list[dict[str, Any]] = []
    failures: list[str] = []
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
        if worker["ratio_requested"] != requested_ratio:
            failures.append(
                f"initiator {worker_id}: requested ratio is "
                f"{worker['ratio_requested']}, expected {requested_ratio}"
            )
        if worker["read_keys"] == 0 or worker["write_keys"] == 0:
            failures.append(f"initiator {worker_id}: no completed read or write keys")
        if worker["read_success"] != worker["read_keys"]:
            failures.append(f"initiator {worker_id}: read success mismatch")
        if worker["write_success"] != worker["write_keys"]:
            failures.append(f"initiator {worker_id}: write success mismatch")
        if worker["timed_out"]:
            failures.append(f"initiator {worker_id}: timed out")
        if worker["started_at_unix_sec"] is None:
            failures.append(f"initiator {worker_id}: missing mixed window marker")
        if worker["read_window_sec"] <= 0 or worker["write_window_sec"] <= 0:
            failures.append(f"initiator {worker_id}: invalid measured window")

    if corpus_after != corpus_before:
        failures.append(f"read corpus changed: {corpus_before} -> {corpus_after}")
    for counter in _ERROR_COUNTERS:
        if counter not in counters:
            failures.append(f"missing error-counter delta: {counter}")
        elif counters[counter] != 0:
            failures.append(f"{counter} advanced by {counters[counter]}")

    report: dict[str, Any] = {
        "classification": (
            "existing-controller local multi-process fs_native mixed "
            "read/write functional evidence; not physical multi-initiator, "
            "MP, controller, QP, R2, 400 GbE, or Falcon-offload evidence"
        ),
        "expected_initiators": expected_initiators,
        "data_size_kb": data_size_kb,
        "requested_read_write_ratio": requested_ratio,
        "requested_read_fraction": expected_ratio / (expected_ratio + 1),
        "workers": workers,
        "counters": counters,
        "corpus_files_before": corpus_before,
        "corpus_files_after": corpus_after,
        "counter_correlation": {
            "acceptance_role": "non-gating corroboration",
            "reason": (
                "The RDMA slope is fit over the trimmed common worker "
                "window, while application bytes are only available as "
                "whole-worker totals. Without timestamped application "
                "progress, their rates are not interval-aligned enough "
                "for a byte-for-byte acceptance gate."
            ),
            "directions": {},
        },
        "acceptance_failures": failures,
    }
    if len(workers) != expected_initiators or any(
        worker["started_at_unix_sec"] is None
        or worker["read_window_sec"] <= 0
        or worker["write_window_sec"] <= 0
        for worker in workers
    ):
        report["accepted"] = False
        return report

    starts = [float(worker["started_at_unix_sec"]) for worker in workers]
    ends = [
        float(worker["started_at_unix_sec"]) + float(worker["read_window_sec"])
        for worker in workers
    ]
    common_start = max(starts)
    common_end = min(ends)
    report["window_start_skew_sec"] = common_start - min(starts)
    report["common_window_sec"] = common_end - common_start
    if report["window_start_skew_sec"] > max_start_skew_sec:
        failures.append(
            f"window-start skew {report['window_start_skew_sec']:.3f}s exceeds "
            f"{max_start_skew_sec:.3f}s"
        )

    read_bytes = sum(int(worker["read_success_bytes"]) for worker in workers)
    write_bytes = sum(int(worker["write_success_bytes"]) for worker in workers)
    read_rate = sum(
        int(worker["read_success_bytes"]) / float(worker["read_window_sec"])
        for worker in workers
    )
    write_rate = sum(
        int(worker["write_success_bytes"]) / float(worker["write_window_sec"])
        for worker in workers
    )
    achieved_ratio = read_bytes / write_bytes if write_bytes else 0.0
    report.update(
        aggregate_read_success_bytes=read_bytes,
        aggregate_write_success_bytes=write_bytes,
        aggregate_read_gbps=read_rate * 8 / 1e9,
        aggregate_write_gbps=write_rate * 8 / 1e9,
        aggregate_success_gbps=(read_rate + write_rate) * 8 / 1e9,
        achieved_read_write_ratio=achieved_ratio,
        # Read fraction of total bytes, the form fio reports as rwmixread, so an
        # L2 cell and a fio cell at the same ratio are compared in one unit
        # without converting either. 5:1 -> 83.33%, 9:1 -> 90.00%.
        achieved_read_fraction=(
            read_bytes / (read_bytes + write_bytes) if read_bytes + write_bytes else 0.0
        ),
    )
    if (
        not expected_ratio * (1 - ratio_tolerance)
        <= achieved_ratio
        <= expected_ratio * (1 + ratio_tolerance)
    ):
        failures.append(
            f"achieved ratio {achieved_ratio:.4f} outside {requested_ratio} "
            f"+/-{ratio_tolerance:.0%}"
        )

    interior_start = common_start + trim_sec
    interior_end = common_end - trim_sec
    if interior_end <= interior_start:
        failures.append("overlapping measured window is too short after trim")
        report["accepted"] = False
        return report
    try:
        points = _read_poll(poll_path)
    except OSError as exc:
        failures.append(f"cannot read counter poll: {exc}")
        report["accepted"] = False
        return report

    correlation = report["counter_correlation"]["directions"]
    for counter, index, rate, direction in (
        ("InRdmaWrites", 1, read_rate, "read"),
        ("InRdmaReads", 2, write_rate, "write"),
    ):
        observed = _counter_rate(points, interior_start, interior_end, index)
        if observed is None:
            correlation[direction] = {
                "status": "unavailable",
                "reason": "fewer than eight interior counter samples",
            }
            continue
        expected = rate / _COUNTER_SEGMENT_BYTES[counter]
        ratio = observed / expected if expected else 0.0
        report[f"{direction}_counter_ops_per_sec"] = observed
        report[f"{direction}_expected_counter_ops_per_sec"] = expected
        report[f"{direction}_counter_app_rate_ratio"] = ratio
        correlation[direction] = {
            "status": (
                "within_tolerance"
                if (1 - counter_tolerance) <= ratio <= (1 + counter_tolerance)
                else "outside_tolerance"
            ),
            "observed_ops_per_sec": observed,
            "whole_worker_expected_ops_per_sec": expected,
            "ratio": ratio,
        }

    report["accepted"] = not failures
    return report


def _parse_counter(value: str) -> tuple[str, int]:
    """Parse one ``NAME=DELTA`` command-line value."""
    name, sep, raw = value.partition("=")
    if not sep or not name:
        raise argparse.ArgumentTypeError("counter must use NAME=INTEGER")
    try:
        return name, int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("counter delta must be an integer") from exc


def main(argv: list[str]) -> int:
    """Write an aggregate mixed report and return nonzero when rejected."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--initiators", type=int, required=True)
    parser.add_argument("--data-size-kb", type=int, required=True)
    parser.add_argument("--poll", type=Path, required=True)
    parser.add_argument("--trim-sec", type=float, default=3.0)
    parser.add_argument("--max-start-skew-sec", type=float, default=2.0)
    parser.add_argument(
        "--requested-ratio",
        default="5:1",
        help="READ:WRITE ratio the run requested; must match every worker's "
        "recorded config (default: 5:1).",
    )
    parser.add_argument("--ratio-tolerance", type=float, default=0.01)
    parser.add_argument("--counter-tolerance", type=float, default=0.05)
    parser.add_argument("--counter", action="append", type=_parse_counter, default=[])
    parser.add_argument("--corpus-before", type=int, required=True)
    parser.add_argument("--corpus-after", type=int, required=True)
    args = parser.parse_args(argv)
    report = build_report(
        run_dir=args.run_dir,
        expected_initiators=args.initiators,
        data_size_kb=args.data_size_kb,
        poll_path=args.poll,
        trim_sec=args.trim_sec,
        max_start_skew_sec=args.max_start_skew_sec,
        requested_ratio=args.requested_ratio,
        ratio_tolerance=args.ratio_tolerance,
        counter_tolerance=args.counter_tolerance,
        counters=dict(args.counter),
        corpus_before=args.corpus_before,
        corpus_after=args.corpus_after,
    )
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(f"read goodput: {report.get('aggregate_read_gbps', 0.0):.2f} Gbps")
    print(f"write goodput: {report.get('aggregate_write_gbps', 0.0):.2f} Gbps")
    print(
        f"achieved ratio: {report.get('achieved_read_write_ratio', 0.0):.4f}:1"
        f" (requested {args.requested_ratio},"
        f" read fraction {report.get('achieved_read_fraction', 0.0) * 100:.2f}%)"
    )
    if report["accepted"]:
        print("RESULT: ACCEPTED — existing-controller local multi-process mixed")
        return 0
    print("RESULT: REJECTED")
    for failure in report["acceptance_failures"]:
        print(f"  - {failure}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
