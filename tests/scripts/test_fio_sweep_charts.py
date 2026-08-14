# SPDX-License-Identifier: Apache-2.0
"""Tests for the FIO capacity-sweep chart module's data selection.

The figures themselves are checked by eye. What is tested here is the layer that
decides WHICH number goes on a chart -- cell matching, the measured ceiling, the
L2 pairing, and the CLI surface names -- because that is where a silently wrong
chart comes from.
"""

from __future__ import annotations

# Standard
import argparse
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

# Third Party
import pytest


SCRIPT = Path(__file__).parents[2] / "scripts/ipu-poc/fio_sweep_charts.py"

# The chart script is a plotting tool, not part of the package, so matplotlib is
# not in the test requirements. Skip rather than fail the whole run: the selection
# logic these tests cover is only reachable through the module import.
pytest.importorskip("matplotlib")


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fio_sweep_charts", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


charts = _load_module()


def _row(
    kind: str = "read",
    bs: str = "144k",
    qd: int = 32,
    read_pct: int = 100,
    total: float = 95.7,
    bound: str = "capacity",
) -> dict[str, object]:
    """Build one summary row with the fields the charts read."""
    read = total * read_pct / 100
    return {
        "surface": "remote_xfs",
        "kind": kind,
        "requested_read_pct": read_pct,
        "bs": bs,
        "bs_bytes": 147456,
        "qd": qd,
        "reps": 3,
        "bound": bound,
        "total_gbps_mean": total,
        "total_gbps_min": total - 0.1,
        "total_gbps_max": total + 0.1,
        "spread_pct": 0.2,
        "read_gbps_mean": read,
        "write_gbps_mean": total - read,
        "achieved_read_pct_mean": float(read_pct),
        "read_lat_p99_ms_max": 0.66,
        "read_iops_mean": 81124.0,
        "bracket_overruns": 0,
        "counter_ratios": [0.99],
    }


def _summary(*rows: dict[str, object]) -> dict[str, object]:
    """Wrap rows in the summary envelope fio_sweep_report.py writes."""
    return {
        "context": {"surface": "remote_xfs"},
        "cells": [],
        "summary": list(rows),
        "accepted": len(rows),
        "total": len(rows),
        "corpus_unchanged": True,
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    [("5:1", 5.0), ("9:1", 9.0), ("1:1", 1.0), ("10:2", 5.0)],
)
def test_parse_ratio_accepts_read_write_form(value: str, expected: float) -> None:
    assert charts._parse_ratio(value) == expected


@pytest.mark.parametrize("value", ["5", "5:0", "0:1", "-5:1", "", "5:1:1", "a:b"])
def test_parse_ratio_rejects_malformed(value: str) -> None:
    with pytest.raises(ValueError):
        charts._parse_ratio(value)


def test_rows_matches_on_every_cell_parameter() -> None:
    """A cell is selected by kind, block, depth, and requested mix together."""
    summaries = {
        "remote_xfs": _summary(
            _row(qd=16, total=82.3),
            _row(qd=32, total=95.7),
            _row(kind="mixed", read_pct=83, total=71.9),
            _row(kind="mixed", read_pct=90, total=73.7),
            _row(bs="256k", total=95.9),
        )
    }
    assert (
        charts._rows(summaries, "remote_xfs", "read", "144k", 32)["total_gbps_mean"]
        == 95.7
    )
    assert (
        charts._rows(summaries, "remote_xfs", "read", "144k", 16)["total_gbps_mean"]
        == 82.3
    )
    assert (
        charts._rows(summaries, "remote_xfs", "read", "256k", 32)["total_gbps_mean"]
        == 95.9
    )
    mixed83 = charts._rows(summaries, "remote_xfs", "mixed", "144k", 32, 83)
    mixed90 = charts._rows(summaries, "remote_xfs", "mixed", "144k", 32, 90)
    assert mixed83["total_gbps_mean"] == 71.9
    assert mixed90["total_gbps_mean"] == 73.7


def test_rows_returns_none_for_a_cell_that_was_not_run() -> None:
    """An unrun cell is absent, not silently substituted by a neighbour."""
    summaries = {"remote_xfs": _summary(_row(qd=32))}
    assert charts._rows(summaries, "remote_xfs", "read", "144k", 48) is None
    assert charts._rows(summaries, "remote_xfs", "mixed", "144k", 32, 83) is None
    assert charts._rows(summaries, "local_raw", "read", "144k", 32) is None


def test_ceiling_uses_only_saturated_large_block_read_cells() -> None:
    """The reference line is a measured large-block saturated read rate.

    A sub-saturation cell, a small-block cell, and a mixed cell are all higher-
    or lower-bound artifacts of their own shape, so none of them may set the
    line a reader will treat as this path's ceiling.
    """
    summaries = {
        "remote_xfs": _summary(
            _row(total=95.7),
            _row(qd=16, total=82.3, bound="offered-depth"),
            _row(bs="4k", total=9.4),
            _row(bs="16k", total=30.3),
            _row(kind="mixed", read_pct=83, total=71.9),
        ),
        "remote_raw": _summary(_row(total=96.4)),
    }
    assert charts._ceiling_of(summaries) == 96.4


