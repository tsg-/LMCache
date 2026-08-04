# SPDX-License-Identifier: Apache-2.0
"""Prometheus endpoint for a running ``bench l2`` benchmark.

Exists to correlate a benchmark run with host-side counters (RDMA NIC,
NVMe SMART, per-NUMA CPU) that are already scraped from node_exporter.
Without it, lining a run up against those panels is manual: bracket the
run and diff the counters by hand.

**Scrape-driven, not sampled.** The runners mutate their
:class:`BenchResult` in place as each submit completes -- see
``run_sustained_window``, which appends to ``submit_latencies`` and
``success_counts`` and increments ``completed_submits`` inside its
harvest loop. So the collector here computes everything at scrape time
from the live result. There is no background sampler thread and nothing
is added to the submit path.

**What this is not.** A 1-15 s scrape interval is far too coarse to
attribute host CPU to a specific phase of a run; that needs in-process
bracketing around the measured window. Treat these series as a time
axis to align external counters against, not as the measurement itself.
The authoritative per-run figures remain the end-of-run summary table.

Counters are cumulative and labelled by ``operation``, so each series is
monotonic within its own phase and ``rate()`` behaves. Use a window of
at least 60 s: the irdma driver refreshes ``hw_counters`` asynchronously
(roughly 1 s), and shorter rate windows alias badly against it.
"""

# Future
from __future__ import annotations

# Standard
from typing import TYPE_CHECKING, Iterable
import threading

if TYPE_CHECKING:
    # Third Party
    from prometheus_client.core import Metric

    # First Party
    from lmcache.cli.commands.bench.l2_adapter_bench.result import BenchResult

_NAMESPACE = "lmcache_bench_l2"


class MetricsServerError(RuntimeError):
    """The metrics endpoint could not be started."""


class BenchMetricsState:
    """Live benchmark results, keyed by phase, for the collector to read.

    The benchmark registers each phase's :class:`BenchResult` here before
    running it. Results are mutated in place by the runners, so the
    collector sees current values without any copying or notification.

    Thread safety: the benchmark's producer thread registers results and
    the HTTP server's thread reads them, so the registry dict is guarded.
    The :class:`BenchResult` objects themselves are read without a lock --
    see :meth:`snapshot` for why that is acceptable here.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._results: dict[str, "BenchResult"] = {}

    def register(self, operation: str, result: "BenchResult") -> None:
        """Publish *result* under the label ``operation``.

        Args:
            operation: Phase name used as the ``operation`` label, e.g.
                ``"Store"``. Re-registering a phase replaces it.
            result: Result object the runner will mutate in place.
        """
        with self._lock:
            self._results[operation] = result

    def snapshot(self) -> list[tuple[str, "BenchResult"]]:
        """Return the registered ``(operation, result)`` pairs.

        Only the dict is locked. The results themselves are read
        unsynchronised while a runner appends to them, which is safe for
        this purpose: CPython list appends and int increments are atomic
        under the GIL, so a scrape sees a valid-but-possibly-stale value
        rather than a torn one. The alternative -- locking the runner's
        harvest loop -- would put scrape contention on the measured path,
        which is exactly what must not happen.
        """
        with self._lock:
            return list(self._results.items())


class _BenchCollector:
    """Prometheus collector that renders :class:`BenchMetricsState`."""

    def __init__(self, state: BenchMetricsState) -> None:
        self._state = state

    def collect(self) -> Iterable["Metric"]:
        """Yield the current metric families, computed at scrape time."""
        # Third Party
        from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

        submits = CounterMetricFamily(
            f"{_NAMESPACE}_completed_submits",
            "Submits completed so far in this phase.",
            labels=["operation"],
        )
        success_keys = CounterMetricFamily(
            f"{_NAMESPACE}_success_keys",
            "Keys the adapter reported successful so far in this phase.",
            labels=["operation"],
        )
        success_bytes = CounterMetricFamily(
            f"{_NAMESPACE}_success_bytes",
            (
                "Payload bytes belonging to successful keys so far. Note a "
                "backend that short-circuits an existing key reports success "
                "without writing, so this counts requested payload, not "
                "necessarily bytes that reached the media."
            ),
            labels=["operation"],
        )
        latency = GaugeMetricFamily(
            f"{_NAMESPACE}_submit_latency_seconds",
            (
                "Observed per-submit latency percentile over the whole phase "
                "so far. Not a sliding window: percentiles are cumulative and "
                "flatten as the run proceeds."
            ),
            labels=["operation", "quantile"],
        )
        in_flight = GaugeMetricFamily(
            f"{_NAMESPACE}_in_flight_target",
            "Configured outstanding submits held by this phase.",
            labels=["operation"],
        )

        for operation, result in self._state.snapshot():
            labels = [operation]
            submits.add_metric(labels, float(result.completed_submits))
            success_keys.add_metric(labels, float(result.total_success))
            success_bytes.add_metric(labels, float(result.total_success_bytes))
            in_flight.add_metric(labels, float(result.in_flight))
            for quantile, value_ms in (
                ("0.5", result.submit_latency_p50_ms),
                ("0.9", result.submit_latency_p90_ms),
                ("0.99", result.submit_latency_p99_ms),
            ):
                latency.add_metric([operation, quantile], value_ms / 1000.0)

        yield submits
        yield success_keys
        yield success_bytes
        yield latency
        yield in_flight


def start_metrics_server(port: int, state: BenchMetricsState) -> None:
    """Serve *state* over HTTP for Prometheus on *port*.

    Uses a dedicated registry rather than the process-global one, so the
    benchmark's series are the only thing exposed and nothing a
    transitively imported module registered leaks into the scrape.

    The server runs on a daemon thread; there is no shutdown call. A
    benchmark is a one-shot process and the endpoint must stay scrapable
    through the end-of-run summary, so it dies with the process.

    Args:
        port: TCP port to bind. Binds all interfaces, matching
            ``prometheus_client`` defaults -- this is a benchmark rig
            tool, so do not expose it on an untrusted network.
        state: Registry the collector reads on each scrape.

    Raises:
        MetricsServerError: ``prometheus_client`` is unavailable, or the
            port could not be bound.
    """
    try:
        # Third Party
        from prometheus_client import CollectorRegistry, start_http_server
    except ImportError as e:
        raise MetricsServerError(
            f"--serve-metrics needs prometheus_client, which is not "
            f"importable ({e}). Install the CLI extras "
            f"(requirements/cli.txt)."
        ) from e

    registry = CollectorRegistry()
    registry.register(_BenchCollector(state))
    try:
        start_http_server(port, registry=registry)
    except OSError as e:
        raise MetricsServerError(
            f"could not bind the metrics endpoint on port {port}: {e}. "
            f"Pick a free port with --serve-metrics, or stop whatever "
            f"holds it."
        ) from e
