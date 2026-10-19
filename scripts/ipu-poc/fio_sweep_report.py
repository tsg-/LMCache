#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Summarise a ``run_fio_capacity_sweep.sh`` run into one JSON dataset.

Reads only the artifacts the sweep itself wrote -- fio's ``--output-format=json``,
the RDMA counter brackets, and the run context -- so a charted number and an
accepted cell cite the same evidence.

Bandwidth always comes from fio's ``group_reporting`` aggregate (``bw_bytes``),
never from a per-job number or a hand-rolled sum, because the sweep deliberately
spreads one job across two devices.

Two gates are applied per cell, both fatal to that cell rather than to the run:

- **Fabric errors.** Any nonzero delta among the six error counters. The read and
  write payload counters are exempt: they are instruments, not errors.
- **Counter agreement.** The RDMA read counter, scaled by the calibrated segment
  size, must land within tolerance of fio's reported read bandwidth. This is
  reported but NOT gating, matching the L2 reporter's contract -- the segment
  constant is calibrated per geometry and is not expected to hold across every
  block size in this sweep.

Each cell also carries two shape signals, because a throughput number alone hides
both of the ways this sweep can mislead:

- ``bound`` is ``capacity`` only at or above the measured saturation depth
  (:data:`SATURATED_QD`); below it the cell reports what the generator kept
  outstanding, so a QD16 cell must not be charted as a surface's ceiling. The
  4 KiB and 16 KiB cells are latency/IOPS points by construction and never reach
  the capacity ceiling at any depth here.
- ``bracket_overrun`` marks a cell whose wall-clock bracket ran materially longer
  than ramp+runtime, which means work that was not the measured workload landed
  inside it. The counter cross-check is suppressed for those cells rather than
  computed against a diluted elapsed time.

Usage:
    python fio_sweep_report.py --run-dir /root/mkp-fio-sweep/<run> \
        --output summary.json
