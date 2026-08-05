# SPDX-License-Identifier: Apache-2.0
"""Tests for ``BenchResult`` derived statistics."""

# Third Party
import pytest

# First Party
from lmcache.cli.commands.bench.l2_adapter_bench.result import (
    BenchMode,
    BenchResult,
    _percentile,
)

_MB = 1024 * 1024


def _rounds_result(
    durations: list[float],
    starts: list[float] | None = None,
    data_size_bytes: int = _MB,
    in_flight: int = 2,
    num_keys: int = 4,
) -> BenchResult:
    return BenchResult(
        operation="Load",
        in_flight=in_flight,
        num_keys=num_keys,
        data_size_bytes=data_size_bytes,
        round_durations=list(durations),
        round_starts=list(starts) if starts is not None else [],
        success_counts=[in_flight * num_keys] * len(durations),
    )


# ---------------------------------------------------------------------------
# Percentiles
# ---------------------------------------------------------------------------


def test_percentile_empty_and_singleton() -> None:
    assert _percentile([], 50.0) == 0.0
    assert _percentile([7.5], 99.0) == 7.5


def test_percentile_nearest_rank() -> None:
    vals = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]

    assert _percentile(vals, 50.0) == 5.0
    assert _percentile(vals, 90.0) == 9.0
    assert _percentile(vals, 99.0) == 10.0


# ---------------------------------------------------------------------------
# Measured window / wall clock
# ---------------------------------------------------------------------------


def test_measured_window_sums_round_durations() -> None:
    result = _rounds_result([0.1, 0.2, 0.3])

    assert result.measured_window_sec == pytest.approx(0.6)


def test_measured_window_is_zero_when_a_round_timed_out() -> None:
    result = _rounds_result([0.1, float("inf")])

    assert result.measured_window_sec == 0.0
    assert result.aggregate_throughput_mbps == 0.0


def test_wall_clock_span_includes_inter_round_gaps() -> None:
    # Two 0.1 s rounds starting 0.5 s apart: 0.2 s measured, 0.6 s span.
    result = _rounds_result([0.1, 0.1], starts=[10.0, 10.5])

    assert result.measured_window_sec == pytest.approx(0.2)
    assert result.wall_clock_span_sec == pytest.approx(0.6)
    assert result.barrier_idle_fraction == pytest.approx(0.4 / 0.6)


def test_wall_clock_span_zero_without_round_starts() -> None:
    result = _rounds_result([0.1, 0.1])

    assert result.wall_clock_span_sec == 0.0
    assert result.wall_clock_throughput_mbps == 0.0
    assert result.barrier_idle_fraction == 0.0


def test_wall_clock_span_zero_on_length_mismatch() -> None:
    result = _rounds_result([0.1, 0.1], starts=[10.0])

    assert result.wall_clock_span_sec == 0.0


def test_wall_clock_span_zero_when_an_earlier_round_timed_out() -> None:
    # The timeout is in the FIRST round, so the last start plus the last
    # duration is still finite and the span alone looks jointly plausible
    # (10.5 + 1.0 - 10.0 = 1.5 s). Publishing a throughput here would report a
    # number for a run that never completed.
    result = _rounds_result([float("inf"), 1.0], starts=[10.0, 10.5])

    assert result.wall_clock_span_sec == 0.0
    assert result.wall_clock_throughput_mbps == 0.0
    assert result.barrier_idle_fraction == 0.0


def test_wall_clock_span_zero_when_timed_out_flag_set() -> None:
    # A run can be flagged timed_out while every recorded duration is finite
    # (the timeout aborts before the offending round's duration is stored), so
    # the finite-duration check alone would not catch it.
    result = _rounds_result([0.1, 0.1], starts=[10.0, 10.5])
    result.timed_out = True

    assert result.wall_clock_span_sec == 0.0
    assert result.wall_clock_throughput_mbps == 0.0


# ---------------------------------------------------------------------------
# Aggregate vs per-round-mean throughput
# ---------------------------------------------------------------------------


def test_aggregate_throughput_is_total_payload_over_total_time() -> None:
    # 8 keys/round * 1 MiB = 8 MiB/round, 2 rounds, 4 s total.
    result = _rounds_result([1.0, 3.0])

    assert result.total_data_bytes == 16 * _MB
    assert result.aggregate_throughput_mbps == pytest.approx(4.0)


def test_mean_of_rates_exceeds_aggregate_when_rounds_are_uneven() -> None:
    """The existing avg metric over-weights fast rounds.

    8 MiB in 1 s and 8 MiB in 3 s is 4 MB/s aggregate, but the mean of
    the two rates (8 and 2.67) is 5.33 MB/s. Documenting the divergence
    is the point of adding the aggregate figure.
    """
    result = _rounds_result([1.0, 3.0])

    assert result.avg_throughput_mbps > result.aggregate_throughput_mbps
    assert result.avg_throughput_mbps == pytest.approx(8.0 / 2 + (8.0 / 3) / 2)


def test_wall_clock_throughput_is_the_most_conservative() -> None:
    result = _rounds_result([0.1, 0.1], starts=[10.0, 10.5])

    assert (
        result.wall_clock_throughput_mbps
        < result.aggregate_throughput_mbps
        <= result.avg_throughput_mbps
    )


def test_throughput_is_zero_without_payload() -> None:
    result = _rounds_result([1.0], data_size_bytes=0)

    assert result.aggregate_throughput_mbps == 0.0
    assert result.wall_clock_throughput_mbps == 0.0