def test_ceiling_is_zero_when_no_saturated_cell_exists() -> None:
    """With nothing saturated there is no measured ceiling to draw."""
    summaries = {"remote_xfs": _summary(_row(qd=16, total=82.3, bound="offered-depth"))}
    assert charts._ceiling_of(summaries) == 0.0


def test_backup_capacity_surfaces_exclude_direct_raw() -> None:
    """The general large-page chart stays on the local/fs_native path."""
    local = _row()
    local["surface"] = "local_raw"
    raw = _row()
    raw["surface"] = "remote_raw"
    xfs = _row()
    xfs["surface"] = "remote_xfs"
    summaries = {
        "local_raw": _summary(local),
        "remote_raw": _summary(raw),
        "remote_xfs": _summary(xfs),
    }

    assert charts._backup_capacity_surfaces(summaries) == ["local_raw", "remote_xfs"]


def test_backup_depth_points_use_only_remote_xfs_144k() -> None:
    """The depth chart establishes the selected fs_native saturation point."""
    qd8 = _row(qd=8, total=53.5, bound="offered-depth")
    qd32 = _row(qd=32, total=95.7)
    raw = _row(bs="512k", qd=32, total=82.3)
    raw["surface"] = "remote_raw"
    local = _row(qd=32, total=107.8)
    local["surface"] = "local_raw"
    summaries = {
        "local_raw": _summary(local),
        "remote_raw": _summary(raw),
        "remote_xfs": _summary(qd8, qd32),
    }

    assert charts._backup_depth_points(summaries) == [(8, 53.5), (32, 95.7)]


def test_repeat_whiskers_preserve_the_observed_min_max_range() -> None:
    """Capacity bars show repetition range instead of hiding an unstable mean."""
    row = _row(total=82.3)
    row["total_gbps_min"] = 76.5
    row["total_gbps_max"] = 91.2
    row["spread_pct"] = 19.3

    assert charts._repeat_whiskers(row) == pytest.approx((5.8, 8.9))
    assert charts._is_unstable(row) is True


def test_capacity_ladder_renders_an_unstable_cell(tmp_path: Path) -> None:
    """A wide-spread cell still renders; its whisker sets the axis headroom.

    The ladder is rendered per block size, so at 512 KiB one bar is the unstable
    remote-raw cell. Its annotation sits above the whisker cap, which is taller
    than the mean the ylim used to be derived from.
    """
    charts._style()
    row = _row(bs="512k", total=82.3)
    row["total_gbps_min"] = 76.5
    row["total_gbps_max"] = 91.2
    row["spread_pct"] = 19.3
    summaries = {"remote_xfs": _summary(row)}

    path = charts.fig_capacity_ladder(summaries, tmp_path, block="512k")

    assert path.is_file()
    assert path.with_suffix(".svg").is_file()


def _l2_profile(*cells: dict[str, object]) -> dict[str, object]:
    """Wrap L2 mixed cells in the profile shape geom_collect.py writes."""
    return {"mixed": list(cells)}


def _l2_row(
    ratio: float = 4.99954488565252,
    initiators: int = 1,
    accepted: bool = True,
    total: float = 63.2,
) -> dict[str, object]:
    """Build one L2 mixed cell with the fields the comparison figure reads."""
    read = total * ratio / (ratio + 1)
    return {
        "initiators": initiators,
        "accepted": accepted,
        "ratio": ratio,
        "read_gbps": read,
        "write_gbps": total - read,
        "total_gbps": total,
    }


def test_l2_cell_matches_an_achieved_ratio_not_a_requested_label() -> None:
    """A 5:1 request lands as 4.9995 in the dataset and must still match."""
    profile = _l2_profile(_l2_row(ratio=4.99954488565252, total=63.2))
    assert charts._l2_cell(profile, "5:1")["total_gbps"] == 63.2


def test_l2_cell_rejects_a_different_ratio() -> None:
    """A 5:1 cell must not be drawn against the 90%-read fio bar."""
    profile = _l2_profile(_l2_row(ratio=4.99954488565252))
    assert charts._l2_cell(profile, "9:1") is None


def test_l2_cell_ignores_multi_initiator_and_rejected_cells() -> None:
    """Only accepted single-process cells are comparable to a one-job fio cell."""
    profile = _l2_profile(
        _l2_row(initiators=4, total=88.0),
        _l2_row(accepted=False, total=99.0),
        _l2_row(total=63.2),
    )
    assert charts._l2_cell(profile, "5:1")["total_gbps"] == 63.2


def test_l2_cell_returns_none_when_only_multi_initiator_cells_exist() -> None:
    profile = _l2_profile(_l2_row(initiators=4), _l2_row(initiators=2))
    assert charts._l2_cell(profile, "5:1") is None


