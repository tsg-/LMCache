#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Render the matched FIO capacity sweep as charts.

Consumes one or more ``fio_sweep_report.py`` summaries -- which read only run
artifacts -- plus, for the mixed comparison, the ``geom_collect.py`` dataset that
carries the ``bench l2`` cells. Every plotted number traces to the artifact its
acceptance gate used; nothing is transcribed from a report.

Each figure is captioned with what it does NOT show, because these numbers are
easy to over-read in three specific ways: a sub-saturation depth looks like a
surface ceiling, a small-block cell looks like a capacity result when it is a
per-request-cost result, and a fio cell looks like an L2 cell when the two differ
in warmup, verification, and process count.

Usage:
    python fio_sweep_charts.py --summary remote_xfs=xfs.json \
        --summary remote_raw=raw.json --l2-data geomdata.json --outdir charts/
"""

from __future__ import annotations

# Standard
import argparse
import json
import textwrap
from pathlib import Path
from typing import Any

# Third Party
import matplotlib

matplotlib.use("Agg")
# Third Party
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

# Same academic palette as geom_charts.py, so the two figure sets read as one
# document: light fills, thin dark borders, no chartjunk.
FILL = {
    "read": "#c6dbef",
    "write": "#fdd0a2",
    "mixed": "#c7e9c0",
    "l2": "#dadaeb",
    "muted": "#e0e0e0",
    "unsat": "#f7f7f7",
}
EDGE = "#222222"
CEILING_STYLE = dict(color="#999999", linestyle="--", linewidth=1.0, zorder=1)

# Surface display order and labels, coarsest path last. The labels name the
# measured thing rather than the flag, because "remote_xfs" does not tell a
# reader that this is the only surface bench l2 actually runs on.
SURFACE_ORDER = ["local_raw", "remote_raw", "remote_xfs"]
SURFACE_LABEL = {
    "local_raw": "Local raw\n(target media)",
    "remote_raw": "Remote raw\n(NVMe-oF namespaces)",
    "remote_xfs": "Remote XFS/md0\n(the fs_native surface)",
}
SURFACE_FILL = {
    "local_raw": "#fdd0a2",
    "remote_raw": "#c7e9c0",
    "remote_xfs": "#c6dbef",
}
# Darker companions to SURFACE_FILL, for lines. The fills are chosen to sit under
# black text in a bar; at 1.2 pt they are too pale to follow across a panel.
SURFACE_LINE = {
    "local_raw": "#d8860b",
    "remote_raw": "#4a9a45",
    "remote_xfs": "#4a7fb5",
}

# Block sizes that are capacity measurements versus per-request-cost
# measurements. 4 KiB and 16 KiB never approach the large-block ceiling at any
# depth on this path, so they are charted on their own axes, never beside a
# 512 KiB bar.
CAPACITY_BLOCKS = ["144k", "256k", "512k"]
SMALL_BLOCKS = ["4k", "16k"]
BACKUP_CAPACITY_SURFACES = ["local_raw", "remote_xfs"]

# fio's rwmixread value paired with the bench l2 --read-write-ratio that
# requests the same BYTE fraction. Both sides report a byte mix, so the pairing
# needs no unit conversion: 5:1 -> 5/6 -> 83.3%, 9:1 -> 9/10 -> 90%.
MIX_PAIRS = [(83, "5:1"), (90, "9:1")]


def _parse_ratio(value: str) -> float:
    """Parse a ``READ:WRITE`` ratio into its read-per-write byte factor.

    Args:
        value: A ratio such as ``5:1``, matching the ``bench l2
            --read-write-ratio`` spelling.

    Returns:
        Read bytes per write byte, e.g. ``5.0`` for ``5:1``.

    Raises:
        ValueError: If the value is not two positive numbers split by a colon.
    """
    read_part, sep, write_part = value.partition(":")
    if not sep:
        raise ValueError(f"ratio must be READ:WRITE, got {value!r}")
    read, write = float(read_part), float(write_part)
    if read <= 0 or write <= 0:
        raise ValueError(f"ratio parts must be positive, got {value!r}")
    return read / write


def _style() -> None:
    """Apply the shared academic style to all subsequent figures."""
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
            "font.size": 10,
            "axes.titlesize": 11,
            "axes.titleweight": "bold",
            "legend.frameon": False,
            "figure.dpi": 150,
        }
    )


def _caption(fig: plt.Figure, text: str) -> None:
    """Put a scope/caveat note under a figure, where a reader cannot miss it.

    The text is hard-wrapped to the figure's own width and the reserved strip is
    sized from the resulting line count, because matplotlib's ``wrap=True``
    measures against the figure edge and silently overprints the axis labels.

    Args:
        fig: Figure to annotate.
        text: Caption text; internal whitespace is collapsed before wrapping.
    """
    width = max(60, int(fig.get_figwidth() * 16.4))
    lines = textwrap.wrap(" ".join(text.split()), width=width)
    fig.text(
        0.012,
        0.012,
        "\n".join(lines),
        fontsize=7.4,
        color="#444444",
        va="bottom",
        linespacing=1.5,
    )
    fig.__dict__["_caption_frac"] = min(
        0.34, (len(lines) * 13.0 + 12) / (fig.get_figheight() * 100)
    )


def _save(fig: plt.Figure, outdir: Path, name: str) -> Path:
    """Write one figure as both PNG (documents) and SVG (scalable/editable).

    Args:
        fig: Figure to write.
        outdir: Destination directory, assumed to exist.
        name: Basename without extension.

    Returns:
        The PNG path.
    """
    bottom = fig.__dict__.get("_caption_frac", 0.05)
    top = 1.0 - fig.__dict__.get("_legend_frac", 0.0)
    fig.tight_layout(rect=(0, bottom, 1, top))
    png = outdir / f"{name}.png"
    # No bbox_inches="tight": it re-crops to the artists and undoes the reserved
    # caption strip, which is the overlap this layout exists to prevent.
    fig.savefig(png)
    fig.savefig(outdir / f"{name}.svg")
    plt.close(fig)
    return png


def _pin_category_axis(ax: plt.Axes, count: int) -> None:
    """Fix a category axis's limits instead of letting the bars set them.

    With one or two categories matplotlib derives ``xlim`` from the patch extent
    alone, so a bar of any width fills the panel and stops reading as a bar. The
    sweep renders one surface at a time, so this is the common case rather than
    the edge case.

    Args:
        ax: Axes whose x-limits to pin.
        count: Number of categories plotted at integer positions from zero.
    """
    ax.set_xlim(-0.7, count - 0.3)


def _bar_labels(ax: plt.Axes, bars: Any, fmt: str = "{:.1f}", dy: float = 0.6) -> None:
    """Label bars in place; a reader should not have to eyeball a gridline."""
    for bar in bars:
        height = bar.get_height()
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            height + dy,
            fmt.format(height),
            ha="center",
            va="bottom",
            fontsize=7.5,
        )


def _repeat_whiskers(row: dict[str, Any]) -> tuple[float, float]:
    """Return the distance from a repeated-cell mean to its observed extremes."""
    mean = float(row["total_gbps_mean"])
    return (
        max(0.0, mean - float(row["total_gbps_min"])),
        max(0.0, float(row["total_gbps_max"]) - mean),
    )


def _is_unstable(row: dict[str, Any]) -> bool:
    """Return whether repeated measurements span at least five percent."""
    return float(row["spread_pct"]) >= 5.0


def _rows(
    summaries: dict[str, dict[str, Any]],
    surface: str,
    kind: str,
    block: str,
    depth: int,
    read_pct: int = 100,
) -> dict[str, Any] | None:
    """Return the one summary row matching a cell's parameters, if present.

    Args:
        summaries: Per-surface summary datasets keyed by surface name.
        surface: Surface key, e.g. ``remote_xfs``.
        kind: ``read`` or ``mixed``.
        block: Block-size token, e.g. ``144k``.
        depth: Queue depth.
        read_pct: Requested read percentage; 100 for pure-read cells.

    Returns:
        The matching row, or ``None`` when that cell was not run.
    """
    data = summaries.get(surface)
    if data is None:
        return None
    for row in data["summary"]:
        if (
            row["kind"] == kind
            and row["bs"] == block
            and row["qd"] == depth
            and row["requested_read_pct"] == read_pct
        ):
            return row
    return None


def _ceiling_of(summaries: dict[str, dict[str, Any]]) -> float:
    """Return the highest accepted saturated read rate across all surfaces.

    Used as the reference line rather than a nominal link rate, so the line is a
    thing that was measured on this path rather than a spec number.

    Args:
        summaries: Per-surface summary datasets.

    Returns:
        The maximum mean total throughput over saturated pure-read rows, or
        ``0.0`` if there are none.
    """
    rates = [
        row["total_gbps_mean"]
        for data in summaries.values()
        for row in data["summary"]
        if row["kind"] == "read"
        and row["bound"] == "capacity"
        and row["bs"] in CAPACITY_BLOCKS
    ]
    return max(rates) if rates else 0.0


def _l2_cell(profile: dict[str, Any], ratio: str) -> dict[str, Any] | None:
    """Return the single-initiator L2 mixed cell whose ACHIEVED ratio matches.

    The dataset records the achieved read:write ratio as a float (4.9995 for a
    requested 5:1), not the requested string, so matching is by value within 1%
    rather than by label. Single-initiator only: the fio cell is one process, and
    a 4-initiator L2 cell would introduce process fan-in as a second uncontrolled
    variable.

    Args:
        profile: One ``geom_collect.py`` profile carrying a ``mixed`` cell list.
        ratio: The ``READ:WRITE`` ratio to match, e.g. ``5:1``.

    Returns:
        The matching accepted cell, or ``None`` when that ratio was not run.
    """
    want = _parse_ratio(ratio)
    for row in profile["mixed"]:
        if row["initiators"] != 1 or not row.get("accepted", True):
            continue
        achieved = row.get("ratio")
        if achieved and abs(achieved - want) / want <= 0.01:
            return row
    return None


# ---------------------------------------------------------------- figures


def fig_capacity_ladder(
    summaries: dict[str, dict[str, Any]], outdir: Path, block: str = "144k"
) -> Path:
    """Headline: where the capacity goes between target media and the fs surface.

    Args:
        summaries: Per-surface summary datasets.
        outdir: Destination directory.
        block: Block size to chart; the DeepSeek 144 KiB page by default.

    Returns:
        The PNG path.
    """
    found = [
        (s, _rows(summaries, s, "read", block, 32))
        for s in SURFACE_ORDER
        if _rows(summaries, s, "read", block, 32) is not None
    ]
    surfaces = [s for s, _ in found]
    fig, ax = plt.subplots(figsize=(7.6, 4.4))

    means, lows, highs, reps = [], [], [], []
    for _, row in found:
        low, high = _repeat_whiskers(row)
        means.append(row["total_gbps_mean"])
        lows.append(low)
        highs.append(high)
        reps.append(row["reps"])

    xs = range(len(surfaces))
    # Narrower when few surfaces are present: a 0.58-wide bar on a one-category
    # axis fills the panel and stops reading as a bar.
    bar_width = 0.58 if len(surfaces) >= 3 else 0.34
    ax.bar(
        list(xs),
        means,
        bar_width,
        yerr=[lows, highs],
        capsize=4,
        error_kw=dict(elinewidth=0.9, ecolor="#555555"),
        color=[SURFACE_FILL[s] for s in surfaces],
        edgecolor=EDGE,
        linewidth=0.8,
        zorder=3,
    )
    # Labels clear the upper whisker rather than printing through it.
    for x, mean, high in zip(xs, means, highs, strict=True):
        ax.text(
            x,
            mean + high + 2.2,
            f"{mean:.1f}",
            ha="center",
            va="bottom",
            fontsize=7.5,
        )
    for x, n in zip(xs, reps, strict=True):
        ax.text(x, 1.5, f"n={n}", ha="center", fontsize=7, color="#666666", zorder=4)
    # An unstable cell must not read as a rung. This figure is rendered per block
    # size, so at 512 KiB the remote-raw bar is a wide-spread cell and the reader
    # has to see that on the bar, not only in the caption.
    for x, (_, row), high in zip(xs, found, highs, strict=True):
        if not _is_unstable(row):
            continue
        ax.text(
            x,
            row["total_gbps_mean"] + high + 8.0,
            f"unstable: {row['spread_pct']:.1f}% span",
            ha="center",
            va="bottom",
            fontsize=7,
            color="#9c3d18",
        )

    ax.set_xticks(list(xs))
    ax.set_xticklabels([SURFACE_LABEL[s] for s in surfaces], fontsize=8.5)
    ax.set_ylabel("Read throughput (Gb/s)")
    ax.set_title(f"Matched read capacity by surface — {block} sequential read, QD32")
    # Headroom is measured from the tallest whisker, not the tallest mean: on an
    # unstable cell the annotation sits above the whisker cap.
    top = max(mean + high for mean, high in zip(means, highs, strict=True))
    ax.set_ylim(0, top * 1.22)
    _pin_category_axis(ax, len(surfaces))

    _caption(
        fig,
        "Every bar is the SAME fio job shape -- 60 s, 10 s ramp, one job spread "
        "across both devices, group-reported aggregate -- so a gap between bars is "
        "a property of the surface and not of how it was measured. That is the "
        "point of this figure: the three rungs previously on record used three "
        "different runtimes and depths. Whiskers span the min and max of the "
        "repetitions, not a confidence interval. QD32 is the measured saturation "
        "depth on this path; see the depth figure. Falcon-offloaded kernel "
        "NVMe-oF, existing-controller, single-process load; not a 400 GbE, "
        "fresh-QP, multi-initiator, or Falcon-offload-benefit result.",
    )
    return _save(fig, outdir, "fio-capacity-ladder")


def fig_capacity_by_block(summaries: dict[str, dict[str, Any]], outdir: Path) -> Path:
    """Small multiples: one panel per model-page geometry, surfaces side by side.

    Args:
        summaries: Per-surface summary datasets.
        outdir: Destination directory.

    Returns:
        The PNG path.
    """
    ceiling = _ceiling_of(summaries)
    surfaces = _backup_capacity_surfaces(summaries)
    fig, axes = plt.subplots(1, len(CAPACITY_BLOCKS), figsize=(10.6, 4.0), sharey=True)

    bar_width = 0.56 if len(surfaces) >= 3 else 0.42
    for ax, block in zip(axes, CAPACITY_BLOCKS, strict=True):
        xs = range(len(surfaces))
        main, second, rows, lows, highs = [], [], [], [], []
        for surface in surfaces:
            row32 = _rows(summaries, surface, "read", block, 32)
            row16 = _rows(summaries, surface, "read", block, 16)
            main.append(row32["total_gbps_mean"] if row32 else 0.0)
            second.append(row16["total_gbps_mean"] if row16 else None)
            rows.append(row32)
            low, high = _repeat_whiskers(row32) if row32 else (0.0, 0.0)
            lows.append(low)
            highs.append(high)
        bars = ax.bar(
            list(xs),
            main,
            bar_width,
            yerr=[lows, highs],
            capsize=3,
            error_kw=dict(elinewidth=0.9, ecolor="#555555"),
            color=[SURFACE_FILL[s] for s in surfaces],
            edgecolor=EDGE,
            linewidth=0.8,
            zorder=3,
        )
        # Label above the upper whisker, not above the bar: on an unstable cell
        # the mean-relative offset lands the number on top of the whisker cap.
        for bar, high in zip(bars, highs, strict=True):
            _bar_labels(ax, [bar], dy=1.6 + high)
        for x, row, high in zip(xs, rows, highs, strict=True):
            if row is None or not _is_unstable(row):
                continue
            # Stacked in data units, above the value label rather than a fixed
            # point offset: an offset large enough to clear the label on the
            # widest whisker lands on the ceiling rule on a narrow one.
            ax.text(
                x,
                row["total_gbps_mean"] + high + 8.0,
                f"{row['spread_pct']:.1f}% span",
                ha="center",
                va="bottom",
                fontsize=7,
                color="#9c3d18",
            )
        # QD16 as a marker on the same bar, not a second bar: it is a point on
        # the depth curve, not a competing measurement of the same thing.
        for x, value in zip(xs, second, strict=True):
            if value is None:
                continue
            ax.plot(
                [x - bar_width / 2, x + bar_width / 2],
                [value, value],
                color="#444444",
                linewidth=1.3,
                zorder=5,
            )
        if ceiling:
            ax.axhline(ceiling, **CEILING_STYLE)
        _pin_category_axis(ax, len(surfaces))
        ax.set_xticks(list(xs))
        ax.set_xticklabels(
            [SURFACE_LABEL[s].split("\n")[0] for s in surfaces], fontsize=8, rotation=18
        )
        ax.set_title(f"{block} pages", fontsize=9.5)

    axes[0].set_ylabel("Read throughput (Gb/s)")
    axes[0].set_ylim(0, (ceiling or 100) * 1.16)
    handles = [
        Patch(
            facecolor=SURFACE_FILL[s],
            edgecolor=EDGE,
            label=SURFACE_LABEL[s].split("\n")[0],
        )
        for s in surfaces
    ] + [
        plt.Line2D([0], [0], color="#444444", linewidth=1.3, label="QD16 (same cell)"),
        plt.Line2D(
            [0], [0], **{**CEILING_STYLE, "label": f"best measured {ceiling:.1f} Gb/s"}
        ),
    ]
    # A figure legend anchored above y=1 lands outside the canvas, because _save
    # deliberately does not use bbox_inches="tight". Keep it inside and reserve a
    # strip for it the same way the caption reserves one at the bottom.
    fig.legend(
        handles=handles,
        loc="upper center",
        ncol=len(handles),
        fontsize=7.6,
        bbox_to_anchor=(0.5, 0.995),
    )
    fig.__dict__["_legend_frac"] = 0.07
    _caption(
        fig,
        "This comparison stays on the planned fs_native surface: local target "
        "media versus remote XFS/md0. Bars are QD32, the measured saturation "
        "depth; the horizontal rule inside each bar is the SAME cell at QD16, "
        "which is offered-depth-limited rather than a second surface ceiling. The "
        "dashed line is the best rate measured anywhere in this sweep, not a "
        "nominal link rate. Whiskers span repeated cell minima and maxima. The "
        "direct-raw diagnostic surface is intentionally outside this figure. "
        "4 KiB and 16 KiB are per-request-cost measurements and belong on their "
        "own axes, not next to a 512 KiB bar.",
    )
    return _save(fig, outdir, "fio-capacity-by-block")


def merge_probe_rows(
    summaries: dict[str, dict[str, Any]], probes: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Return *summaries* with single-cell probe rows folded into their surface.

    The depth probes were run as separate one-cell sweeps, so their measurements
    live in their own summaries. Folding them in lets the depth figure plot the
    points that were actually measured rather than reciting them in a caption. A
    probe row that duplicates a depth already in the sweep is dropped, so the
    sweep's repeated cells always win over a single-rep probe.

    Args:
        summaries: Per-surface summary datasets.
        probes: Additional summary datasets, each typically one cell.

    Returns:
        A new summaries mapping; the inputs are not modified.
    """
    merged = {
        surface: {**data, "summary": list(data["summary"])}
        for surface, data in summaries.items()
    }
    for probe in probes:
        for row in probe["summary"]:
            surface = row["surface"]
            if surface not in merged:
                continue
            existing = merged[surface]["summary"]
            if any(
                other["kind"] == row["kind"]
                and other["bs"] == row["bs"]
                and other["qd"] == row["qd"]
                and other["requested_read_pct"] == row["requested_read_pct"]
                for other in existing
            ):
                continue
            existing.append(row)
    return merged


