#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Aggregate an N-initiator fs_native run at model-page geometry.

Why this is separate from ``multi_initiator_mixed_report.py``: that script is
the historical 28 MiB aggregator and hardcodes ``InRdmaWrites = 52428`` bytes
per op, a constant calibrated against 28 MiB objects. At 144 KiB the measured
value is ~45370 -- 13.5% different -- so inheriting it would bias every
counter check. It also pins the ratio to 5:1 and the worker count to 2. Here
both segment constants and the ratio are parameters, and the initiator count is
free.

Handles both shapes of cell:

  * ``--mode read``  -- every initiator runs ``--only load`` over a shared read
    corpus. Application bytes come from ``total_success * page_kb * 1024``.
  * ``--mode mixed`` -- every initiator runs a read/write ratio, reading the
    shared corpus and writing to its own distinct prefix. Application bytes
    come from the ``mixed`` section's own read/write byte totals.

Acceptance gates (all must hold):
  1. every expected initiator produced a result and exited zero;
  2. every key succeeded, on both directions where mixed;
  3. no timeout, retransmit, NAK, RTO, RNR, out-of-order or proto error;
  4. the shared read corpus file count is unchanged;
  5. released-together: window-start skew within ``--max-start-skew-sec``;
  6. each initiator's reported throughput matches bytes/window within 1%;
  7. mixed only: achieved byte ratio within tolerance of the requested one;
  8. every initiator's emitted geometry equals the profile the driver resolved
     -- SHA-256, page bytes, and objects per submit, plus ``config.num_keys``;
  9. an RDMA counter slope was actually measurable: the read direction always,
     and the write direction as well when mixed.

