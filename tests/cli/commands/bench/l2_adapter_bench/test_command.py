# SPDX-License-Identifier: Apache-2.0
"""Tests for ``bench l2`` argument validation and warmup stripping."""

# Standard
from pathlib import Path
from unittest.mock import MagicMock
import argparse
import json

# Third Party
import pytest

# First Party
from lmcache.cli.commands.bench.l2_adapter_bench.command import (
    _strip_warmup,
    add_l2_arguments,
    run_l2_adapter_bench,
)
from lmcache.cli.commands.bench.l2_adapter_bench.result import BenchResult

_MB = 1024 * 1024
# Points at a path that is never created; validation must reject the
# arguments before any adapter touches the filesystem.
_ADAPTER_JSON = '{"type":"fs","base_path":"/nonexistent/bench-l2-validation"}'


def _parse(*argv: str, adapter_json: str = _ADAPTER_JSON) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_l2_arguments(parser)
    return parser.parse_args(["--l2-adapter", adapter_json, *argv])


def _fs_adapter_json(tmp_path: Path) -> str:
    return json.dumps({"type": "fs", "base_path": str(tmp_path / "l2")})


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_rounds_mode_is_the_default() -> None:
    args = _parse()

    assert args.duration_sec == 0.0
    assert args.warmup_sec == 0.0


def test_duration_sec_and_warmup_sec_parse() -> None:
    args = _parse("--duration-sec", "30", "--warmup-sec", "5")

    assert args.duration_sec == 30.0
    assert args.warmup_sec == 5.0


# ---------------------------------------------------------------------------
# Validation — all must fail before an adapter is constructed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--duration-sec", "-1"], id="negative-duration"),
        pytest.param(
            ["--duration-sec", "5", "--warmup-sec", "-1"], id="negative-warmup"
        ),
        pytest.param(
            ["--duration-sec", "5", "--only", "lookup"], id="sustained-lookup"
        ),
        pytest.param(
            ["--duration-sec", "5", "--no-skip-verify"], id="sustained-verify"
        ),
        pytest.param(
            ["--no-skip-verify", "--only", "load"], id="verify-with-only-load"
        ),
        pytest.param(
            ["--no-skip-verify", "--only", "store"], id="verify-with-only-store"
        ),
        pytest.param(["--warmup-sec", "5"], id="warmup-sec-without-duration"),
    ],
)
def test_invalid_combinations_exit_2(argv: list[str]) -> None:
    args = _parse(*argv)

    with pytest.raises(SystemExit) as exc:
        run_l2_adapter_bench(MagicMock(), args)

    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# End-to-end against the local fs adapter
# ---------------------------------------------------------------------------


def test_verify_runs_when_both_directions_are_present(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--no-skip-verify without --only must actually reach the gate.

    The complement of the rejection cases above: the gate is reachable,
    and its success line appears, so a passing run is distinguishable
    from one that silently skipped verification.
    """
    args = _parse(
        "--no-skip-verify",
        "--num-keys",
        "4",
        "--in-flight",
        "2",
        "--data-size-kb",
        "4",
        "--rounds",
        "1",
        "--warmup-rounds",
        "1",
        adapter_json=_fs_adapter_json(tmp_path),
    )

    run_l2_adapter_bench(MagicMock(), args)

    assert "All 8 keys data verified OK." in capsys.readouterr().out


def test_sustained_store_writes_a_distinct_file_per_submit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Sustained store keys must not wrap onto already-stored keys.

    ``fs_native`` short-circuits a store whose key already exists and
    reports success without writing, so a wrapped store window would
    measure the existence check while still counting payload bytes. The
    key space here (rounds=2 x in_flight=2 x num_keys=2 = 8 keys, i.e. 4
    submit slots) is far smaller than the number of submits a 0.3 s window
    completes, so a wrapping implementation would plateau at 8 files.
    """
    base = tmp_path / "l2"
    args = _parse(
        "--duration-sec",
        "0.3",
        "--only",
        "store",
        "--num-keys",
        "2",
        "--in-flight",
        "2",
        "--data-size-kb",
        "4",
        "--rounds",
        "2",
        adapter_json=json.dumps({"type": "fs", "base_path": str(base)}),
    )

    run_l2_adapter_bench(MagicMock(), args)

    out = capsys.readouterr().out
    # Summary line: "  [Store] <n> submits in <t>s (drain tail ...)"
    summary = next(line for line in out.splitlines() if "submits in" in line)
    submits = int(summary.split("submits in")[0].split()[-1])
    files = [p for p in base.rglob("*") if p.is_file()]

    assert submits > 4, "window too short to exercise wrap-around"
    # One file per key, every key distinct.
    assert len(files) == submits * 2


def test_sustained_mode_records_per_submit_latencies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sustained window must complete submits and report a window."""
    args = _parse(
        "--duration-sec",
        "0.3",
        "--only",
        "store",
        "--num-keys",
        "2",
        "--in-flight",
        "2",
        "--data-size-kb",
        "4",
        "--rounds",
        "8",
        adapter_json=_fs_adapter_json(tmp_path),
    )

    run_l2_adapter_bench(MagicMock(), args)

    out = capsys.readouterr().out
    assert "Sustained window for 0.3s" in out
    assert "submits in" in out


# ---------------------------------------------------------------------------
# Warmup stripping
# ---------------------------------------------------------------------------


def _rounds_result(rounds: int, in_flight: int = 2) -> BenchResult:
    result = BenchResult(
        operation="Load",
        in_flight=in_flight,
        num_keys=4,
        data_size_bytes=_MB,
    )
    for r in range(rounds):
        result.round_starts.append(float(r))
        result.round_durations.append(0.1)
        result.success_counts.append(in_flight * 4)
        for i in range(in_flight):
            # Encode round and submit so the survivors are identifiable.
            result.submit_latencies.append(r + i / 100.0)
        result.round_latency_counts.append(in_flight)
    return result


def test_strip_warmup_drops_matching_latencies() -> None:
    result = _rounds_result(rounds=5, in_flight=2)

    kept = _strip_warmup(result, warmup=2)

    assert len(kept.round_durations) == 3
    assert len(kept.round_starts) == 3
    assert len(kept.submit_latencies) == 6
    # First surviving latency belongs to round 2, submit 0.
    assert kept.submit_latencies[0] == pytest.approx(2.0)
    assert kept.round_latency_counts == [2, 2, 2]


def test_strip_warmup_handles_a_short_warmup_round() -> None:
    """A timed-out warmup round contributes fewer than in_flight entries."""
    result = _rounds_result(rounds=3, in_flight=4)
    # Simulate round 0 completing only 1 of 4 submits.
    result.submit_latencies = [0.0] + result.submit_latencies[4:]
    result.round_latency_counts[0] = 1

    kept = _strip_warmup(result, warmup=1)

    # 8 entries survive (rounds 1 and 2), and the single warmup entry is
    # dropped -- not four, which would have eaten real data.
    assert len(kept.submit_latencies) == 8
    assert kept.submit_latencies[0] == pytest.approx(1.0)


def test_strip_warmup_preserves_mode_and_timeout_flag() -> None:
    result = _rounds_result(rounds=2)
    result.timed_out = True

    kept = _strip_warmup(result, warmup=1)

    assert kept.mode is result.mode
    assert kept.timed_out is True