def _backup_capacity_surfaces(
    summaries: dict[str, dict[str, Any]],
) -> list[str]:
    """Return the two surfaces relevant to the fs_native capacity comparison."""
    return [
        surface
        for surface in BACKUP_CAPACITY_SURFACES
        if any(
            _rows(summaries, surface, "read", block, 32) for block in CAPACITY_BLOCKS
        )
    ]


def _backup_depth_points(
    summaries: dict[str, dict[str, Any]],
) -> list[tuple[int, float]]:
    """Return the measured remote-XFS 144 KiB saturation curve."""
    return sorted(
        (row["qd"], row["total_gbps_mean"])
        for row in summaries.get("remote_xfs", {"summary": []})["summary"]
        if row["kind"] == "read" and row["bs"] == "144k"
    )


def fig_depth_curve(summaries: dict[str, dict[str, Any]], outdir: Path) -> Path:
    """Why the repetitions sit at QD32: QD16 does not saturate this path.

    Args:
        summaries: Per-surface summary datasets, optionally with probe rows
            already folded in by :func:`merge_probe_rows`.
        outdir: Destination directory.

    Returns:
        The PNG path.
    """
    fig, ax = plt.subplots(figsize=(7.8, 4.2))
    points = _backup_depth_points(summaries)
    depths = [depth for depth, _ in points]
    if points:
        ax.plot(
            depths,
            [throughput for _, throughput in points],
            marker="o",
            markersize=5,
            linewidth=1.2,
            markerfacecolor=SURFACE_FILL["remote_xfs"],
            markeredgecolor=EDGE,
            color=SURFACE_LINE["remote_xfs"],
            label="Remote XFS/md0, 144 KiB",
            zorder=3,
        )
    ax.axvline(32, color="#999999", linestyle=":", linewidth=1.0, zorder=1)
    ax.text(
        32,
        0.02,
        " QD32: the depth the repetitions use",
        transform=ax.get_xaxis_transform(),
        fontsize=7.4,
        color="#777777",
        rotation=90,
        va="bottom",
    )
    ax.set_xticks(depths)
    ax.set_xlabel("Queue depth (offered outstanding requests)")
    ax.set_ylabel("Read throughput (Gb/s)")
    ax.set_title("Remote XFS/md0 reaches the 144 KiB plateau at QD32")
    if points:
        ax.legend(fontsize=8, loc="lower right")

    _caption(
        fig,
        "Every point is a measured accepted remote-XFS cell at 144 KiB. This is "
        "the fs_native surface, and the curve is why its repetitions sit at QD32 "
        "rather than QD16: QD16 is roughly 13 Gb/s short of the 95.7 Gb/s plateau "
        "and its own run-to-run spread was 5.5%. QD24, QD48, and QD64 are "
        "single-rep probes; QD64 was lower at 93.8 Gb/s, so it is not presented "
        "as a second plateau. The direct-raw diagnostic surface is intentionally "
        "outside this figure.",
    )
    return _save(fig, outdir, "fio-depth-curve")