def test_mix_pairs_request_the_same_byte_fraction_on_both_sides() -> None:
    """Each fio rwmixread is paired with the L2 ratio requesting that fraction.

    This is the whole basis of the comparison figure: if the pairing table drifts
    the two bars stop being comparable while still looking like a pair.
    """
    for read_pct, ratio in charts.MIX_PAIRS:
        factor = charts._parse_ratio(ratio)
        assert factor / (factor + 1) * 100 == pytest.approx(read_pct, abs=0.34)


def test_parse_summary_accepts_a_known_surface(tmp_path: Path) -> None:
    surface, path = charts._parse_summary(f"remote_xfs={tmp_path / 'x.json'}")
    assert surface == "remote_xfs"
    assert path == tmp_path / "x.json"


@pytest.mark.parametrize("value", ["remote_xfs", "=x.json", "remote_xfs=", ""])
def test_parse_summary_rejects_malformed(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        charts._parse_summary(value)


def test_parse_summary_rejects_an_unknown_surface() -> None:
    """A typo'd surface must fail loudly, not render an empty figure."""
    with pytest.raises(argparse.ArgumentTypeError, match="unknown surface"):
        charts._parse_summary("remote_ext4=x.json")


def _l2_dataset() -> dict[str, object]:
    """Build a geom dataset carrying every sweep the L2 figures consume."""
    return {
        "ceiling_gbps": 95.92,
        "profiles": {
            "DeepSeek-V3": {
                "read": [
                    {"initiators": 1, "gbps": 95.33, "accepted": True},
                    {"initiators": 4, "gbps": 95.14, "accepted": True},
                ],
                "mixed": [_l2_row(total=63.2), _l2_row(initiators=4, total=72.2)],
            },
            "Llama-3 405B": {
                "read": [{"initiators": 1, "gbps": 95.64, "accepted": True}],
                "mixed": [_l2_row(total=69.3), _l2_row(initiators=4, total=44.7)],
            },
        },
        "inflight": [
            {"in_flight": 1, "gbps": 55.7, "workers": 32},
            {"in_flight": 8, "gbps": 95.3, "workers": 32},
        ],
        "workers": [
            {"workers": 32, "gbps": 95.27, "binding": "none"},
            {"workers": 64, "gbps": 41.55, "binding": "none"},
        ],
        "numa": [
            {"workers": 32, "gbps": 95.45, "binding": "node0"},
            {"workers": 64, "gbps": 53.97, "binding": "node1"},
        ],
    }


def test_l2_figures_render_from_the_geom_dataset(tmp_path: Path) -> None:
    """The three L2-only figures survive a dataset with sparse per-profile cells.

    Llama-3 405B has one read cell and node1 has one worker point, so these
    figures must handle a series too short to draw a line.
    """
    charts._style()
    summaries = {"remote_xfs": _summary(_row(total=95.7), _row(qd=16, total=82.3))}
    l2_data = _l2_dataset()
    for path in (
        charts.fig_model_geometry_mixed(l2_data, tmp_path),
        charts.fig_offered_concurrency(summaries, l2_data, tmp_path),
        charts.fig_worker_pool(l2_data, tmp_path),
    ):
        assert path.is_file()
        assert path.with_suffix(".svg").is_file()


def test_offered_concurrency_survives_a_missing_xfs_surface(tmp_path: Path) -> None:
    """The L2 curve is plottable before any fio surface has been measured."""
    charts._style()
    assert charts.fig_offered_concurrency({}, _l2_dataset(), tmp_path).is_file()


def test_every_figure_renders_from_one_surface(tmp_path: Path) -> None:
    """The full figure set survives a single-surface dataset.

    The sweep runs one surface at a time, so the charts are rendered before the
    other two exist. Every figure must degrade rather than raise.
    """
    charts._style()
    summaries = {
        "remote_xfs": _summary(
            _row(total=95.7),
            _row(qd=16, total=82.3, bound="offered-depth"),
            _row(bs="256k", total=95.9),
            _row(bs="512k", total=95.9),
            _row(bs="4k", total=9.4),
            _row(bs="16k", total=30.3),
            _row(kind="mixed", read_pct=83, total=71.9),
            _row(kind="mixed", read_pct=90, total=73.7),
        )
    }
    l2_data = {
        "profiles": {
            "DeepSeek-V3": {
                "mixed": [
                    {
                        "initiators": 1,
                        "accepted": True,
                        "ratio": 4.99954488565252,
                        "read_gbps": 52.7,
                        "write_gbps": 10.5,
                        "total_gbps": 63.2,
                    }
                ]
            }
        }
    }
    for path in (
        charts.fig_capacity_ladder(summaries, tmp_path),
        charts.fig_capacity_by_block(summaries, tmp_path),
        charts.fig_depth_curve(summaries, tmp_path),
        charts.fig_small_blocks(summaries, tmp_path),
        charts.fig_fio_vs_l2_mixed(summaries, l2_data, tmp_path),
        charts.fig_evidence(summaries, tmp_path),
    ):
        assert path.is_file()
        assert path.with_suffix(".svg").is_file()