"""

from __future__ import annotations

# Standard
import argparse
import json
import re
from pathlib import Path
from typing import Any

# The six counters that indicate fabric trouble. InRdmaWrites/InRdmaReads are
# excluded on purpose: they are the payload instruments and are expected to be
# large. Names match the irdma sysfs spelling exactly.
ERROR_COUNTERS = (
    "RetransSegs",
    "Nak Sequence Error",
    "RTO",
    "RNR received",
    "Rcvd Out of order packets",
    "InProtoErrors",
)

# On the initiator the counter model is inverted relative to its name: an
# NVMe-oF READ is satisfied by the target RDMA-writing into initiator memory, so
# InRdmaWrites is the READ instrument. Do not "correct" this.
READ_COUNTER = "InRdmaWrites"

# RDMA write segments cap at ~52428 B for transfers at or above 256 KiB; the
# 144 KiB geometry calibrates to 45369.8. Used only for the non-gating
# cross-check, and only where the block size has a calibrated constant.
SEGMENT_BYTES: dict[str, float] = {"144k": 45369.8, "256k": 52428.3, "512k": 52428.3}

# Measured saturation point of this path, from the 2026-08-11 depth probe at
# 144 KiB on remote_xfs: 53.5 Gb/s at qd8, 82.6 at qd16, 93.6 at qd24, 95.6 at
# qd32, then flat (95.7 at qd48 and qd64). So qd32 is the first saturated depth
# and qd16 is genuinely short of the ceiling -- NOT a rounding difference.
#
# A cell is therefore only comparable across surfaces as a *capacity* result if
# it is at or above this depth. Below it, the number is a function of how much
# the generator kept outstanding, and two surfaces can differ simply because one
# had lower per-request latency.
SATURATED_QD = 32

CELL_RE = re.compile(
    r"^(?P<kind>read|mixed(?P<ratio>\d+))_(?P<surface>\w+?)"
    r"_bs(?P<bs>\w+?)_qd(?P<qd>\d+)_rep(?P<rep>\d+)$"
)


def parse_kv(path: Path) -> dict[str, str]:
    """Read a ``key=value`` artifact into a dict.

    Args:
        path: File whose lines are ``key=value``; other lines are ignored.

    Returns:
        The parsed mapping, empty if the file does not exist.
    """
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


def bs_bytes(bs: str) -> int:
    """Convert an fio block-size token to bytes.

    Args:
        bs: A token such as ``4k``, ``144k``, or ``1m``.

    Returns:
        The size in bytes.

    Raises:
        ValueError: If the token is not a recognised fio size.
    """
    match = re.fullmatch(r"(\d+)([kmKM]?)", bs)
    if match is None:
        raise ValueError(f"unrecognised block size {bs!r}")
    scale = {"": 1, "k": 1024, "m": 1024 * 1024}[match.group(2).lower()]
    return int(match.group(1)) * scale


def read_cell(run_dir: Path, name: str) -> dict[str, Any]:
    """Summarise one sweep cell from its fio JSON and counter bracket.

    Args:
        run_dir: The sweep run directory.
        name: The cell name, e.g. ``read_remote_xfs_bs144k_qd16_rep1``.

    Returns:
        The cell's parameters, fio-reported throughput and latency, counter
        cross-check, gate verdict, and whether it is capacity- or
        latency-bound.

    Raises:
        ValueError: If the cell name does not parse.
    """
    match = CELL_RE.match(name)
    if match is None:
        raise ValueError(f"cell name does not parse: {name}")

    payload = json.loads((run_dir / "fio" / f"{name}.json").read_text())
    job = payload["jobs"][0]
    read, write = job["read"], job["write"]
    read_bytes_s, write_bytes_s = read["bw_bytes"], write["bw_bytes"]
    total_bytes_s = read_bytes_s + write_bytes_s

    delta = parse_kv(run_dir / "counters" / f"{name}.delta")
    errors = {
        counter: int(delta[counter])
        for counter in ERROR_COUNTERS
        if delta.get(counter, "NA") != "NA"
    }
    missing = [c for c in ERROR_COUNTERS if delta.get(c, "NA") == "NA"]

    block = bs_bytes(match.group("bs"))
    depth = int(match.group("qd"))
    outstanding = block * depth

    # The bracket should span ramp+runtime and little else. If it is materially
    # longer, something ran inside it that was not the measured workload -- file
    # layout is the case that actually occurred -- and any per-second quantity
    # derived from the bracket is diluted. Flagged rather than silently divided.
    ramp = float(delta.get("ramp_sec", 0) or 0)
    runtime = float(delta.get("runtime_sec", 0) or 0)
    elapsed_bracket = float(delta.get("window_end", 0) or 0) - float(
        delta.get("window_start", 0) or 0
    )
    expected = ramp + runtime
    bracket_overrun = bool(expected and elapsed_bracket > expected * 1.25)

    # Non-gating: does the fabric counter corroborate fio's read bandwidth?
    ratio = None
    segment = SEGMENT_BYTES.get(match.group("bs"))
    counter_ops = int(delta.get(READ_COUNTER, 0) or 0)
    if segment and read_bytes_s and elapsed_bracket > 0 and not bracket_overrun:
        # The bracket spans ramp+measured while bw_bytes covers measured only,
        # so this is a corroboration, not an identity.
        ratio = (counter_ops * segment / elapsed_bracket) / read_bytes_s

    achieved_read_frac = read_bytes_s / total_bytes_s if total_bytes_s else None
    disk_util = sorted(u["name"] for u in payload.get("disk_util", []))

    return {
        "cell": name,
        "kind": "mixed" if match.group("kind").startswith("mixed") else "read",
        "requested_read_pct": int(match.group("ratio"))
        if match.group("ratio")
        else 100,
        "surface": match.group("surface"),
        "bs": match.group("bs"),
        "bs_bytes": block,
        "qd": depth,
        "rep": int(match.group("rep")),
        "outstanding_bytes": outstanding,
        "read_gbps": read_bytes_s * 8 / 1e9,
        "write_gbps": write_bytes_s * 8 / 1e9,
        "total_gbps": total_bytes_s * 8 / 1e9,
        "read_iops": read["iops"],
        "write_iops": write["iops"],
        "achieved_read_pct": achieved_read_frac * 100 if achieved_read_frac else None,
        "read_lat_mean_ms": read["clat_ns"]["mean"] / 1e6,
        "read_lat_p99_ms": read["clat_ns"].get("percentile", {}).get("99.000000", 0)
        / 1e6,
        "write_lat_p99_ms": write["clat_ns"].get("percentile", {}).get("99.000000", 0)
        / 1e6,
        "devices_touched": disk_util,
        "counter_read_ratio": ratio,
        "bracket_elapsed_sec": elapsed_bracket,
        "bracket_overrun": bracket_overrun,
        "fabric_errors": errors,
        "fabric_counters_missing": missing,
        # Capacity vs offered-depth-limited, by measured saturation depth rather
        # than an assumed outstanding-bytes threshold.
        "bound": "capacity" if depth >= SATURATED_QD else "offered-depth",
        "accepted": not any(errors.values()) and not missing,
        "reject_reason": "nonzero fabric errors"
        if any(errors.values())
        else ("counters unavailable" if missing else ""),
    }


def summarise(cells: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group accepted cells into one row per (surface, kind, bs, qd).

    Reps are collapsed to min/mean/max so run-to-run spread is visible: a 4%
    gap between two surfaces means nothing if the spread within one surface is
    also 4%.

    Args:
        cells: Per-cell records from :func:`read_cell`.

    Returns:
        One row per parameter combination, ordered by surface, kind, block
        size, and depth.
    """
    groups: dict[tuple[str, str, int, int, int], list[dict[str, Any]]] = {}
    for cell in cells:
        if not cell["accepted"]:
            continue
        key = (
            cell["surface"],
            cell["kind"],
            cell["requested_read_pct"],
            cell["bs_bytes"],
            cell["qd"],
        )
        groups.setdefault(key, []).append(cell)

    rows: list[dict[str, Any]] = []
    for (surface, kind, read_pct, block, depth), members in sorted(groups.items()):
        totals = [m["total_gbps"] for m in members]
        rows.append(
            {
                "surface": surface,
                "kind": kind,
                "requested_read_pct": read_pct,
                "bs": members[0]["bs"],
                "bs_bytes": block,
                "qd": depth,
                "reps": len(members),
                "bound": members[0]["bound"],
                "total_gbps_mean": sum(totals) / len(totals),
                "total_gbps_min": min(totals),
                "total_gbps_max": max(totals),
                "spread_pct": (max(totals) - min(totals)) / min(totals) * 100
                if min(totals)
                else 0.0,
                "read_gbps_mean": sum(m["read_gbps"] for m in members) / len(members),
                "write_gbps_mean": sum(m["write_gbps"] for m in members) / len(members),
                "achieved_read_pct_mean": sum(
                    m["achieved_read_pct"] for m in members if m["achieved_read_pct"]
                )
                / len(members)
                if members[0]["achieved_read_pct"]
                else None,
                "read_lat_p99_ms_max": max(m["read_lat_p99_ms"] for m in members),
                "read_iops_mean": sum(m["read_iops"] for m in members) / len(members),
                "bracket_overruns": sum(1 for m in members if m["bracket_overrun"]),
                "counter_ratios": [
                    round(m["counter_read_ratio"], 4)
                    for m in members
                    if m["counter_read_ratio"] is not None
                ],
            }
        )
    return rows