def fig_small_blocks(summaries: dict[str, dict[str, Any]], outdir: Path) -> Path:
    """The small-page end: IOPS and latency, explicitly not a capacity result.

    Args:
        summaries: Per-surface summary datasets.
        outdir: Destination directory.

    Returns:
        The PNG path.
    """
    ceiling = _ceiling_of(summaries)
    surfaces = [
        s
        for s in SURFACE_ORDER
        if any(_rows(summaries, s, "read", b, 32) for b in SMALL_BLOCKS)
    ]
    blocks = SMALL_BLOCKS + CAPACITY_BLOCKS[:1]
    fig, (ax_bw, ax_iops) = plt.subplots(1, 2, figsize=(10.4, 4.2))

    # Capped so a single-surface run does not render one 0.8-wide slab per
    # category, which reads as a filled column rather than a bar.
    width = min(0.42, 0.8 / max(1, len(surfaces)))
    for index, surface in enumerate(surfaces):
        offset = (index - (len(surfaces) - 1) / 2) * width
        bw, iops, xs = [], [], []
        for position, block in enumerate(blocks):
            row = _rows(summaries, surface, "read", block, 32)
            if row is None:
                continue
            xs.append(position + offset)
            bw.append(row["total_gbps_mean"])
            iops.append(row["read_iops_mean"] / 1000.0)
        label = SURFACE_LABEL[surface].split("\n")[0]
        ax_bw.bar(
            xs,
            bw,
            width,
            color=SURFACE_FILL[surface],
            edgecolor=EDGE,
            linewidth=0.8,
            zorder=3,
            label=label,
        )
        ax_iops.bar(
            xs,
            iops,
            width,
            color=SURFACE_FILL[surface],
            edgecolor=EDGE,
            linewidth=0.8,
            zorder=3,
            label=label,
        )

    if ceiling:
        ax_bw.axhline(ceiling, **CEILING_STYLE)
        # Below the line and left-aligned: the reference bar (the largest block)
        # is rightmost and reaches the ceiling, so a right-aligned label would
        # overprint it, and an above-line label would sit outside the axes.
        ax_bw.text(
            0.02,
            ceiling - ceiling * 0.035,
            f" large-block ceiling {ceiling:.1f} Gb/s",
            transform=ax_bw.get_yaxis_transform(),
            ha="left",
            va="top",
            fontsize=7.5,
            color="#777777",
        )
        ax_bw.set_ylim(0, ceiling * 1.2)
    for ax, ylabel, title in (
        (
            ax_bw,
            "Read throughput (Gb/s)",
            "Bandwidth: small pages are far from the ceiling",
        ),
        (ax_iops, "Read IOPS (thousands)", "IOPS: the limit small pages actually hit"),
    ):
        ax.set_xticks(range(len(blocks)))
        ax.set_xticklabels(blocks, fontsize=9)
        ax.set_xlabel("Block size")
        ax.set_ylabel(ylabel)
        ax.set_title(title, fontsize=9.5)
    ax_bw.legend(fontsize=7.6, loc="upper center", ncol=3)

    _caption(
        fig,
        "The 144 KiB bar is present only as the scale reference; this figure is "
        "about the two small sizes. Read the left panel as what a small-page "
        "geometry could reach on this path, NOT as a surface capacity result: the "
        "binding limit at 4 KiB is per-request cost, which is why the right panel "
        "rises as the left one falls. Both panels are QD32.",
    )
    return _save(fig, outdir, "fio-small-blocks")


