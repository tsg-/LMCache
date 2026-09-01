# SPDX-License-Identifier: Apache-2.0
"""Prometheus endpoint for a running ``bench l2`` benchmark.

Exists to correlate a benchmark run with host-side counters (RDMA NIC,
NVMe SMART, per-NUMA CPU) that are already scraped from node_exporter.
Without it, lining a run up against those panels is manual: bracket the
run and diff the counters by hand.

**Scrape-driven, not sampled.** The runners mutate their
:class:`BenchResult` in place as each submit completes -- see
``run_sustained_window``, which calls ``record_latency`` and
``record_success`` and increments ``completed_submits`` inside its
harvest loop. So the collector here reads everything at scrape time from
the live result. There is no background sampler thread and nothing is
added to the submit path.

**Every series is O(1) per scrape.** This is a hard constraint, not an
optimisation. The HTTP server shares the interpreter with the single
benchmark producer thread and holds the GIL while it collects, so any
per-scrape work that grows with the run length eventually stalls the
thread being measured -- and it stalls it *worst* exactly during the
long saturation runs the endpoint exists to observe. Measured before
the fix: one scrape took ~0.5 s against a one-million-sample latency
history. So the collector may only read counters and running totals
(``completed_submits``, ``success_total``, ``submit_latency_total_sec``);
it must never sort, sum, copy, or otherwise walk ``submit_latencies`` or
``success_counts``.

**Hence no live percentiles.** p50/p90/p99 cannot be had from a
cumulative history without scanning it, and the bounded alternatives
are all worse than nothing here: a reservoir or a sliding window adds
per-completion work to the harvest loop, and histogram buckets need a
latency range nobody has calibrated for this rig yet. What is exposed
instead is ``_submit_latency_seconds_total``, which with
``_completed_submits_total`` gives a mean -- and, under ``rate()``, a
windowed mean. For a real distribution use the end-of-run summary
table, which computes percentiles once, off the hot path.

**What this is not.** A 1-15 s scrape interval is far too coarse to
attribute host CPU to a specific phase of a run; that needs in-process
bracketing around the measured window. Treat these series as a time
axis to align external counters against, not as the measurement itself.
The authoritative per-run figures remain the end-of-run summary table.

Counters are cumulative and labelled by ``operation``, ``phase``, and
``model``, so each series is monotonic within its own phase and model and
``rate()`` behaves.
Warmup is exposed under ``phase="warmup"`` rather than hidden: the
external counters this endpoint exists to align against *do* include
warmup I/O, so omitting it would leave an unexplained gap in the NIC and
NVMe series. It is a separate series precisely so it can never be
mistaken for, or summed into, the measured figures. Use a window of at
least 60 s: RDMA drivers refresh ``hw_counters`` asynchronously (order
1 s), and shorter rate windows alias badly against that refresh.

**Thread-safety model.** The unsynchronised read in
:meth:`BenchMetricsState.snapshot` relies on CPython's GIL making list
appends and int increments atomic. This is a CPython-specific guarantee,
not general thread safety: on a free-threaded build it would need real
locking. Fields are read independently, so a scrape can straddle an
instant -- ``completed_submits`` and ``submit_latency_total_sec`` may
reflect times a few microseconds apart, which shows up as a slightly
off mean for one scrape. Each individual counter is still
nondecreasing, which is all ``rate()`` requires.
"""

# Future
from __future__ import annotations

# Standard
from typing import TYPE_CHECKING, Callable, Iterable
import threading

if TYPE_CHECKING:
    # Third Party
    from prometheus_client.core import Metric

    # First Party
    from lmcache.cli.commands.bench.l2_adapter_bench.result import BenchResult

_NAMESPACE = "lmcache_bench_l2"


class MetricsServerError(RuntimeError):
    """The metrics endpoint could not be started."""


PHASE_MEASURED = "measured"
PHASE_WARMUP = "warmup"
# Rounds mode drives warmup and measured rounds through ONE result, so a
# live observer cannot separate them -- the split only happens afterwards,
# when the summary is computed. Labelling that series honestly is better
# than either claiming it is measured-only or hiding it.
PHASE_WARMUP_AND_MEASURED = "warmup+measured"


