# SPDX-License-Identifier: Apache-2.0
"""Aggregated benchmark statistics for L2 adapter operations."""

# Future
from __future__ import annotations

# Standard
from dataclasses import dataclass, field
from enum import Enum
import math
import statistics

_KB = 1024
_MB = 1024 * 1024


class BenchMode(Enum):
    """How the measured window was driven.

    ``ROUNDS``: fixed number of round-synchronised waves. Each round
    issues ``in_flight`` submits then drains all of them before the next
    round starts, so the worker pool idles at every round edge.

    ``SUSTAINED``: a sliding window of ``in_flight`` outstanding submits
    held for a fixed duration. One replacement submit is issued per
    completion, so there is no drain barrier.
    """

    ROUNDS = "rounds"
    SUSTAINED = "sustained"


def _percentile(values: list[float], pct: float) -> float:
    """Return the percentile *pct* (0..100) using nearest-rank.

    Returns 0.0 for an empty list.
    """
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    sorted_vals = sorted(values)
    # Nearest-rank method: rank = ceil(pct/100 * N)
    rank = max(1, int((pct / 100.0) * len(sorted_vals) + 0.999999))
    rank = min(rank, len(sorted_vals))
    return sorted_vals[rank - 1]


@dataclass
class BenchResult:
    """Aggregated benchmark statistics for one operation type.

    In :attr:`BenchMode.ROUNDS` each measured *round* issues
    ``in_flight`` submits, where each submit carries ``num_keys`` keys.
    ``round_durations[r]`` is the wall-clock elapsed for the whole round
    (from issuing the first submit to all submits of that round
    completing) and ``round_starts[r]`` is its ``perf_counter`` origin.

    In :attr:`BenchMode.SUSTAINED` there are no rounds:
    ``completed_submits`` and ``sustained_window_sec`` describe the
    measured window and the round lists stay empty.

    ``submit_latencies`` is populated in both modes: one entry per
    completed submit, in seconds. **It is an upper bound on the true
    service time** -- the clock stops when the single producer thread
    *observes* the completion during a harvest, not when the adapter's
    demux thread recorded it, and every completion harvested in the same
    wakeup shares one timestamp. Treat it as submit-to-observed-completion
    for a batch of ``num_keys`` keys, never as a per-key latency.
    """

    operation: str
    in_flight: int
    num_keys: int
    data_size_bytes: int
    mode: BenchMode = BenchMode.ROUNDS
    round_durations: list[float] = field(default_factory=list)
    round_starts: list[float] = field(default_factory=list)
    success_counts: list[int] = field(default_factory=list)
    # Per-submit observed latencies in seconds (both modes).
    submit_latencies: list[float] = field(default_factory=list)
    # ROUNDS mode: how many entries each round contributed to
    # ``submit_latencies``. Normally ``in_flight``, but fewer for a round
    # that timed out. Lets warmup rounds be stripped exactly.
    round_latency_counts: list[int] = field(default_factory=list)
    # Submits that completed, in BOTH modes. In ROUNDS mode this equals
    # ``sum(round_latency_counts)`` and is *not* used to derive
    # :attr:`total_keys` (rounds mode counts whole rounds); it exists so a
    # live observer has a monotonic progress counter in either mode.
    completed_submits: int = 0
    # Sustained-mode accounting (unused in ROUNDS mode).
    sustained_window_sec: float = 0.0
    # Ramp-down tail: time from the refill deadline until the last
    # outstanding submit completed. Concurrency decays across this
    # stretch, so a large value means the window understates throughput.
    sustained_drain_sec: float = 0.0
    # True when a submit never completed within the harvest timeout. In
    # ROUNDS mode a timed-out round also records an infinite duration.
    timed_out: bool = False
    # Lookup-specific metadata (left as defaults for store/load).
    expected_max_hit_rate: float = 0.0
    expected_hit_count: int = 0

    # ------------------------------------------------------------------
    # Derived counts
    # ------------------------------------------------------------------

    @property
    def keys_per_round(self) -> int:
        return self.in_flight * self.num_keys

    @property
    def total_keys(self) -> int:
        if self.mode is BenchMode.SUSTAINED:
            return self.completed_submits * self.num_keys
        return self.keys_per_round * len(self.round_durations)

    @property
    def total_data_bytes_per_round(self) -> int:
        return self.keys_per_round * self.data_size_bytes

    @property
    def total_data_bytes(self) -> int:
        """Bytes the run *requested*, successful or not.

        Counts every key submitted. A load that missed, or a store the
        backend short-circuited, still contributes here -- compare with
        :attr:`total_success_bytes` before quoting a throughput figure.
        """
        return self.total_keys * self.data_size_bytes

    @property
    def total_success(self) -> int:
        return sum(self.success_counts)

    @property
    def total_success_bytes(self) -> int:
        """Bytes belonging to keys the adapter reported successful."""
        return self.total_success * self.data_size_bytes

    # ------------------------------------------------------------------
    # Duration stats (seconds)
    # ------------------------------------------------------------------

    @property
    def avg_duration(self) -> float:
        return statistics.mean(self.round_durations) if self.round_durations else 0.0

    @property
    def min_duration(self) -> float:
        return min(self.round_durations) if self.round_durations else 0.0

    @property
    def max_duration(self) -> float:
        return max(self.round_durations) if self.round_durations else 0.0

    @property
    def std_duration(self) -> float:
        if len(self.round_durations) > 1:
            return statistics.stdev(self.round_durations)
        return 0.0

    @property
    def p50_duration(self) -> float:
        return _percentile(self.round_durations, 50.0)

    @property
    def p99_duration(self) -> float:
        return _percentile(self.round_durations, 99.0)

    # ------------------------------------------------------------------
    # Throughput stats (per-round, MB/s)
    # ------------------------------------------------------------------

    @property
    def per_round_throughput_mbps(self) -> list[float]:
        if self.data_size_bytes <= 0:
            return []
        bytes_per_round = self.total_data_bytes_per_round
        out: list[float] = []
        for d in self.round_durations:
            if d <= 0:
                out.append(float("inf"))
            else:
                out.append((bytes_per_round / _MB) / d)
        return out

    @property
    def avg_throughput_mbps(self) -> float:
        vals = self.per_round_throughput_mbps
        return statistics.mean(vals) if vals else 0.0

    @property
    def min_throughput_mbps(self) -> float:
        vals = self.per_round_throughput_mbps
        return min(vals) if vals else 0.0

    @property
    def max_throughput_mbps(self) -> float:
        vals = self.per_round_throughput_mbps
        return max(vals) if vals else 0.0

    # ------------------------------------------------------------------
    # Aggregate throughput (total payload / total measured time)
    # ------------------------------------------------------------------

    @property
    def measured_window_sec(self) -> float:
        """Total time inside the measured region, in seconds.

        In :attr:`BenchMode.SUSTAINED` this is the sliding-window
        duration. In :attr:`BenchMode.ROUNDS` it is the sum of the round
        durations, which **excludes** the inter-round producer-side work
        (buffer zeroing, key construction) that happens outside the timed
        region -- see :attr:`wall_clock_span_sec`.

        Returns 0.0 if any round timed out (an infinite duration makes
        the sum meaningless).
        """
        if self.mode is BenchMode.SUSTAINED:
            return self.sustained_window_sec
        total = math.fsum(self.round_durations)
        return total if math.isfinite(total) else 0.0

    @property
    def wall_clock_span_sec(self) -> float:
        """Wall-clock span of the measured region, in seconds.

        First round start to last round end, so inter-round producer work
        is included. Returns 0.0 in :attr:`BenchMode.SUSTAINED` (where the
        window *is* the wall clock, so use
        :attr:`measured_window_sec`), or when round starts were not
        recorded, or when a round timed out.
        """
        if self.mode is BenchMode.SUSTAINED:
            return 0.0
        if not self.round_starts or len(self.round_starts) != len(self.round_durations):
            return 0.0
        end = self.round_starts[-1] + self.round_durations[-1]
        span = end - self.round_starts[0]
        return span if math.isfinite(span) else 0.0

    @property
    def barrier_idle_fraction(self) -> float:
        """Fraction of the wall-clock span spent outside the timed rounds.

        Quantifies the round-edge cost of :attr:`BenchMode.ROUNDS`: the
        producer thread rebuilds keys and zeroes load buffers between
        rounds while the adapter's worker pool has nothing queued.
        Returns 0.0 when it cannot be computed.
        """
        span = self.wall_clock_span_sec
        if span <= 0:
            return 0.0
        measured = self.measured_window_sec
        if measured <= 0 or measured >= span:
            return 0.0
        return (span - measured) / span

    @property
    def aggregate_throughput_mbps(self) -> float:
        """Requested payload divided by total measured time, in MB/s.

        Differs from :attr:`avg_throughput_mbps`, which is the arithmetic
        mean of per-round rates and therefore over-weights fast rounds.

        Counts *requested* bytes, so it does not distinguish a real
        transfer from a load miss or a store the backend short-circuited.
        Quote :attr:`success_throughput_mbps` against a fio comparator.
        """
        if self.data_size_bytes <= 0:
            return 0.0
        window = self.measured_window_sec
        if window <= 0:
            return 0.0
        return (self.total_data_bytes / _MB) / window

    @property
    def success_throughput_mbps(self) -> float:
        """Successful payload divided by total measured time, in MB/s.

        The figure to compare against a fio comparator: it excludes keys
        the adapter did not report successful, so a mismatched
        prepopulation (all-miss loads) or a backend that no-ops repeat
        stores shows up as low throughput rather than as a fast run.
        """
        if self.data_size_bytes <= 0:
            return 0.0
        window = self.measured_window_sec
        if window <= 0:
            return 0.0
        return (self.total_success_bytes / _MB) / window

    @property
    def wall_clock_throughput_mbps(self) -> float:
        """Total payload divided by :attr:`wall_clock_span_sec`, in MB/s.

        The most conservative throughput figure in
        :attr:`BenchMode.ROUNDS`: it charges the run for the round-edge
        gaps. Returns 0.0 when the span is unavailable.
        """
        if self.data_size_bytes <= 0:
            return 0.0
        span = self.wall_clock_span_sec
        if span <= 0:
            return 0.0
        return (self.total_data_bytes / _MB) / span

    # ------------------------------------------------------------------
    # Per-submit latency distribution
    # ------------------------------------------------------------------

    @property
    def submit_count(self) -> int:
        return len(self.submit_latencies)

    @property
    def submit_latency_avg_ms(self) -> float:
        vals = self.submit_latencies
        return statistics.mean(vals) * 1000 if vals else 0.0

    @property
    def submit_latency_min_ms(self) -> float:
        vals = self.submit_latencies
        return min(vals) * 1000 if vals else 0.0

    @property
    def submit_latency_max_ms(self) -> float:
        vals = self.submit_latencies
        return max(vals) * 1000 if vals else 0.0

    @property
    def submit_latency_p50_ms(self) -> float:
        return _percentile(self.submit_latencies, 50.0) * 1000

    @property
    def submit_latency_p90_ms(self) -> float:
        return _percentile(self.submit_latencies, 90.0) * 1000

    @property
    def submit_latency_p99_ms(self) -> float:
        return _percentile(self.submit_latencies, 99.0) * 1000

    # ------------------------------------------------------------------
    # Ops/sec (key-rate) stats — useful for lookup which has no payload
    # ------------------------------------------------------------------

    @property
    def per_round_ops_per_sec(self) -> list[float]:
        out: list[float] = []
        for d in self.round_durations:
            if d <= 0:
                out.append(float("inf"))
            else:
                out.append(self.keys_per_round / d)
        return out

    @property
    def avg_ops_per_sec(self) -> float:
        vals = self.per_round_ops_per_sec
        return statistics.mean(vals) if vals else 0.0

    @property
    def aggregate_ops_per_sec(self) -> float:
        """Total keys divided by :attr:`measured_window_sec`.

        Defined in both modes, unlike :attr:`avg_ops_per_sec` which is a
        mean of per-round rates and is 0.0 in
        :attr:`BenchMode.SUSTAINED`.
        """
        window = self.measured_window_sec
        if window <= 0:
            return 0.0
        return self.total_keys / window

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    @property
    def avg_success_per_round(self) -> float:
        return statistics.mean(self.success_counts) if self.success_counts else 0.0

    @property
    def avg_latency_per_key_ms(self) -> float:
        """Round duration divided by keys per round, in milliseconds.

        **Not a latency.** With ``in_flight`` concurrent submits per round
        and ``num_keys`` keys per submit, this is round makespan spread
        evenly over the keys -- an arithmetic artifact of the round
        barrier that shrinks as concurrency grows. Use
        :attr:`submit_latency_p50_ms` / :attr:`submit_latency_p99_ms` for
        a real distribution. Retained for output continuity.

        Returns 0.0 in :attr:`BenchMode.SUSTAINED`, which has no rounds.
        """
        if self.keys_per_round <= 0:
            return 0.0
        return (self.avg_duration / self.keys_per_round) * 1000

    @property
    def actual_hit_rate(self) -> float:
        if self.total_keys <= 0:
            return 0.0
        return self.total_success / self.total_keys