def fig_fio_vs_l2_mixed(
    summaries: dict[str, dict[str, Any]],
    l2_data: dict[str, Any],
    outdir: Path,
    l2_profile: str = "DeepSeek-V3",
) -> Path:
    """The comparator: does the mixed L2 shortfall live in the storage path?

    Pairs each fio ``randrw`` cell against the ``bench l2`` run requesting the
    same byte mix. If the storage path alone accounts for the duplex cost, the
    two bars match; a residual gap is attributable above the storage path.

    Args:
        summaries: Per-surface summary datasets; the XFS surface is used.
        l2_data: ``geom_collect.py`` dataset carrying the L2 mixed cells.
        outdir: Destination directory.
        l2_profile: Profile whose geometry matches the fio mixed block size.

    Returns:
        The PNG path.
    """
    profile = l2_data["profiles"][l2_profile]
    pairs = [
        (mix, ratio, _rows(summaries, "remote_xfs", "mixed", "144k", 32, mix))
        for mix, ratio in MIX_PAIRS
        if _rows(summaries, "remote_xfs", "mixed", "144k", 32, mix) is not None
    ]
    fig, ax = plt.subplots(figsize=(8.4, 4.4))
    xs = range(len(pairs))
    width = 0.26

    for index, (mix, ratio, row) in enumerate(pairs):
        ax.bar(
            index - width / 2,
            row["read_gbps_mean"],
            width,
            color=FILL["mixed"],
            edgecolor=EDGE,
            linewidth=0.8,
            zorder=3,
        )
        ax.bar(
            index - width / 2,
            row["write_gbps_mean"],
            width,
            bottom=row["read_gbps_mean"],
            color=FILL["write"],
            edgecolor=EDGE,
            linewidth=0.8,
            zorder=3,
        )
        ax.text(
            index - width / 2,
            row["total_gbps_mean"] + 1.2,
            f"{row['total_gbps_mean']:.1f}",
            ha="center",
            fontsize=7.5,
        )

        l2 = _l2_cell(profile, ratio)
        if l2 is None:
            ax.text(
                index + width / 2,
                2.0,
                "L2 cell\nnot run",
                ha="center",
                fontsize=7.5,
                color="#777777",
            )
            continue
        ax.bar(
            index + width / 2,
            l2["read_gbps"],
            width,
            color=FILL["l2"],
            edgecolor=EDGE,
            linewidth=0.8,
            zorder=3,
        )
        ax.bar(
            index + width / 2,
            l2["write_gbps"],
            width,
            bottom=l2["read_gbps"],
            color=FILL["write"],
            edgecolor=EDGE,
            linewidth=0.8,
            zorder=3,
        )
        ax.text(
            index + width / 2,
            l2["total_gbps"] + 1.2,
            f"{l2['total_gbps']:.1f}",
            ha="center",
            fontsize=7.5,
        )

    ax.set_xticks(list(xs))
    ax.set_xticklabels(
        [
            f"{mix}% read bytes\n(fio rwmixread={mix} / bench l2 {ratio})"
            for mix, ratio, _ in pairs
        ],
        fontsize=8.5,
    )
    ax.set_ylabel("Throughput (Gb/s)")
    ax.set_title("Mixed duplex cost: fio randrw against bench l2 at the same byte mix")
    handles = [
        Patch(facecolor=FILL["mixed"], edgecolor=EDGE, label="fio: read component"),
        Patch(facecolor=FILL["l2"], edgecolor=EDGE, label="bench l2: read component"),
        Patch(facecolor=FILL["write"], edgecolor=EDGE, label="write component (both)"),
    ]
    ax.legend(handles=handles, fontsize=8, ncol=3, loc="upper center")
    ax.set_ylim(0, 112)
    _pin_category_axis(ax, len(pairs))

    _caption(
        fig,
        "The pairing is exact in ONE respect only -- both sides express the mix as "
        "a fraction of bytes, so rwmixread=83 and 5:1 (5/6 = 83.3%) request the "
        "same thing and need no conversion. They differ in every other respect: "
        "the fio cell is 60 s with a 10 s ramp and no verification, the L2 cell is "
        "an unwarmed 60 s window with byte-verified readback, because bench l2 "
        "rejects --warmup-sec together with --read-write-ratio. Both asymmetries "
        "bias the L2 bar low, so a residual gap is an upper bound on the "
        "above-storage cost, not a measurement of it. Single-initiator L2 cells "
        "only; multi-process fan-in is a separate figure.",
    )
    return _save(fig, outdir, "fio-vs-l2-mixed")


