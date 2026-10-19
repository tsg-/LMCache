#!/usr/bin/env python3
"""Interior steady-state rate estimator for the InRdmaWrites bracket.

Estimates wire ops attributable to a sustained load's *measured window* by
least-squares fitting the counter's slope over the window interior, then
scaling that rate across the full window span.

Why not a simple two-point bracket at the window edges: irdma ``hw_counters``
refresh asynchronously, and the refresh lag is NOT symmetric between the two
edges, so it does not cancel in a difference (measured: 5.25% undercount).
A slope taken strictly inside the window never reads either edge, so both the
edge lag and the discarded warmup traffic drop out of the estimate.

Usage:
    interior_rate.py <load.log> <poll.txt> <load.json> <trim_sec>

Prints the ops-equivalent delta for the measured span, or ``NA`` if the inputs
do not support an estimate (no window marker, too few interior samples, or a
degenerate time base). ``NA`` is deliberate: the caller must not silently treat
a missing estimate as a passing measurement.
"""

import json
import sys


def main() -> int:
    if len(sys.argv) != 5:
        print("NA")
        return 2
    log_path, poll_path, json_path, trim_arg = sys.argv[1:5]
    try:
        trim = float(trim_arg)
    except ValueError:
        print("NA")
        return 2

    # t0: real wall-clock instant the measured window opened. Requires the
    # producer to have been run unbuffered, else every line shares one stamp.
    t0 = 0.0
    found = False
    with open(log_path, errors="replace") as handle:
        for line in handle:
            if "Sustained window" in line:
                try:
                    t0 = float(line.split()[0])
                    found = True
                except (IndexError, ValueError):
                    pass
                break
    if not found:
        print("NA")
        return 1

    with open(json_path) as handle:
        metrics = json.load(handle)["metrics"]["op_0"]
    # window ALONE, never window + drain. runner.py sets sustained_window_sec =
    # last_observed - t_start, already spanning first submit to last completion;
    # sustained_drain_sec = last_observed - refill_end is a SUBSET of it. Summing
    # them double-counts the tail, inflating both t1 and the scaled slope.
    span = float(metrics["window_sec"])
    t1 = t0 + span

    points = []
    with open(poll_path, errors="replace") as handle:
        for line in handle:
            parts = line.split()
            if len(parts) != 2:
                continue
            try:
                points.append((float(parts[0]), int(parts[1])))
            except ValueError:
                continue

    interior = [(x, y) for x, y in points if t0 + trim <= x <= t1 - trim]
    if len(interior) < 8:
        print("NA")
        return 1

    count = len(interior)
    mean_x = sum(x for x, _ in interior) / count
    mean_y = sum(y for _, y in interior) / count
    denom = sum((x - mean_x) ** 2 for x, _ in interior)
    if denom <= 0:
        print("NA")
        return 1
    slope = sum((x - mean_x) * (y - mean_y) for x, y in interior) / denom

    # Scale the steady-state ops/sec across the full measured span so the
    # reporter can compare it to app bytes accumulated over that same span.
    print(int(round(slope * span)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
