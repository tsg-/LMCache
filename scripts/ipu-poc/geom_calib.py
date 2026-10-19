#!/usr/bin/env python3
"""Derive bytes-per-op for an RDMA hw_counter at the model-page geometry.

The existing gate pins ``SEG = 52428`` bytes per ``InRdmaWrites`` op and
``4096`` per ``InRdmaReads`` op. Both were calibrated against 28 MiB objects,
where a sub-op rounding residue per object is invisible. At 147456-byte objects
the residue is a much larger fraction of one object's ops, and the two
directions were calibrated separately and must stay separate -- neither
constant may be reused across directions or geometries without a fresh
measurement.

This tool takes a rounds-mode run's own reported success count plus the
whole-process counter delta for that run's bracket and prints the implied
bytes per op. It makes no accept/reject decision: agreement between a short and
a long run at the same geometry is what licenses quoting a constant, and that
comparison is the caller's.

Rounds mode with ``--warmup-rounds 0`` is required, so that application bytes
are exactly ``successes x page bytes`` with no discarded warmup traffic inside
the bracket.

A constant is only valid for the geometry it was measured at, so the record is
scoped by profile SHA-256, page size, objects per submit, and key prefix -- not
by the consumer-side filename, which says nothing about what was measured. The
driver refuses to apply a constant whose scope does not match the cell.

Usage:
    geom_calib.py <run.json> <counter_delta> <page_kb> <counter_name> <tag>
                  <profile_sha> <objects_per_submit> <key_prefix>

Appends one ``key=value`` block to stdout. ``read_segment_bytes=`` /
``write_segment_bytes=`` lines are the machine-readable outputs the driver
greps for.
"""

import json
import sys

_DIRECTION = {
    # On the initiator, InRdmaWrites is the READ instrument: the target writes
    # the payload back. InRdmaReads is the WRITE instrument. This inversion is
    # deliberate and has burned prior runs -- do not "fix" it.
    "InRdmaWrites": ("read", "Load"),
    "InRdmaReads": ("write", "Store"),
}


def main() -> int:
    if len(sys.argv) != 9:
        print(
            "usage: geom_calib.py <run.json> <delta> <page_kb> <counter> <tag> "
            "<profile_sha> <objects_per_submit> <key_prefix>"
        )
        return 2
    js, delta_arg, kb_arg, counter, tag, sha, ops_arg, prefix = sys.argv[1:9]
    if counter not in _DIRECTION:
        print(f"ABORT: unknown counter {counter!r}")
        return 2
    direction, operation = _DIRECTION[counter]
    delta = int(delta_arg)
    page_kb = int(kb_arg)
    ops_per_submit = int(ops_arg)

    metrics = json.load(open(js))["metrics"]
    config = metrics.get("config") or {}
    section = next(
        (
            v
            for k, v in metrics.items()
            if k != "config" and v.get("operation") == operation
        ),
        None,
    )
    if section is None:
        print(f"ABORT: {js} has no {operation} section")
        return 2

    mode = config.get("mode")
    if mode != "rounds":
        print(f"ABORT: calibration requires rounds mode, got {mode!r}")
        return 2

    # An object-group run reports no single per-key size. Deriving bytes per
    # op from the caller's page_kb anyway would scope the constant to a
    # geometry the run never had, so refuse instead of trusting the argument.
    cfg_page_kb = config.get("data_size_kb")
    if cfg_page_kb is None:
        print(
            f"ABORT: {js} reports no data_size_kb, so its objects are not a "
            f"uniform page. Calibration derives application bytes from one "
            f"page size and cannot describe an object-group run."
        )
        return 2
    if int(cfg_page_kb) != page_kb:
        print(
            f"ABORT: run's data_size_kb {cfg_page_kb} != declared page_kb "
            f"{page_kb}; the constant would be scoped to the wrong geometry"
        )
        return 2

    cfg_keys = config.get("num_keys")
    if cfg_keys is not None and int(cfg_keys) != ops_per_submit:
        print(
            f"ABORT: run's num_keys {cfg_keys} != declared objects/submit "
            f"{ops_per_submit}; the constant would be scoped to the wrong geometry"
        )
        return 2

    keys = section.get("total_keys") or 0
    succ = section.get("total_success") or 0
    if keys == 0 or succ != keys:
        print(f"ABORT: calibration run did not fully succeed: {succ}/{keys}")
        return 1
    if delta <= 0:
        print(f"ABORT: counter delta must be positive, got {delta}")
        return 1

    app_bytes = succ * page_kb * 1024
    bytes_per_op = app_bytes / delta

    print(f"# --- calibration {tag} ({direction} via {counter}) ---")
    print(f"tag={tag}")
    print(f"counter={counter}")
    print(f"direction={direction}")
    print(f"profile_sha256={sha}")
    print(f"objects_per_submit={ops_per_submit}")
    print(f"key_prefix={prefix}")
    print(f"pages={succ}")
    print(f"page_kb={page_kb}")
    print(f"app_bytes={app_bytes}")
    print(f"counter_delta={delta}")
    print(f"{direction}_segment_bytes={bytes_per_op:.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