def fig_model_geometry_mixed(l2_data: dict[str, Any], outdir: Path) -> Path:
    """Reads are geometry-blind; mixed is not.

    Every profile reads at the fabric ceiling regardless of page geometry or
    process count, so a read-only chart cannot distinguish them. Under duplex
    load the same four geometries diverge, and two of them lose throughput as
    processes are added. That divergence is the point of this figure: it is the
    only place in the dataset where model geometry changes the answer.

    Args:
        l2_data: ``geom_collect.py`` dataset carrying per-profile cells.
        outdir: Destination directory.

    Returns:
        The PNG path.
    """
    profiles = list(l2_data["profiles"])
    counts = sorted(
        {row["initiators"] for p in l2_data["profiles"].values() for row in p["mixed"]}
    )
    fig, (ax_read, ax_mixed) = plt.subplots(1, 2, figsize=(11.4, 5.2), sharey=True)
    # One marker per initiator count, so a reader can follow a single fan-in
    # level across profiles without counting bar positions.
    markers = ["o", "s", "^", "D"]

    for axis, kind in ((ax_read, "read"), (ax_mixed, "mixed")):
        for index, count in enumerate(counts):
            values: list[float | None] = []
            for name in profiles:
                cells = [
                    row
                    for row in l2_data["profiles"][name][kind]
                    if row["initiators"] == count and row.get("accepted", True)
                ]
                if not cells:
                    values.append(None)
                    continue
                # The read cells carry `gbps`; the mixed cells carry a split
                # read/write pair plus `total_gbps`. Chart the comparable total.
                key = "total_gbps" if kind == "mixed" else "gbps"
                values.append(max(row[key] for row in cells))
            xs = [i for i, v in enumerate(values) if v is not None]
            ys = [v for v in values if v is not None]
            axis.plot(
                xs,
                ys,
                marker=markers[index % len(markers)],
                markersize=6,
                linewidth=1.2,
                color=SURFACE_LINE["remote_xfs"] if kind == "read" else "#b3542f",
                alpha=0.5 + 0.5 * index / max(1, len(counts) - 1),
                markerfacecolor=FILL["read"] if kind == "read" else FILL["write"],
                markeredgecolor=EDGE,
                label=f"{count} initiator" + ("s" if count > 1 else ""),
                zorder=3,
            )
        axis.set_xticks(range(len(profiles)))
        axis.set_xticklabels(profiles, fontsize=8.2, rotation=18, ha="right")
        axis.legend(fontsize=7.6, ncol=len(counts), loc="lower center")
        # Rotated labels extend below the axes, and _save reserves height only
        # for the caption; without margin here the profile names are clipped.
        axis.set_xlim(-0.45, len(profiles) - 0.55)

    ceiling = l2_data.get("ceiling_gbps", 0.0)
    if ceiling:
        for axis in (ax_read, ax_mixed):
            axis.axhline(ceiling, **CEILING_STYLE)
        # The read series sit ON the ceiling, so a label just under the line
        # overprints them. Put it above the line instead; ylim leaves room.
        ax_read.text(
            0.02,
            ceiling + 1.2,
            f" measured read ceiling {ceiling:.1f} Gb/s",
            transform=ax_read.get_yaxis_transform(),
            ha="left",
            va="bottom",
            fontsize=7.4,
            color="#777777",
        )
    ax_read.set_ylabel("Throughput (Gb/s)")
    ax_read.set_ylim(0, 108)
    ax_read.set_title("100% read: every geometry sits at the ceiling", fontsize=9.5)
    ax_mixed.set_title("5:1 mixed: the geometries separate", fontsize=9.5)

    _caption(
        fig,
        "Both panels are bench l2 fs_native on remote XFS/md0, same fabric and "
        "same 5:1 byte mix; only page geometry and process count differ. Read the "
        "left panel as a null result -- it is evidence that geometry does NOT "
        "matter for pure reads, which is what makes the right panel meaningful. On "
        "the right, Llama-3 405B falls from 72.2 to 44.7 Gb/s and Llama-3 70B from "
        "71.9 to 65.4 as processes go 2 to 4, while DeepSeek-V3 and Mixtral 8x22B "
        "keep rising. Cause is not established: multi-process fan-in and page "
        "geometry vary together here, so this figure locates the effect and does "
        "not explain it. Single run per cell at 4 initiators, so the 405B drop "
        "wants a repetition before it carries weight in a decision.",
    )
    return _save(fig, outdir, "l2-model-geometry-mixed")