class BenchMetricsState:
    """Live benchmark results for the collector to read.

    The benchmark registers each result here before running it. Results
    are mutated in place by the runners, so the collector sees current
    values without any copying or notification.

    Keyed by ``(operation, phase)``. Warmup and measured results are
    therefore distinct series, which is what keeps each one monotonic: a
    single series carrying warmup and then measured values would show a
    counter reset that ``rate()`` would misread as a restart.

    Thread safety: the benchmark's producer thread registers results and
    the HTTP server's thread reads them, so the registry dict is guarded.
    The :class:`BenchResult` objects themselves are read without a lock --
    see :meth:`snapshot` for why that is acceptable here.

    Args:
        model_name: Profile identity for the whole process's lifetime
            (e.g. a ``--kvcache-shape-profile`` file's stem), rendered as
            the ``model`` label on every series. Fixed at construction,
            not per-``register`` call: one process benchmarks one model,
            and a label that changed mid-process would fragment a single
            run's series the same way switching phase does on purpose.
    """

    def __init__(self, model_name: str = "") -> None:
        self._lock = threading.Lock()
        self._results: dict[tuple[str, str], "BenchResult"] = {}
        self.model_name = model_name

    def register(
        self, operation: str, result: "BenchResult", phase: str = PHASE_MEASURED
    ) -> None:
        """Publish *result* under ``(operation, phase)``.

        Args:
            operation: Direction under test, used as the ``operation``
                label, e.g. ``"Store"``.
            result: Result object the runner will mutate in place.
            phase: :data:`PHASE_MEASURED` or :data:`PHASE_WARMUP`.
                Re-registering the same pair replaces it.
        """
        with self._lock:
            self._results[(operation, phase)] = result

    def snapshot(self) -> list[tuple[tuple[str, str], "BenchResult"]]:
        """Return the registered ``((operation, phase), result)`` pairs.

        Only the dict is locked. The results themselves are read
        unsynchronised while a runner appends to them. This is safe under
        CPython specifically: list appends and int increments are atomic
        under the GIL, so a scrape sees a valid-but-possibly-stale value
        rather than a torn one. It is not general thread safety -- a
        free-threaded build would need real locking. The alternative --
        locking the runner's harvest loop -- would put scrape contention
        on the measured path, which is exactly what must not happen.
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
            labels=["operation", "phase", "model"],
        )
        success_keys = CounterMetricFamily(
            f"{_NAMESPACE}_success_keys",
            "Keys the adapter reported successful so far in this phase.",
            labels=["operation", "phase", "model"],
        )
        success_bytes = CounterMetricFamily(
            f"{_NAMESPACE}_success_bytes",
            (
                "Payload bytes belonging to successful keys so far. Note a "
                "backend that short-circuits an existing key reports success "
                "without writing, so this counts requested payload, not "
                "necessarily bytes that reached the media."
            ),
            labels=["operation", "phase", "model"],
        )
        # Rendered as ``..._submit_latency_seconds_total``:
        # ``CounterMetricFamily`` appends the suffix itself.
        latency_sum = CounterMetricFamily(
            f"{_NAMESPACE}_submit_latency_seconds",
            (
                "Cumulative observed submit-to-completion time. Divide by "
                "completed_submits, or take rate() of both, for a mean "
                "submit latency. There is no percentile here on purpose -- "
                "see the module docstring."
            ),
            labels=["operation", "phase", "model"],
        )
        in_flight = GaugeMetricFamily(
            f"{_NAMESPACE}_in_flight_target",
            "Configured outstanding submits held by this phase.",
            labels=["operation", "phase", "model"],
        )
        phase_start = GaugeMetricFamily(
            f"{_NAMESPACE}_phase_start_time_seconds",
            (
                "Unix time this phase's sustained window opened (first "
                "submit). 0 for a phase that has not opened one -- rounds "
                "mode and mixed mode do not set this yet."
            ),
            labels=["operation", "phase", "model"],
        )
        phase_end = GaugeMetricFamily(
            f"{_NAMESPACE}_phase_end_time_seconds",
            (
                "Unix time this phase's sustained window closed. 0 while "
                "the window is still open -- this is set once, the "
                "instant it actually closes, never predicted from the "
                "configured duration."
            ),
            labels=["operation", "phase", "model"],
        )

        for (operation, phase), result in self._state.snapshot():
            labels = [operation, phase, self._state.model_name]
            submits.add_metric(labels, float(result.completed_submits))
            success_keys.add_metric(labels, float(result.total_success))
            success_bytes.add_metric(labels, float(result.total_success_bytes))
            in_flight.add_metric(labels, float(result.in_flight))
            latency_sum.add_metric(labels, result.submit_latency_total_sec)
            phase_start.add_metric(labels, result.phase_started_at)
            phase_end.add_metric(labels, result.phase_ended_at)

        yield submits
        yield success_keys
        yield success_bytes
        yield latency_sum
        yield in_flight
        yield phase_start
        yield phase_end


def start_metrics_server(
    port: int, state: BenchMetricsState, address: str = "127.0.0.1"
) -> Callable[[], None]:
    """Serve *state* over HTTP for Prometheus on *port*.

    Uses a dedicated registry rather than the process-global one, so the
    benchmark's series are the only thing exposed and nothing a
    transitively imported module registered leaks into the scrape.

    The server runs on a daemon thread, so a one-shot CLI process does
    not need to stop it. In-process callers do: without the returned
    shutdown, each call leaks a listening socket for the life of the
    interpreter, which matters for tests.

    Args:
        port: TCP port to bind.
        state: Registry the collector reads on each scrape.
        address: Interface to bind. Defaults to loopback rather than
            ``prometheus_client``'s all-interfaces default: the endpoint
            is unauthenticated, so reaching it off-box must be an
            explicit choice. Pass ``"0.0.0.0"`` when Prometheus scrapes
            the rig remotely.

    Returns:
        A callable that closes the listening socket and joins the server
        thread. Idempotent, so calling it from a ``finally`` block that
        may run twice is safe.

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
        server, thread = start_http_server(port, addr=address, registry=registry)
    except OSError as e:
        raise MetricsServerError(
            f"could not bind the metrics endpoint on {address}:{port}: "
            f"{e}. Pick a free port with --serve-metrics, or stop "
            f"whatever holds it."
        ) from e

    stopped = threading.Event()

    def shutdown() -> None:
        if stopped.is_set():
            return
        stopped.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    return shutdown