Gate 9 gates AVAILABILITY only. The slope's agreement with application bytes
stays corroborative: it is fit over the trimmed common window while application
bytes are only available as whole-initiator totals, so the two rates are not
interval-aligned tightly enough to gate the ratio on. But an unavailable slope
is a different thing from a slope that merely disagrees -- it means the cell has
no independent wire-level witness at all, which is exactly the case that must
not pass silently.
"""

# Future
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

# result.py defines _MB = 1024 * 1024, so the bench's throughput fields are
# MiB/s. Converting as decimal MB/s understates every figure by 4.86%.
MIB_S_TO_GBPS = 2**20 * 8 / 1e9
# Reported-vs-derived goodput agreement. Two independent derivations of one
# quantity; a wider gap means a units, window, or success-set change.
GOODPUT_TOL = 0.01


def _window_start(log_path: Path, marker: str) -> float | None:
    """Return the timestamped measured-window start from *log_path*.

    Args:
        log_path: Timestamp-prefixed initiator log.
        marker: Substring identifying the window-start line.

    Returns:
        Unix seconds, or None when the marker is absent or unparseable.
    """
    try:
        for line in log_path.read_text(errors="replace").splitlines():
            if marker in line:
                return float(line.split(maxsplit=1)[0])
    except (OSError, ValueError):
        return None
    return None


def _operation(metrics: dict[str, Any], name: str) -> dict[str, Any]:
    """Return the sole operation section named *name*.

    Args:
        metrics: The result's ``metrics`` mapping.
        name: Operation name, ``Load`` or ``Store``.

    Returns:
        That operation's section.

    Raises:
        StopIteration: If no section carries the name.
    """
    return next(
        section
        for section in metrics.values()
        if isinstance(section, dict) and section.get("operation") == name
    )


def _load_initiator(
    run_dir: Path, worker_id: int, mode: str, page_kb: int, marker: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Load one initiator's result, or a reason it is unusable.

    Args:
        run_dir: Directory holding ``initiator-<id>.{json,log,status}``.
        worker_id: Initiator index.
        mode: ``read`` or ``mixed``.
        page_kb: Page size in KiB, used to derive read-only app bytes.
        marker: Window-start log marker for this mode.

    Returns:
        ``(record, None)`` on success, ``(None, reason)`` otherwise.
    """
    json_path = run_dir / f"initiator-{worker_id}.json"
    log_path = run_dir / f"initiator-{worker_id}.log"
    status_path = run_dir / f"initiator-{worker_id}.status"
    if not json_path.is_file() or not status_path.is_file():
        return None, f"initiator {worker_id}: missing result or status"
    try:
        metrics = json.loads(json_path.read_text())["metrics"]
        config = metrics["config"]
        load = _operation(metrics, "Load")
        # The bench emits a "geometry" section only when a profile or shape spec
        # resolved the shape. Its absence is therefore itself the signal that
        # this initiator ran the old flat geometry, so read it with .get and let
        # the caller's gate reject rather than raising a KeyError here.
        geometry = metrics.get("geometry") or {}
        record: dict[str, Any] = {
            "initiator_id": worker_id,
            "exit_code": int(status_path.read_text().strip()),
            "mode": config["mode"],
            "num_keys": config.get("num_keys"),
            "geometry_profile_sha256": geometry.get("profile_sha256"),
            "geometry_page_size_bytes": geometry.get("page_size_bytes"),
            "geometry_objects_per_submit": geometry.get("objects_per_submit"),
            "started_at_unix_sec": _window_start(log_path, marker),
            "read_window_sec": float(load["window_sec"]),
            "read_keys": int(load["total_keys"]),
            "read_success": int(load["total_success"]),
            "read_reported_gbps": float(load.get("throughput_aggregate_mbps") or 0.0)
            * MIB_S_TO_GBPS,
            "read_submit_latency_ms": load.get("submit_latency_avg_ms"),
            "timed_out": bool(load.get("timed_out")),
            "result_path": json_path.name,
        }
        if mode == "mixed":
            store = _operation(metrics, "Store")
            mixed = metrics["mixed"]
            record.update(
                ratio_requested=config["read_write_ratio_requested"],
                write_key_prefix=config["write_key_prefix"],
                write_window_sec=float(store["window_sec"]),
                write_keys=int(store["total_keys"]),
                write_success=int(store["total_success"]),
                read_success_bytes=int(mixed["read_success_bytes"]),
                write_success_bytes=int(mixed["write_success_bytes"]),
                ratio_achieved=float(mixed["read_write_ratio_achieved"]),
            )
            record["timed_out"] = record["timed_out"] or bool(store.get("timed_out"))
        else:
            # Read-only cells have no mixed section, so derive app bytes the
            # same way geom_report.py does: a "key" is one page, not one submit.
            record.update(
                read_success_bytes=int(load["total_success"]) * page_kb * 1024,
                write_success_bytes=0,
                write_window_sec=0.0,
            )
        return record, None
    except (
        KeyError,
        OSError,
        StopIteration,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        return None, f"initiator {worker_id}: invalid result ({exc})"


def _read_poll(path: Path) -> list[tuple[float, int, int]]:
    """Read valid ``time InRdmaWrites InRdmaReads`` samples from *path*.

    Args:
        path: Poll file written alongside the run.

    Returns:
        Parsed samples; malformed lines are skipped.
    """
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


def _counter_slope(
    points: list[tuple[float, int, int]], start: float, end: float, index: int
) -> float | None:
    """Least-squares slope of one counter over the interior window.

    A two-point bracket undercounts because the irdma ``hw_counters`` refresh
    lag is asymmetric across the window edges; the interior fit avoids that.

    Args:
        points: Poll samples.
        start: Interior window start, unix seconds.
        end: Interior window end, unix seconds.
        index: Tuple index of the counter, 1 or 2.

    Returns:
        Operations per second, or None with fewer than eight interior samples
        or a degenerate time base.
    """
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
    mode: str,
    page_kb: int,
    poll_path: Path,
    read_seg_bytes: float,
    write_seg_bytes: float | None,
    expected_ratio: float,
    trim_sec: float,
    max_start_skew_sec: float,
    ratio_tolerance: float,
    counter_tolerance: float,
    counters: dict[str, int],
    corpus_before: int,
    corpus_after: int,
    expected_profile_sha256: str,
    expected_page_size_bytes: int,
    expected_objects_per_submit: int,
) -> dict[str, Any]:
    """Build the aggregate report and its acceptance decision.

    Args:
        run_dir: Directory holding the per-initiator artifacts.
        expected_initiators: How many initiators must have reported.
        mode: ``read`` or ``mixed``.
        page_kb: Page size in KiB.
        poll_path: RDMA counter poll file.
        read_seg_bytes: Bytes per ``InRdmaWrites`` op at this geometry.
        write_seg_bytes: Bytes per ``InRdmaReads`` op at this geometry. Required
            under ``mixed``; None when reading, which drives no writes.
        expected_ratio: Requested read:write byte ratio; ignored when reading.
        trim_sec: Seconds trimmed from each end before fitting counters.
        max_start_skew_sec: Largest tolerated barrier release skew.
        ratio_tolerance: Fractional tolerance on the achieved ratio.
        counter_tolerance: Fractional tolerance on counter corroboration.
        counters: Whole-run counter deltas by name.
        corpus_before: Shared read corpus file count before the run.
        corpus_after: Shared read corpus file count after the run.
        expected_profile_sha256: SHA-256 of the profile the driver resolved.
        expected_page_size_bytes: Page size the driver resolved, in bytes.
        expected_objects_per_submit: Objects per submit the driver resolved.

    Returns:
        The report mapping, including ``accepted`` and ``acceptance_failures``.

    Raises:
        ValueError: On a nonsensical argument combination.
    """
    if expected_initiators <= 0 or page_kb <= 0:
        raise ValueError("initiators and page_kb must be positive")
    if read_seg_bytes <= 0 or (write_seg_bytes is not None and write_seg_bytes <= 0):
        raise ValueError("segment sizes must be positive")
    # No default: the write constant is geometry-specific (4083.9 at 61 x 144
    # KiB, not the 4096.0 a page-sized guess would give), and a mixed cell that
    # quoted an uncalibrated constant would report a write-side correlation
    # scoped to the wrong geometry. A read cell never drives the direction, so
    # it legitimately has none.
    if mode == "mixed" and write_seg_bytes is None:
        raise ValueError(
            "mixed mode requires write_seg_bytes calibrated at this geometry"
        )
    if mode not in ("read", "mixed"):
        raise ValueError(f"mode must be read or mixed, got {mode!r}")
    if trim_sec < 0 or ratio_tolerance < 0 or counter_tolerance < 0:
        raise ValueError("tolerances and trim_sec must not be negative")
    if expected_objects_per_submit <= 0 or expected_page_size_bytes <= 0:
        raise ValueError("expected geometry values must be positive")
    if len(expected_profile_sha256) != 64:
        raise ValueError("expected_profile_sha256 must be a 64-hex-digit digest")
    # page_kb drives the read-only app-byte derivation while page_size_bytes
    # drives the geometry gate. If they disagree the two halves of this report
    # describe different objects, so refuse before measuring anything.
    if expected_page_size_bytes != page_kb * 1024:
        raise ValueError(
            f"page_kb {page_kb} KiB does not match expected page "
            f"{expected_page_size_bytes} bytes"
        )

    marker = "[Mixed] Sustained window" if mode == "mixed" else "Sustained window"
    initiators: list[dict[str, Any]] = []
    failures: list[str] = []
    for worker_id in range(expected_initiators):
        record, failure = _load_initiator(run_dir, worker_id, mode, page_kb, marker)
        if failure is not None:
            failures.append(failure)
        elif record is not None:
            initiators.append(record)

    for rec in initiators:
        wid = rec["initiator_id"]
        if rec["exit_code"] != 0:
            failures.append(f"initiator {wid}: exit {rec['exit_code']}")
        if rec["mode"] != "sustained":
            failures.append(f"initiator {wid}: mode is {rec['mode']}, not sustained")
        if rec["timed_out"]:
            failures.append(f"initiator {wid}: timed out")
        if rec["started_at_unix_sec"] is None:
            failures.append(f"initiator {wid}: missing sustained window marker")
        if rec["read_window_sec"] <= 0:
            failures.append(f"initiator {wid}: read window is {rec['read_window_sec']}")
        if rec["read_keys"] == 0 or rec["read_success"] != rec["read_keys"]:
            failures.append(
                f"initiator {wid}: reads {rec['read_success']}/{rec['read_keys']}"
            )
        # The geometry resolver drove these runs, so the shape the initiator
        # emitted must be the shape the driver resolved -- not merely present.
        # A same-page-size different profile, or a profile edited between the
        # corpus build and this cell, would otherwise pass on the strength of
        # having *some* geometry, and the cell would report the driver's SHA
        # over bytes laid out to a different one.
        if rec["num_keys"] != expected_objects_per_submit:
            failures.append(
                f"initiator {wid}: config.num_keys {rec['num_keys']} != profile "
                f"objects/submit {expected_objects_per_submit}"
            )
        if rec["geometry_profile_sha256"] != expected_profile_sha256:
            failures.append(
                f"initiator {wid}: geometry profile sha256 "
                f"{rec['geometry_profile_sha256']} != expected "
                f"{expected_profile_sha256}"
            )
        if rec["geometry_page_size_bytes"] != expected_page_size_bytes:
            failures.append(
                f"initiator {wid}: geometry page {rec['geometry_page_size_bytes']} "
                f"!= expected {expected_page_size_bytes} bytes"
            )
        if rec["geometry_objects_per_submit"] != expected_objects_per_submit:
            failures.append(
                f"initiator {wid}: geometry objects/submit "
                f"{rec['geometry_objects_per_submit']} != expected "
                f"{expected_objects_per_submit}"
            )
        # Each initiator's own two goodput derivations must agree, which
        # catches a units or window-definition change per process rather than
        # only in the aggregate.
        if rec["read_window_sec"] > 0 and rec["read_reported_gbps"] > 0:
            derived = rec["read_success_bytes"] * 8 / rec["read_window_sec"] / 1e9
            rec["read_derived_gbps"] = derived
            gap = abs(rec["read_reported_gbps"] - derived) / derived if derived else 1.0
            rec["read_goodput_gap"] = gap
            if gap > GOODPUT_TOL:
                failures.append(
                    f"initiator {wid}: reported {rec['read_reported_gbps']:.2f} vs "
                    f"derived {derived:.2f} Gbps differ by {gap:.2%}"
                )
        if mode == "mixed":
            if rec["write_window_sec"] <= 0:
                failures.append(f"initiator {wid}: write window is not positive")
            if rec["write_keys"] == 0 or rec["write_success"] != rec["write_keys"]:
                w_ok, w_all = rec["write_success"], rec["write_keys"]
                failures.append(f"initiator {wid}: writes {w_ok}/{w_all}")

    if corpus_after != corpus_before:
        failures.append(
            f"shared read corpus changed: {corpus_before} -> {corpus_after}"
        )
    for counter in _ERROR_COUNTERS:
        if counter not in counters:
            failures.append(f"missing error-counter delta: {counter}")
        elif counters[counter] != 0:
            failures.append(f"{counter} advanced by {counters[counter]}")

    report: dict[str, Any] = {
        "classification": (
            "Falcon-offloaded kernel NVMe-oF, existing-controller, local "
            "multi-process fs_native sustained load at model-page geometry, "
            "O_DIRECT re-read corpus. Does NOT quantify offload benefit (no "
            "unoffloaded control). NOT fresh-QP/64-QP, R2, physical "
            "multi-initiator, or 400 GbE evidence."
        ),
        "mode": mode,
        "expected_initiators": expected_initiators,
        "page_kb": page_kb,
        "expected_geometry": {
            "profile_sha256": expected_profile_sha256,
            "page_size_bytes": expected_page_size_bytes,
            "objects_per_submit": expected_objects_per_submit,
        },
        "read_seg_bytes": read_seg_bytes,
        "write_seg_bytes": write_seg_bytes,
        "initiators": initiators,
        "counters": counters,
        "corpus_files_before": corpus_before,
        "corpus_files_after": corpus_after,
        "counter_correlation": {
            "acceptance_role": "availability gated; agreement non-gating",
            "reason": (
                "A slope must be measurable on every direction the cell drove, "
                "because without one the cell has no independent wire-level "
                "witness at all. Its AGREEMENT with application bytes stays "
                "corroborative: the slope is fit over the trimmed common "
                "window while application bytes are only available as "
                "whole-initiator totals, so without timestamped application "
                "progress the two rates are not interval-aligned enough to "
                "gate the ratio on."
            ),
            "directions": {},
        },
        "acceptance_failures": failures,
    }

    if len(initiators) != expected_initiators or any(
        rec["started_at_unix_sec"] is None or rec["read_window_sec"] <= 0
        for rec in initiators
    ):
        report["accepted"] = False
        return report

    starts = [float(rec["started_at_unix_sec"]) for rec in initiators]
    ends = [
        float(rec["started_at_unix_sec"]) + float(rec["read_window_sec"])
        for rec in initiators
    ]
    common_start, common_end = max(starts), min(ends)
    report["window_start_skew_sec"] = common_start - min(starts)
    report["common_window_sec"] = common_end - common_start
    if report["window_start_skew_sec"] > max_start_skew_sec:
        failures.append(
            f"window-start skew {report['window_start_skew_sec']:.3f}s exceeds "
            f"{max_start_skew_sec:.3f}s"
        )

    # Per-initiator window, not the common window: each initiator's bytes were
    # earned over its own window, and summing rates is what aggregates.
    read_rate = sum(
        rec["read_success_bytes"] / rec["read_window_sec"] for rec in initiators
    )
    write_rate = sum(
        rec["write_success_bytes"] / rec["write_window_sec"]
        for rec in initiators
        if rec["write_window_sec"] > 0
    )
    read_bytes = sum(rec["read_success_bytes"] for rec in initiators)
    write_bytes = sum(rec["write_success_bytes"] for rec in initiators)
    report.update(
        aggregate_read_success_bytes=read_bytes,
        aggregate_write_success_bytes=write_bytes,
        aggregate_read_gbps=read_rate * 8 / 1e9,
        aggregate_write_gbps=write_rate * 8 / 1e9,
        aggregate_success_gbps=(read_rate + write_rate) * 8 / 1e9,
    )
    if mode == "mixed":
        achieved = read_bytes / write_bytes if write_bytes else 0.0
        report["achieved_read_write_ratio"] = achieved
        lo = expected_ratio * (1 - ratio_tolerance)
        hi = expected_ratio * (1 + ratio_tolerance)
        if not lo <= achieved <= hi:
            failures.append(
                f"achieved ratio {achieved:.4f} outside {expected_ratio:g}:1 "
                f"+/-{ratio_tolerance:.0%}"
            )

    interior_start = common_start + trim_sec
    interior_end = common_end - trim_sec
    if interior_end <= interior_start:
        failures.append("common window is too short after trim")
        report["accepted"] = not failures
        return report
    try:
        points = _read_poll(poll_path)
    except OSError as exc:
        failures.append(f"cannot read counter poll: {exc}")
        report["accepted"] = not failures
        return report

    directions = report["counter_correlation"]["directions"]
    for counter, index, rate, seg, name in (
        ("InRdmaWrites", 1, read_rate, read_seg_bytes, "read"),
        ("InRdmaReads", 2, write_rate, write_seg_bytes, "write"),
    ):
        if name == "write" and mode != "mixed":
            continue
        observed = _counter_slope(points, interior_start, interior_end, index)
        if observed is None:
            directions[name] = {
                "status": "unavailable",
                "reason": "fewer than eight interior samples or degenerate time base",
            }
            # Availability is a gate even though agreement is not. An
            # unavailable slope means the poller died, the window was too short
            # to fit, or the time base is degenerate -- the cell then rests on
            # the application's own accounting alone, with nothing from the wire
            # to contradict it. That is the shape of an unmeasured cell, not a
            # merely uncorroborated one.
            failures.append(
                f"no {name}-direction RDMA counter slope: {directions[name]['reason']}"
            )
            continue
        expected = rate / seg
        ratio = observed / expected if expected else 0.0
        directions[name] = {
            "status": (
                "within_tolerance"
                if (1 - counter_tolerance) <= ratio <= (1 + counter_tolerance)
                else "outside_tolerance"
            ),
            "observed_ops_per_sec": observed,
            "expected_ops_per_sec": expected,
            "ratio": ratio,
            "segment_bytes": seg,
        }

    report["accepted"] = not failures
    return report