def fig_offered_concurrency(
    summaries: dict[str, dict[str, Any]], l2_data: dict[str, Any], outdir: Path
) -> Path:
    """The same saturation question asked at two layers.

    fio offers depth through libaio against the block device; bench l2 offers it
    as in-flight submissions through the fs_native pool. Plotting them on one
    axis shows whether the two layers saturate at the same offered concurrency,
    which is what justifies comparing their absolute numbers at all.

    Args:
        summaries: Per-surface summary datasets, for the fio depth points.
        l2_data: ``geom_collect.py`` dataset carrying the in-flight sweep.
        outdir: Destination directory.

    Returns:
        The PNG path.
    """
    fig, ax = plt.subplots(figsize=(8.0, 4.4))

    fio_points = sorted(
        (row["qd"], row["total_gbps_mean"])
        for row in summaries.get("remote_xfs", {"summary": []})["summary"]
        if row["kind"] == "read" and row["bs"] == "144k"
    )
    if fio_points:
        ax.plot(
            [p[0] for p in fio_points],
            [p[1] for p in fio_points],
            marker="o",
            markersize=5.5,
            linewidth=1.3,
            color=SURFACE_LINE["remote_xfs"],
            markerfacecolor=SURFACE_FILL["remote_xfs"],
            markeredgecolor=EDGE,
            label="fio libaio depth (144k, remote XFS/md0)",
            zorder=3,
        )

    l2_points = sorted((row["in_flight"], row["gbps"]) for row in l2_data["inflight"])
    ax.plot(
        [p[0] for p in l2_points],
        [p[1] for p in l2_points],
        marker="D",
        markersize=5.5,
        linewidth=1.3,
        linestyle="--",
        color="#b3542f",
        markerfacecolor=FILL["write"],
        markeredgecolor=EDGE,
        label="bench l2 in-flight (W=32, DeepSeek-V3 144k)",
        zorder=3,
    )

    ax.set_xscale("log", base=2)
    ax.set_xticks([1, 2, 4, 8, 16, 24, 32, 48, 64])
    ax.get_xaxis().set_major_formatter(matplotlib.ticker.ScalarFormatter())
    ax.set_xlabel("Offered concurrency (fio iodepth / bench l2 in-flight)")
    ax.set_ylabel("Read throughput (Gb/s)")
    ax.set_title("Both layers saturate, but not at the same offered concurrency")
    ax.legend(fontsize=8, loc="lower right")
    ax.set_ylim(0, 108)

    _caption(
        fig,
        "The x axis is NOT one quantity: fio's iodepth is outstanding block "
        "requests from one process, bench l2's in-flight is concurrent submissions "
        "across a 32-worker pool, and each l2 submission fans into per-tile "
        "open/read/close work. They are plotted together because they are the two "
        "knobs that control offered concurrency at their respective layers, not "
        "because 8 means the same thing on both curves. The usable reading is the "
        "SHAPE: l2 reaches the ceiling by 8 in-flight and stays flat to 32, while "
        "fio needs 24-32 iodepth to get there -- so neither curve's plateau is an "
        "artifact of stopping too early.",
    )
    return _save(fig, outdir, "l2-vs-fio-offered-concurrency")