def test_aggregate_ops_per_sec_uses_measured_window() -> None:
    result = _rounds_result([1.0, 3.0])

    # 8 keys/round * 2 rounds = 16 keys over 4 s.
    assert result.aggregate_ops_per_sec == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Per-submit latency distribution
# ---------------------------------------------------------------------------


def test_submit_latency_percentiles_are_per_submit_not_per_round() -> None:
    """One straggler must move p99 without moving p50.

    A round-level percentile cannot express this: with in_flight submits
    collapsed into one round duration, a single slow submit and a
    uniformly slow round are indistinguishable.
    """
    result = _rounds_result([1.0])
    result.submit_latencies = [0.010] * 99 + [1.000]

    assert result.submit_count == 100
    assert result.submit_latency_p50_ms == pytest.approx(10.0)
    assert result.submit_latency_p99_ms == pytest.approx(10.0)
    assert result.submit_latency_max_ms == pytest.approx(1000.0)
    assert result.submit_latency_min_ms == pytest.approx(10.0)


def test_submit_latency_stats_are_zero_when_unrecorded() -> None:
    result = _rounds_result([1.0])

    assert result.submit_count == 0
    assert result.submit_latency_avg_ms == 0.0
    assert result.submit_latency_p50_ms == 0.0
    assert result.submit_latency_p99_ms == 0.0


def test_latency_per_key_is_documented_as_an_artifact() -> None:
    """``avg_latency_per_key_ms`` shrinks as concurrency grows.

    Same per-submit service time, 4x the in-flight submits, and the
    "latency" drops 4x -- which is why it is not a latency.
    """
    low = _rounds_result([1.0], in_flight=1, num_keys=4)
    high = _rounds_result([1.0], in_flight=4, num_keys=4)

    assert high.avg_latency_per_key_ms == pytest.approx(low.avg_latency_per_key_ms / 4)


# ---------------------------------------------------------------------------
# Sustained mode
# ---------------------------------------------------------------------------


def test_sustained_totals_come_from_completed_submits() -> None:
    result = BenchResult(
        operation="Load",
        in_flight=4,
        num_keys=8,
        data_size_bytes=_MB,
        mode=BenchMode.SUSTAINED,
        completed_submits=10,
        sustained_window_sec=2.0,
        success_counts=[8] * 10,
    )

    assert result.total_keys == 80
    assert result.total_data_bytes == 80 * _MB
    assert result.measured_window_sec == pytest.approx(2.0)
    assert result.aggregate_throughput_mbps == pytest.approx(40.0)
    assert result.aggregate_ops_per_sec == pytest.approx(40.0)


def test_sustained_timeout_suppresses_partial_window_throughput() -> None:
    """A pre-stall interval must not publish a rate for a failed window."""
    result = BenchResult(
        operation="Load",
        in_flight=2,
        num_keys=4,
        data_size_bytes=_MB,
        mode=BenchMode.SUSTAINED,
        success_counts=[4],
        completed_submits=1,
        sustained_window_sec=0.01,
        timed_out=True,
    )

    assert result.measured_window_sec == 0.0
    assert result.aggregate_throughput_mbps == 0.0
    assert result.success_throughput_mbps == 0.0


def test_success_throughput_excludes_missed_keys() -> None:
    """An all-miss load must not report the requested rate as throughput.

    10 submits x 8 keys requested, zero successful: the aggregate figure
    still shows the request rate, so the success figure is the one that
    exposes a mismatched prepopulation.
    """
    result = BenchResult(
        operation="Load",
        in_flight=4,
        num_keys=8,
        data_size_bytes=_MB,
        mode=BenchMode.SUSTAINED,
        completed_submits=10,
        sustained_window_sec=2.0,
        success_counts=[0] * 10,
    )

    assert result.total_data_bytes == 80 * _MB
    assert result.total_success_bytes == 0
    assert result.aggregate_throughput_mbps == pytest.approx(40.0)
    assert result.success_throughput_mbps == 0.0


def test_success_throughput_matches_aggregate_when_all_succeed() -> None:
    result = BenchResult(
        operation="Store",
        in_flight=4,
        num_keys=8,
        data_size_bytes=_MB,
        mode=BenchMode.SUSTAINED,
        completed_submits=10,
        sustained_window_sec=2.0,
        success_counts=[8] * 10,
    )

    assert result.success_throughput_mbps == pytest.approx(
        result.aggregate_throughput_mbps
    )


def test_success_throughput_is_partial_on_partial_success() -> None:
    # Half the keys of each submit succeeded.
    result = BenchResult(
        operation="Load",
        in_flight=4,
        num_keys=8,
        data_size_bytes=_MB,
        mode=BenchMode.SUSTAINED,
        completed_submits=10,
        sustained_window_sec=2.0,
        success_counts=[4] * 10,
    )

    assert result.success_throughput_mbps == pytest.approx(
        result.aggregate_throughput_mbps / 2
    )


def test_sustained_has_no_round_derived_stats() -> None:
    result = BenchResult(
        operation="Store",
        in_flight=4,
        num_keys=8,
        data_size_bytes=_MB,
        mode=BenchMode.SUSTAINED,
        completed_submits=10,
        sustained_window_sec=2.0,
    )

    assert result.wall_clock_span_sec == 0.0
    assert result.barrier_idle_fraction == 0.0
    assert result.avg_throughput_mbps == 0.0
    assert result.avg_latency_per_key_ms == 0.0
    assert result.p50_duration == 0.0