def _parse_counter(value: str) -> tuple[str, int]:
    """Parse one ``NAME=DELTA`` command-line value.

    Args:
        value: Raw ``NAME=INTEGER`` text.

    Returns:
        The counter name and its integer delta.

    Raises:
        argparse.ArgumentTypeError: If the value is malformed.
    """
    name, sep, raw = value.partition("=")
    if not sep or not name:
        raise argparse.ArgumentTypeError("counter must use NAME=INTEGER")
    try:
        return name, int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("counter delta must be an integer") from exc


def main(argv: list[str]) -> int:
    """Write the aggregate report and return nonzero when rejected.

    Args:
        argv: Command-line arguments without the program name.

    Returns:
        Process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--initiators", type=int, required=True)
    parser.add_argument("--mode", choices=("read", "mixed"), required=True)
    parser.add_argument("--page-kb", type=int, required=True)
    parser.add_argument("--poll", type=Path, required=True)
    parser.add_argument("--read-seg-bytes", type=float, required=True)
    # Deliberately no default: 4096.0 is a page-sized guess, and the value
    # calibrated at 61 x 144 KiB is 4083.9. Mixed must supply it.
    parser.add_argument("--write-seg-bytes", type=float)
    parser.add_argument("--expected-ratio", type=float, default=5.0)
    parser.add_argument("--trim-sec", type=float, default=3.0)
    parser.add_argument("--max-start-skew-sec", type=float, default=2.0)
    parser.add_argument("--ratio-tolerance", type=float, default=0.01)
    parser.add_argument("--counter-tolerance", type=float, default=0.05)
    parser.add_argument("--counter", action="append", type=_parse_counter, default=[])
    parser.add_argument("--corpus-before", type=int, required=True)
    parser.add_argument("--corpus-after", type=int, required=True)
    # Required, not defaulted: the driver already resolved the profile to launch
    # the cell, so a default here could only serve to let a caller that did not
    # resolve one through the geometry gate.
    parser.add_argument("--expected-profile-sha256", required=True)
    parser.add_argument("--expected-page-size-bytes", type=int, required=True)
    parser.add_argument("--expected-objects-per-submit", type=int, required=True)
    args = parser.parse_args(argv)

    report = build_report(
        run_dir=args.run_dir,
        expected_initiators=args.initiators,
        mode=args.mode,
        page_kb=args.page_kb,
        poll_path=args.poll,
        read_seg_bytes=args.read_seg_bytes,
        write_seg_bytes=args.write_seg_bytes,
        expected_ratio=args.expected_ratio,
        trim_sec=args.trim_sec,
        max_start_skew_sec=args.max_start_skew_sec,
        ratio_tolerance=args.ratio_tolerance,
        counter_tolerance=args.counter_tolerance,
        counters=dict(args.counter),
        corpus_before=args.corpus_before,
        corpus_after=args.corpus_after,
        expected_profile_sha256=args.expected_profile_sha256,
        expected_page_size_bytes=args.expected_page_size_bytes,
        expected_objects_per_submit=args.expected_objects_per_submit,
    )
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")

    n = report["expected_initiators"]
    label = report["mode"].upper()
    print(f"\n===== {label} — {n} initiator(s), {args.page_kb} KiB =====")
    for rec in report.get("initiators", []):
        line = (
            f"  init {rec['initiator_id']}: read {rec['read_reported_gbps']:.2f} Gbps"
            f"  pages {rec['read_success']}/{rec['read_keys']}"
        )
        if report["mode"] == "mixed":
            line += f"  writes {rec.get('write_success')}/{rec.get('write_keys')}"
        print(line)
    print(f"  window skew          : {report.get('window_start_skew_sec', 0.0):.3f} s")
    print(f"  common window        : {report.get('common_window_sec', 0.0):.2f} s")
    print(f"  aggregate read       : {report.get('aggregate_read_gbps', 0.0):.2f} Gbps")
    if report["mode"] == "mixed":
        print(
            f"  aggregate write      : "
            f"{report.get('aggregate_write_gbps', 0.0):.2f} Gbps"
        )
        print(
            f"  aggregate read+write : "
            f"{report.get('aggregate_success_gbps', 0.0):.2f} Gbps"
        )
        print(
            f"  achieved ratio       : "
            f"{report.get('achieved_read_write_ratio', 0.0):.4f}:1"
        )
    for name, info in report["counter_correlation"]["directions"].items():
        if info.get("status") == "unavailable":
            print(
                f"  counter {name:<5}        : UNAVAILABLE ({info['reason']}) — gating"
            )
        else:
            print(
                f"  counter {name:<5}        : ratio {info['ratio']:.4f} "
                f"({info['status']}, seg {info['segment_bytes']:.1f}, "
                f"agreement non-gating)"
            )
    print(
        f"  corpus before/after  : {report['corpus_files_before']} / "
        f"{report['corpus_files_after']}"
    )

    if report["accepted"]:
        print("RESULT: ACCEPTED")
        print(f"  {report['classification']}")
        return 0
    print("RESULT: REJECTED")
    for failure in report["acceptance_failures"]:
        print(f"  - {failure}")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