def fig_worker_pool(l2_data: dict[str, Any], outdir: Path) -> Path:
    """Where the fs_native pool stops scaling, and what NUMA binding does not fix.

    Args:
        l2_data: ``geom_collect.py`` dataset carrying the worker and NUMA sweeps.
        outdir: Destination directory.

    Returns:
        The PNG path.
    """
    fig, ax = plt.subplots(figsize=(8.2, 4.4))
    series = [
        ("none", "Unbound", "#4a7fb5", FILL["read"], "o", "-"),
        ("node0", "Bound to node0", "#4a9a45", FILL["mixed"], "s", "--"),
        ("node1", "Bound to node1", "#b3542f", FILL["write"], "^", ":"),
    ]
    rows = l2_data["workers"] + l2_data["numa"]
    for binding, label, color, fill, marker, style in series:
        points = sorted(
            (row["workers"], row["gbps"])
            for row in rows
            if row.get("binding") == binding
        )
        if not points:
            continue
        # node1 was probed at one pool size only. A single point drawn with a
        # linestyle reads as a series whose line is missing, so mark it as the
        # spot check it is rather than implying an unmeasured curve.
        single = len(points) == 1
        ax.plot(
            [p[0] for p in points],
            [p[1] for p in points],
            marker="X" if single else marker,
            markersize=8.5 if single else 5.5,
            linewidth=0 if single else 1.3,
            linestyle="none" if single else style,
            color=color,
            markerfacecolor=fill,
            markeredgecolor=EDGE,
            label=f"{label} (single point)" if single else label,
            zorder=4 if single else 3,
        )

    ax.axvline(32, color="#999999", linestyle=":", linewidth=1.0, zorder=1)
    ax.text(
        32,
        0.02,
        " W=32: the configuration every other figure uses",
        transform=ax.get_xaxis_transform(),
        rotation=90,
        fontsize=7.2,
        color="#777777",
        va="bottom",
    )
    ax.set_xticks([24, 32, 40, 48, 64])
    ax.set_xlabel("fs_native worker-pool size")
    ax.set_ylabel("Read throughput (Gb/s)")
    ax.set_title("The pool peaks at 32 workers and degrades past it")
    ax.legend(fontsize=8, loc="lower left")
    ax.set_ylim(0, 108)

    _caption(
        fig,
        "100% read, 8 in-flight, DeepSeek-V3 144k geometry. THE CAUSE OF THE "
        "DECLINE IS NOT ESTABLISHED -- this figure records that it is real and "
        "reproducible across bindings, not why it happens. What it does rule out "
        "is a simple cross-socket-memory explanation: pinning to node0 raises "
        "every point but does not remove the knee, and node0 versus node1 at W=64 "
        "differ by under 2 Gb/s. Single run per point. Read it as the reason the "
        "rest of the sweep is configured at W=32, and as an open question, not as "
        "a tuning recommendation.",
    )
    return _save(fig, outdir, "l2-worker-pool-scaling")


