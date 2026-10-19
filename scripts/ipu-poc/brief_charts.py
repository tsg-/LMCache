#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Render the figures the MEV+GNR brief needs, at final page size.

This is the document-geometry companion to ``fio_sweep_charts.py``. That script
renders eleven slide-sized figures with the scope caveats baked into the image;
this one renders the three that earn space on a short brief, sized so they are
inserted into Word at 100% scale and never resampled. Captions stay out of the
image and live in the document's Caption style, so the reserved caption strip
that the slide figures need is absent here on purpose.

None of the three carries a title: the assertion belongs in the body paragraph
above the figure, and repeating it inside the image is a mirror.

Every plotted number is read from the same run artifacts the acceptance gates
used -- the ``fio_sweep_report.py`` summaries and the ``geom_collect.py``
dataset -- so nothing is transcribed.

Usage:
    python brief_charts.py --results docs/.../results/fio-sweep-2026-08-11 \
        --outdir docs/.../diagrams/brief
"""

from __future__ import annotations

# Standard
import argparse
import json
import statistics
from pathlib import Path
from typing import Any

# Third Party
import matplotlib

matplotlib.use("Agg")
# Third Party
import matplotlib.pyplot as plt

# Page geometry. Letter with 1 in margins gives a 6.5 in text column; every
# figure is rendered at exactly that width so Word inserts it at 100% and the
# in-figure point sizes survive as on-page point sizes.
PAGE_WIDTH_IN = 6.5
LADDER_HEIGHT_IN = 2.4
MIXED_HEIGHT_IN = 2.6
DEPTH_HEIGHT_IN = 2.5
DOC_DPI = 300

# Same fills as the slide figure set, so the two sets read as one document.
FILL_LOCAL = "#fdd0a2"
FILL_REMOTE = "#c7e9c0"
FILL_XFS = "#c6dbef"
FILL_L2 = "#dadaeb"
FILL_WRITE = "#fdd0a2"
EDGE = "#222222"
CEILING_STYLE = dict(color="#999999", linestyle="--", linewidth=1.0, zorder=1)

# The one cell shape the retrieve ladder compares across surfaces. Every bar is
# this same fio job -- 256 KiB sequential read, QD32, 60 s with a 10 s ramp -- so a
# gap between bars is a property of the surface and not of the measurement.
# 256 KiB because that is the block size MMG-400 phase 1 is written against;
# the 144 KiB cells are measured too but only 256 KiB earns document space.
LADDER_BS = "256k"
LADDER_QD = 32
# 5:1 is the mixed ratio bench l2 expresses as a read:write ratio; as a fraction
# of bytes that is 5/6 = 83.3%, which is what fio's rwmixread=83 requests.
MIXED_READ_PCT = 83
# Mixtral 8x22B is the 256 KiB profile whose cells are clean at every process
# count. Llama-3 405B is also 256 KiB and matches on read, but its 4-process
# mixed cell carries an 11% counter/app mismatch, so it stays out of the figures.
MIXED_PROFILE = "Mixtral 8x22B"


def _style() -> None:
    """Apply the shared academic style, retuned for 6.5-inch document figures.

    Body copy in the brief is 10 pt, so in-figure text is set at 9 pt: large
    enough to read at final size, small enough that it stays subordinate to the
    document's own type. Neither figure carries a title -- the assertion lives in
    the body paragraph above it and the measurement detail in the caption below.
    """
    plt.rcParams.update(
        {
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.edgecolor": EDGE,
            "axes.linewidth": 0.8,
            "axes.grid": True,
            "axes.grid.axis": "y",
            "grid.color": "#dddddd",
            "grid.linewidth": 0.6,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "font.family": "sans-serif",
            "font.size": 9,
            "axes.labelsize": 9,
            "xtick.labelsize": 8.5,
            "ytick.labelsize": 8.5,
            "legend.fontsize": 8.5,
            "legend.frameon": False,
            "figure.dpi": DOC_DPI,
        }
    )


def _save(fig: plt.Figure, outdir: Path, name: str) -> Path:
    """Write one figure as PNG for the document and SVG for later editing.

    Args:
        fig: Figure to write.
        outdir: Destination directory; created if absent.
        name: Basename without extension.

    Returns:
        The PNG path.
    """
    outdir.mkdir(parents=True, exist_ok=True)
    fig.tight_layout(pad=0.4)
    png = outdir / f"{name}.png"
    fig.savefig(png)
    fig.savefig(outdir / f"{name}.svg")
    plt.close(fig)
    return png


def _load_summary(results: Path, surface: str) -> dict[str, Any]:
    """Read one fio sweep summary.

    Args:
        results: Directory holding the sweep artifacts.
        surface: Surface tag, matching the ``<surface>.json`` filename.

    Returns:
        The parsed summary mapping.

    Raises:
        FileNotFoundError: If the surface summary is absent.
    """
    path = results / f"{surface}.json"
    if not path.exists():
        raise FileNotFoundError(f"missing fio sweep summary: {path}")
    with path.open() as handle:
        return json.load(handle)


def _fio_cell(
    summary: dict[str, Any],
    kind: str,
    bs: str,
    qd: int,
    read_pct: int,
) -> dict[str, float]:
    """Average the accepted repetitions of one fio cell.

    Args:
        summary: Parsed sweep summary for a single surface.
        kind: Cell kind, ``read`` or ``mixed``.
        bs: Block size tag, e.g. ``144k``.
        qd: Queue depth.
        read_pct: Requested read percentage for the cell.

    Returns:
        Mapping with ``read``, ``write``, ``total``, and ``n`` keys, in Gb/s.

    Raises:
        ValueError: If no accepted repetition matches the cell.
    """
    matches = [
        cell
        for cell in summary["cells"]
        if cell.get("accepted")
        and cell["kind"] == kind
        and cell["bs"] == bs
        and cell["qd"] == qd
        and cell.get("requested_read_pct") == read_pct
    ]
    if not matches:
        raise ValueError(f"no accepted {kind} cell at bs={bs} qd={qd} read={read_pct}%")
    return {
        "read": statistics.mean(c["read_gbps"] for c in matches),
        "write": statistics.mean(c.get("write_gbps", 0.0) for c in matches),
        "total": statistics.mean(c.get("total_gbps", c["read_gbps"]) for c in matches),
        "n": float(len(matches)),
    }


def _l2_read_gbps(geom: dict[str, Any], profile: str) -> float:
    """Average the accepted single-initiator sustained read runs for a profile.

    Args:
        geom: Parsed ``geom_collect.py`` dataset.
        profile: Model profile key, e.g. ``DeepSeek-V3``.

    Returns:
        Mean read throughput in Gb/s.

    Raises:
        ValueError: If the profile has no accepted single-initiator read run.
    """
    runs = [
        r
        for r in geom["profiles"][profile]["read"]
        if r.get("accepted") and r["initiators"] == 1
    ]
    if not runs:
        raise ValueError(f"no accepted single-initiator read run for {profile}")
    return statistics.mean(r["gbps"] for r in runs)


def _l2_mixed(geom: dict[str, Any], profile: str, initiators: int) -> dict[str, float]:
    """Pull one accepted mixed cell for a profile at a given process count.

    Args:
        geom: Parsed ``geom_collect.py`` dataset.
        profile: Model profile key.
        initiators: Number of concurrent load processes.

    Returns:
        Mapping with ``read``, ``write``, and ``total`` keys, in Gb/s.

    Raises:
        ValueError: If no accepted mixed cell matches the process count.
    """
    for cell in geom["profiles"][profile]["mixed"]:
        if cell.get("accepted") and cell["initiators"] == initiators:
            return {
                "read": cell["read_gbps"],
                "write": cell["write_gbps"],
                "total": cell["total_gbps"],
            }
    raise ValueError(f"no accepted mixed cell for {profile} at {initiators} initiators")


def chart_retrieve_ladder(
    summaries: dict[str, dict[str, Any]],
    geom: dict[str, Any],
    outdir: Path,
) -> Path:
    """Chart the retrieve path from target media out to the LMCache client.

    Horizontal bars, because the surface names are long enough that vertical
    ticks would wrap to two lines and eat the vertical space a 2-page brief
    cannot spare. The dashed reference line is the measured NVMe-oF read
    ceiling on this link, which turns "we are at the wire" from arithmetic the
    reader has to do into something visible.

    Args:
        summaries: Parsed fio sweep summaries, keyed by surface tag.
        geom: Parsed ``geom_collect.py`` dataset.
        outdir: Destination directory.

    Returns:
        The PNG path.
    """
    local = _fio_cell(summaries["local_raw"], "read", LADDER_BS, LADDER_QD, 100)
    remote = _fio_cell(summaries["remote_raw"], "read", LADDER_BS, LADDER_QD, 100)
    xfs = _fio_cell(summaries["remote_xfs"], "read", LADDER_BS, LADDER_QD, 100)
    l2 = _l2_read_gbps(geom, MIXED_PROFILE)
    ceiling = float(geom["ceiling_gbps"])

    # Surface names are the customer-facing ones: "NVMe-oF block" is the raw
    # remote namespace with no filesystem, and "NVMe-oF XFS raid0" is the md0
    # stripe plus XFS, which live on the INITIATOR across the namespaces the
    # target exports raw. See results/mkp1-fsnative-sustained-2026-08-04.md.
    # The three fabric surfaces name the link explicitly so the one bar that
    # never crosses it -- local target media -- reads as the outlier it is.
    rows = [
        # Two label lines, matching its neighbours: the worker count is in the
        # caption, so it does not need a third line here.
        ("LMCache retrieve\nFalcon 100GbE fabric", l2, FILL_L2),
        ("NVMe-oF XFS raid0\nFalcon 100GbE fabric", xfs["read"], FILL_XFS),
        ("NVMe-oF block\nFalcon 100GbE fabric", remote["read"], FILL_REMOTE),
        ("Local target media\n2x PM9A3, no fabric", local["read"], FILL_LOCAL),
    ]

    fig, ax = plt.subplots(figsize=(PAGE_WIDTH_IN, LADDER_HEIGHT_IN))
    positions = range(len(rows))
    ax.barh(
        list(positions),
        [value for _, value, _ in rows],
        height=0.62,
        color=[fill for _, _, fill in rows],
        edgecolor=EDGE,
        linewidth=0.8,
        zorder=3,
    )
    for index, (_, value, _) in enumerate(rows):
        ax.text(
            value + 1.6,
            index,
            f"{value:.1f}",
            va="center",
            ha="left",
            fontsize=8.5,
            fontweight="bold",
        )

    ax.axvline(ceiling, **CEILING_STYLE)
    # Below the bottom bar rather than above the top one, where it would sit
    # between the top tick label and the figure edge.
    ax.text(
        ceiling - 2.0,
        -0.62,
        f"{ceiling:.1f} Gb/s NVMe-oF read ceiling",
        ha="right",
        va="center",
        fontsize=7.4,
        color="#666666",
    )

    ax.set_yticks(list(positions))
    ax.set_yticklabels([label for label, _, _ in rows])
    ax.set_ylim(-1.0, len(rows) - 0.35)
    ax.set_xlabel("Read throughput (Gb/s)")
    ax.set_xlim(0, 122)
    ax.grid(False, axis="y")
    ax.grid(True, axis="x", color="#dddddd", linewidth=0.6)
    ax.set_axisbelow(True)
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0)

    return _save(fig, outdir, "brief-retrieve-ladder")


def chart_store_and_retrieve(geom: dict[str, Any], outdir: Path) -> Path:
    """Chart retrieve-only beside the 5:1 store+retrieve mix on the same link.

    Horizontal bars for the same reason the ladder uses them: the row labels name
    a trafficgen configuration and would wrap under a vertical tick. Stacked,
    because the point is that both flows are running in one measured window and
    the write component is a real share of the link, not a footnote. The
    retrieve-only row is the reference the mixed rows fall short of, since that
    shortfall is the brief's open question.

    Args:
        geom: Parsed ``geom_collect.py`` dataset.
        outdir: Destination directory.

    Returns:
        The PNG path.
    """
    read_only = _l2_read_gbps(geom, MIXED_PROFILE)
    mixed_one = _l2_mixed(geom, MIXED_PROFILE, 1)
    mixed_four = _l2_mixed(geom, MIXED_PROFILE, 4)
    ceiling = float(geom["ceiling_gbps"])

    # Top row reads first, so the rows are drawn bottom-up. The ratio rides on
    # the first label line to keep the second line short, since long tick labels
    # push the axes right and steal plot width.
    labels = [
        "Store + retrieve, 5:1\n4 trafficgen instances",
        "Store + retrieve, 5:1\n1 trafficgen instance",
        "Retrieve only, 100% read\n1 trafficgen instance",
    ]
    reads = [mixed_four["read"], mixed_one["read"], read_only]
    writes = [mixed_four["write"], mixed_one["write"], 0.0]

    fig, ax = plt.subplots(figsize=(PAGE_WIDTH_IN, MIXED_HEIGHT_IN))
    positions = range(len(labels))
    # One fill per direction and the segments labelled in place, so the figure
    # needs no legend: on a short brief the legend strip costs more vertical
    # space than it explains.
    ax.barh(
        list(positions),
        reads,
        height=0.52,
        color=FILL_L2,
        edgecolor=EDGE,
        linewidth=0.8,
        zorder=3,
    )
    ax.barh(
        list(positions),
        writes,
        height=0.52,
        left=reads,
        color=FILL_WRITE,
        edgecolor=EDGE,
        linewidth=0.8,
        zorder=3,
    )

    for index, (read, write) in enumerate(zip(reads, writes, strict=True)):
        total = read + write
        ax.text(
            total + 1.6,
            index,
            f"{total:.1f}",
            va="center",
            ha="left",
            fontsize=8.5,
            fontweight="bold",
        )
        label = f"retrieve {read:.1f}" if write == 0 else f"read {read:.1f}"
        ax.text(read / 2, index, label, ha="center", va="center", fontsize=7.6)
        if write > 0:
            # The store segment is ~10 Gb/s wide, too narrow for text inside it,
            # so the label sits just above the segment's midpoint instead.
            ax.text(
                read + write / 2,
                index + 0.42,
                f"store {write:.1f}",
                ha="center",
                va="bottom",
                fontsize=7.4,
            )

    ax.axvline(ceiling, **CEILING_STYLE)
    ax.text(
        ceiling - 2.0,
        -0.62,
        f"{ceiling:.1f} Gb/s NVMe-oF read ceiling",
        ha="right",
        va="center",
        fontsize=7.4,
        color="#666666",
    )

    ax.set_yticks(list(positions))
    ax.set_yticklabels(labels)
    ax.set_ylim(-1.0, len(labels) - 0.3)
    ax.set_xlabel("Throughput (Gb/s)")
    ax.set_xlim(0, 122)
    ax.grid(False, axis="y")
    ax.grid(True, axis="x", color="#dddddd", linewidth=0.6)
    ax.set_axisbelow(True)
    ax.spines["left"].set_visible(False)
    ax.tick_params(axis="y", length=0)

    return _save(fig, outdir, "brief-store-and-retrieve")


def chart_depth_curve(results: Path, outdir: Path) -> Path:
    """Chart read goodput against offered queue depth.

    The queue-depth probes are single repetitions at one block size, so this is a
    shape, not an acceptance result: it shows where the path stops being limited
    by offered depth and starts being limited by capacity. That transition is why
    every headline cell is measured at QD32 and not deeper.

    Args:
        results: Directory holding the ``probe_qd<N>.json`` artifacts.
        outdir: Destination directory.

    Returns:
        The PNG path.

    Raises:
        FileNotFoundError: If no queue-depth probe artifacts are present.
    """
    probes = []
    for path in sorted(results.glob("probe_qd*.json")):
        with path.open() as handle:
            cell = json.load(handle)["cells"][0]
        probes.append(cell)
    if not probes:
        raise FileNotFoundError(f"no probe_qd*.json artifacts in {results}")
    probes.sort(key=lambda c: c["qd"])
    with (results / "geomdata.json").open() as handle:
        ceiling = float(json.load(handle)["ceiling_gbps"])

    depths = [c["qd"] for c in probes]
    goodput = [c["read_gbps"] for c in probes]
    p99 = [c["read_lat_p99_ms"] for c in probes]

    fig, ax = plt.subplots(figsize=(PAGE_WIDTH_IN, DEPTH_HEIGHT_IN))
    ax.plot(depths, goodput, marker="o", markersize=4.5, linewidth=1.4,
            color="#4a6f9c", markerfacecolor=FILL_XFS, markeredgecolor=EDGE,
            zorder=3, label="Read goodput")

    # The bound flips from offered-depth to capacity partway along the sweep, so
    # shade the depth-limited region rather than annotating each point.
    limited = [c["qd"] for c in probes if c["bound"] == "offered-depth"]
    if limited:
        ax.axvspan(min(depths), max(limited), color="#f4f4f4", zorder=0)
        ax.text(
            (min(depths) + max(limited)) / 2,
            8,
            "limited by offered depth",
            ha="center",
            va="bottom",
            fontsize=7.4,
            color="#666666",
        )

    ax.axhline(ceiling, **CEILING_STYLE)

    # p99 on a twin axis: the point of the sweep is that depth past the knee buys
    # latency, not throughput, and that trade is invisible on goodput alone.
    lat = ax.twinx()
    lat.plot(depths, p99, marker="s", markersize=3.8, linewidth=1.2,
             linestyle="--", color="#b5651d", markerfacecolor="#fdd0a2",
             markeredgecolor=EDGE, zorder=3, label="p99 read latency")
    lat.set_ylabel("p99 read latency (ms)")
    lat.set_ylim(0, max(p99) * 1.35)
    lat.spines["top"].set_visible(False)
    lat.grid(False)

    ax.set_xlabel("Offered queue depth")
    ax.set_ylabel("Read goodput (Gb/s)")
    ax.set_xticks(depths)
    ax.set_ylim(0, 122)
    ax.grid(True, axis="y", color="#dddddd", linewidth=0.6)
    ax.set_axisbelow(True)

    handles, labels = ax.get_legend_handles_labels()
    extra_handles, extra_labels = lat.get_legend_handles_labels()
    ax.legend(handles + extra_handles, labels + extra_labels,
              loc="lower right", fontsize=8.0)

    return _save(fig, outdir, "brief-depth-curve")


def main() -> None:
    """Parse arguments and render both brief figures."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results",
        type=Path,
        required=True,
        help="directory holding the fio sweep summaries and geomdata.json",
    )
    parser.add_argument(
        "--outdir",
        type=Path,
        required=True,
        help="destination directory for the rendered figures",
    )
    args = parser.parse_args()

    _style()
    summaries = {
        surface: _load_summary(args.results, surface)
        for surface in ("local_raw", "remote_raw", "remote_xfs")
    }
    with (args.results / "geomdata.json").open() as handle:
        geom = json.load(handle)

    for path in (
        chart_retrieve_ladder(summaries, geom, args.outdir),
        chart_store_and_retrieve(geom, args.outdir),
        chart_depth_curve(args.results, args.outdir),
    ):
        print(path)


if __name__ == "__main__":
    main()