def main() -> None:
    """Summarise one sweep run and write the dataset as JSON."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    # Underscore-prefixed artifacts are setup jobs, not measured cells: the
    # sweep's one-time working-file layout writes _layout.json into the same
    # directory. Anything else that fails to parse is a real error and raises.
    names = sorted(
        p.stem
        for p in (args.run_dir / "fio").glob("*.json")
        if not p.stem.startswith("_")
    )
    cells = [read_cell(args.run_dir, name) for name in names]
    context = parse_kv(args.run_dir / "context.txt")

    data = {
        "context": context,
        "cells": cells,
        "summary": summarise(cells),
        "accepted": sum(1 for c in cells if c["accepted"]),
        "total": len(cells),
        "corpus_unchanged": context.get("corpus_before") == context.get("corpus_after"),
    }
    args.output.write_text(json.dumps(data, indent=1))

    print(f"wrote {args.output}")
    print(f"  cells: {data['accepted']}/{data['total']} accepted")
    print(f"  corpus unchanged: {data['corpus_unchanged']}")
    for row in data["summary"]:
        label = f"{row['surface']} {row['kind']}"
        if row["kind"] == "mixed":
            label += f"@{row['requested_read_pct']}r"
        note = "" if row["bound"] == "capacity" else "  [BELOW SATURATION QD]"
        if row["bracket_overruns"]:
            note += f"  [{row['bracket_overruns']} bracket overrun]"
        print(
            f"  {label:24s} bs={row['bs']:>5s} qd={row['qd']:<3d}"
            f" {row['total_gbps_mean']:7.2f} Gb/s"
            f" (n={row['reps']}, spread {row['spread_pct']:.1f}%)"
            f" p99={row['read_lat_p99_ms_max']:.3f}ms{note}"
        )
    for cell in cells:
        if not cell["accepted"]:
            print(f"  REJECTED {cell['cell']}: {cell['reject_reason']}")


if __name__ == "__main__":
    main()