def fig_evidence(summaries: dict[str, dict[str, Any]], outdir: Path) -> Path:
    """The acceptance record: what gated, what corroborated, what was excluded.

    Args:
        summaries: Per-surface summary datasets.
        outdir: Destination directory.

    Returns:
        The PNG path.
    """
    fig, ax = plt.subplots(figsize=(9.0, 4.6))
    ax.axis("off")
    ax.grid(False)

    lines: list[tuple[str, str]] = []
    for surface in SURFACE_ORDER:
        data = summaries.get(surface)
        if data is None:
            lines.append((SURFACE_LABEL[surface].split("\n")[0], "not run"))
            continue
        overruns = sum(row["bracket_overruns"] for row in data["summary"])
        ratios = [r for row in data["summary"] for r in row["counter_ratios"]]
        detail = (
            f"{data['accepted']}/{data['total']} cells accepted; "
            f"corpus unchanged: {data['corpus_unchanged']}; "
            f"bracket overruns: {overruns}"
        )
        if ratios:
            detail += f"; counter ratio {min(ratios):.2f}-{max(ratios):.2f}"
        lines.append((SURFACE_LABEL[surface].split("\n")[0], detail))

    y = 0.94
    ax.text(
        0.0,
        y,
        "Acceptance record",
        fontsize=10.5,
        fontweight="bold",
        transform=ax.transAxes,
    )
    y -= 0.11
    for label, detail in lines:
        ax.text(0.0, y, label, fontsize=9, fontweight="bold", transform=ax.transAxes)
        ax.text(0.30, y, detail, fontsize=8.2, transform=ax.transAxes)
        y -= 0.085

    y -= 0.04
    ax.text(
        0.0,
        y,
        "Gates (fatal to a cell)",
        fontsize=9.5,
        fontweight="bold",
        transform=ax.transAxes,
    )
    y -= 0.075
    for text in (
        "Six fabric error counters must be zero: RetransSegs, Nak Sequence Error, "
        "RTO, RNR received, Rcvd Out of order packets, InProtoErrors.",
        "Counters must be present; a missing counter means no verdict is possible.",
        "Run level: the bench l2 corpus file count must be unchanged.",
    ):
        ax.text(0.02, y, f"• {text}", fontsize=8.2, transform=ax.transAxes)
        y -= 0.068

    y -= 0.02
    ax.text(
        0.0,
        y,
        "Reported but NOT gating",
        fontsize=9.5,
        fontweight="bold",
        transform=ax.transAxes,
    )
    y -= 0.075
    for text in (
        "RDMA counter agreement with fio bandwidth: the segment constant is "
        "calibrated per geometry and is not expected to hold at every block size.",
        "Bracket overrun: flags a cell whose wall-clock window exceeded "
        "ramp+runtime, which suppresses that cell's counter cross-check.",
    ):
        ax.text(0.02, y, f"• {text}", fontsize=8.2, transform=ax.transAxes)
        y -= 0.068

    _caption(
        fig,
        "Falcon-offloaded kernel NVMe-oF, existing-controller, single-process "
        "fs_native sustained load. Not evidence of 64-QP or fresh-QP scale, R2, "
        "physical multi-initiator operation, 400 GbE, or any Falcon offload "
        "BENEFIT -- there is no unoffloaded control in this sweep.",
    )
    return _save(fig, outdir, "fio-sweep-evidence")


def _parse_summary(value: str) -> tuple[str, Path]:
    """Parse one ``surface=path`` command-line value.

    Args:
        value: A ``surface=path`` pair, e.g. ``remote_xfs=xfs.json``.

    Returns:
        The surface name and its summary path.

    Raises:
        argparse.ArgumentTypeError: If the value is malformed or names an
            unknown surface.
    """
    surface, sep, path = value.partition("=")
    if not sep or not surface or not path:
        raise argparse.ArgumentTypeError("summary must use SURFACE=PATH")
    if surface not in SURFACE_ORDER:
        raise argparse.ArgumentTypeError(
            f"unknown surface {surface!r}; expected one of {', '.join(SURFACE_ORDER)}"
        )
    return surface, Path(path)


def main() -> None:
    """Render every figure the supplied data supports and print the paths."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--summary",
        action="append",
        type=_parse_summary,
        required=True,
        metavar="SURFACE=PATH",
        help="fio_sweep_report.py output for one surface; repeatable.",
    )
    parser.add_argument(
        "--l2-data",
        type=Path,
        default=None,
        help="geom_collect.py dataset; enables the fio-vs-L2 mixed comparison.",
    )
    parser.add_argument(
        "--probe",
        action="append",
        type=Path,
        default=[],
        help="Additional single-cell fio_sweep_report.py summary, e.g. a depth "
        "probe; repeatable. Used by the depth figure only.",
    )
    parser.add_argument("--outdir", type=Path, required=True)
    args = parser.parse_args()

    summaries = {
        surface: json.loads(path.read_text()) for surface, path in args.summary
    }
    probes = [json.loads(path.read_text()) for path in args.probe]
    args.outdir.mkdir(parents=True, exist_ok=True)
    _style()

    written: list[Path] = [
        fig_capacity_ladder(summaries, args.outdir),
        fig_capacity_by_block(summaries, args.outdir),
        fig_depth_curve(merge_probe_rows(summaries, probes), args.outdir),
        fig_small_blocks(summaries, args.outdir),
        fig_evidence(summaries, args.outdir),
    ]
    if args.l2_data is not None:
        l2_data = json.loads(args.l2_data.read_text())
        written.append(fig_fio_vs_l2_mixed(summaries, l2_data, args.outdir))
        written.append(fig_model_geometry_mixed(l2_data, args.outdir))
        # Probe-merged, like the depth figure: without the probes the fio curve is
        # only its two sweep depths, which would not support a shape claim.
        written.append(
            fig_offered_concurrency(
                merge_probe_rows(summaries, probes), l2_data, args.outdir
            )
        )
        written.append(fig_worker_pool(l2_data, args.outdir))
    else:
        print("note: --l2-data not given; skipping the four L2 figures")

    for path in written:
        print(path)


if __name__ == "__main__":
    main()
