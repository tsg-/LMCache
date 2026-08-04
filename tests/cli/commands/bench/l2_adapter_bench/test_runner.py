# SPDX-License-Identifier: Apache-2.0
"""Tests for the sustained-window driver and per-submit latency capture.

The fake adapter below completes submits on a background thread after a
configurable service time, signalling a real eventfd/pipe so the driver
exercises its actual wait path rather than a stubbed one.
"""

# Standard
from typing import Any
import threading
import time

# Third Party
import pytest

# First Party
from lmcache.cli.commands.bench.l2_adapter_bench.result import (
    BenchMode,
    BenchResult,
)
from lmcache.cli.commands.bench.l2_adapter_bench.runner import (
    WarmupNotDrainedError,
    _record_round_latencies,
    _require_drained_warmup,
    run_sustained_window,
)
from lmcache.v1.platform import create_event_notifier

_MB = 1024 * 1024


class _FakeAdapter:
    """Completes each submit after ``service_sec`` on a worker thread.

    Only ``concurrency`` submits are serviced at a time, so the fake has
    a genuine throughput ceiling the driver has to queue behind.
    """

    def __init__(
        self,
        service_sec: float,
        concurrency: int = 64,
        stagger_sec: float = 0.0,
    ) -> None:
        self._service_sec = service_sec
        # Adds ``stagger_sec * slot_position`` to each wave's service time
        # so completions land in distinct harvests. Without it, all
        # in-flight submits can complete inside one wakeup, which makes
        # timing-sensitive assertions racy.
        self._stagger_sec = stagger_sec
        self._sem = threading.Semaphore(concurrency)
        self._notifier = create_event_notifier()
        self._lock = threading.Lock()
        self._done: dict[int, int] = {}
        self._next_id = 0
        self._threads: list[threading.Thread] = []
        self.submitted = 0
        self.max_outstanding = 0
        self._outstanding = 0

    @property
    def event_fd(self) -> int:
        return self._notifier.fileno()

    def submit(self, num_keys: int) -> int:
        with self._lock:
            task_id = self._next_id
            self._next_id += 1
            self.submitted += 1
            self._outstanding += 1
            self.max_outstanding = max(self.max_outstanding, self._outstanding)
        thread = threading.Thread(
            target=self._service, args=(task_id, num_keys), daemon=True
        )
        self._threads.append(thread)
        thread.start()
        return task_id

    def _service(self, task_id: int, num_keys: int) -> None:
        with self._sem:
            time.sleep(self._service_sec + task_id * self._stagger_sec)
        with self._lock:
            self._done[task_id] = num_keys
            self._outstanding -= 1
        self._notifier.notify()

    def harvest(self, pending: set[int]) -> dict[int, int]:
        with self._lock:
            ready = [tid for tid in self._done if tid in pending]
            out = {tid: self._done.pop(tid) for tid in ready}
        return out

    def join(self) -> None:
        for thread in self._threads:
            thread.join(timeout=5.0)
        self._notifier.close()


def _sustained(
    in_flight: int, num_keys: int = 4, data_size_bytes: int = _MB
) -> BenchResult:
    return BenchResult(
        operation="Load",
        in_flight=in_flight,
        num_keys=num_keys,
        data_size_bytes=data_size_bytes,
        mode=BenchMode.SUSTAINED,
    )


def _run(
    adapter: _FakeAdapter,
    result: BenchResult,
    duration_sec: float,
    submitted_slots: list[int] | None = None,
) -> tuple[int, int]:
    def submit(_submit_index: int, slot: int) -> int:
        if submitted_slots is not None:
            submitted_slots.append(slot)
        return adapter.submit(result.num_keys)

    return run_sustained_window(
        result,
        submit=submit,
        harvest=adapter.harvest,
        event_fd=adapter.event_fd,
        duration_sec=duration_sec,
        timeout=10.0,
        success_for=lambda payload: int(payload),
        log=lambda _msg: None,
    )


# ---------------------------------------------------------------------------
# Rounds-mode per-submit latency attribution
# ---------------------------------------------------------------------------


def test_round_latency_charges_each_submit_from_its_own_origin() -> None:
    """Each submit's latency must start when *that* submit was issued.

    A single round-wide origin taken after the submit loop would hide the
    loop's own span from the early submits. Here submit 0 was issued 1.0 s
    before submit 1 and both completed at t=3.0, so their latencies must
    differ by that 1.0 s rather than both reading 1.0 s.
    """
    result = _sustained(in_flight=2)
    submitted_at = {10: 1.0, 11: 2.0}
    observed_at = {10: 3.0, 11: 3.0}

    appended = _record_round_latencies(result, [10, 11], observed_at, submitted_at)

    assert appended == 2
    assert result.submit_latencies == [2.0, 1.0]


def test_round_latency_skips_tasks_that_never_completed() -> None:
    result = _sustained(in_flight=3)
    submitted_at = {10: 1.0, 11: 1.0, 12: 1.0}
    observed_at = {10: 2.0}  # 11 and 12 timed out

    appended = _record_round_latencies(result, [10, 11, 12], observed_at, submitted_at)

    assert appended == 1
    assert result.submit_latencies == [1.0]


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def test_rejects_non_positive_duration() -> None:
    result = _sustained(in_flight=2)

    with pytest.raises(ValueError, match="duration_sec must be positive"):
        run_sustained_window(
            result,
            submit=lambda _i, _s: 0,
            harvest=lambda _p: {},
            event_fd=0,
            duration_sec=0.0,
            timeout=1.0,
            success_for=lambda _p: 0,
            log=lambda _m: None,
        )


def test_rejects_rounds_mode_result() -> None:
    result = BenchResult(operation="Load", in_flight=2, num_keys=4, data_size_bytes=_MB)

    with pytest.raises(ValueError, match="BenchMode.SUSTAINED"):
        run_sustained_window(
            result,
            submit=lambda _i, _s: 0,
            harvest=lambda _p: {},
            event_fd=0,
            duration_sec=1.0,
            timeout=1.0,
            success_for=lambda _p: 0,
            log=lambda _m: None,
        )


def test_rejects_non_positive_in_flight() -> None:
    result = _sustained(in_flight=0)

    with pytest.raises(ValueError, match="in_flight must be positive"):
        run_sustained_window(
            result,
            submit=lambda _i, _s: 0,
            harvest=lambda _p: {},
            event_fd=0,
            duration_sec=1.0,
            timeout=1.0,
            success_for=lambda _p: 0,
            log=lambda _m: None,
        )


# ---------------------------------------------------------------------------
# Window behaviour
# ---------------------------------------------------------------------------


def test_window_holds_in_flight_submits_and_refills() -> None:
    """Concurrency must not sawtooth: no drain barrier between refills."""
    adapter = _FakeAdapter(service_sec=0.02)
    result = _sustained(in_flight=4)

    _run(adapter, result, duration_sec=0.4)
    adapter.join()

    # Far more submits than one wave, so refilling happened.
    assert result.completed_submits > 4
    # The window was never allowed to drain to zero mid-run: the fake
    # saw the full in_flight outstanding at some point, and each refill
    # replaced only completed slots.
    assert adapter.max_outstanding == 4
    assert result.sustained_window_sec > 0
    assert not result.timed_out


def test_every_completed_submit_yields_one_latency_and_one_success() -> None:
    adapter = _FakeAdapter(service_sec=0.01)
    result = _sustained(in_flight=3, num_keys=5)

    _run(adapter, result, duration_sec=0.2)
    adapter.join()

    assert len(result.submit_latencies) == result.completed_submits
    assert len(result.success_counts) == result.completed_submits
    assert result.total_keys == result.completed_submits * 5
    assert result.total_success == result.completed_submits * 5


def test_latency_is_at_least_the_service_time() -> None:
    adapter = _FakeAdapter(service_sec=0.05)
    result = _sustained(in_flight=2)

    _run(adapter, result, duration_sec=0.3)
    adapter.join()

    assert result.submit_latencies
    # Upper bound on service time, so never below it.
    assert min(result.submit_latencies) >= 0.05
    assert result.submit_latency_p50_ms >= 50.0


def test_slots_are_never_double_issued_while_outstanding() -> None:
    """A slot must be reissued only after its previous submit completed.

    This is what makes buffer reuse safe without a separate pool.
    """
    adapter = _FakeAdapter(service_sec=0.01)
    result = _sustained(in_flight=4)
    slots: list[int] = []

    _run(adapter, result, duration_sec=0.2, submitted_slots=slots)
    adapter.join()

    # Each slot index appears, and the first in_flight submits use
    # distinct slots.
    assert len(slots) > 4
    assert len(set(slots[:4])) == 4
    assert set(slots) <= {0, 1, 2, 3}


def test_submit_index_advances_and_is_returned() -> None:
    adapter = _FakeAdapter(service_sec=0.01)
    result = _sustained(in_flight=2)
    indices: list[int] = []

    def submit(submit_index: int, _slot: int) -> int:
        indices.append(submit_index)
        return adapter.submit(result.num_keys)

    next_index, outstanding = run_sustained_window(
        result,
        submit=submit,
        harvest=adapter.harvest,
        event_fd=adapter.event_fd,
        duration_sec=0.15,
        timeout=10.0,
        success_for=lambda payload: int(payload),
        log=lambda _msg: None,
        first_submit_index=100,
    )
    adapter.join()

    assert indices[0] == 100
    assert indices == list(range(100, 100 + len(indices)))
    assert next_index == 100 + len(indices)
    # A clean window leaves nothing in flight.
    assert outstanding == 0


def test_drain_tail_is_reported() -> None:
    """The ramp-down after the refill deadline is charged and reported.

    Service times are staggered so the final submits complete in separate
    harvests. If they all landed in one wakeup the tail would be exactly
    zero -- correct behaviour, but it would not exercise the tail path.
    """
    adapter = _FakeAdapter(service_sec=0.1, stagger_sec=0.02)
    result = _sustained(in_flight=4)

    _run(adapter, result, duration_sec=0.25)
    adapter.join()

    # Window spans past the deadline by roughly one service time.
    assert result.sustained_window_sec >= 0.25
    assert result.sustained_drain_sec > 0


def test_throughput_reflects_the_measured_window() -> None:
    adapter = _FakeAdapter(service_sec=0.01)
    result = _sustained(in_flight=4, num_keys=1, data_size_bytes=_MB)

    _run(adapter, result, duration_sec=0.3)
    adapter.join()

    expected = result.total_data_bytes / _MB / result.sustained_window_sec
    assert result.aggregate_throughput_mbps == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_timeout_sets_the_flag_and_stops() -> None:
    """A submit that never completes must be reported, not hang forever."""

    class _NeverCompletes:
        def __init__(self) -> None:
            self._notifier = create_event_notifier()

        @property
        def event_fd(self) -> int:
            return self._notifier.fileno()

        def close(self) -> None:
            self._notifier.close()

    adapter: Any = _NeverCompletes()
    result = _sustained(in_flight=2)
    task_id = iter(range(100))

    _next, outstanding = run_sustained_window(
        result,
        submit=lambda _i, _s: next(task_id),
        harvest=lambda _p: {},
        event_fd=adapter.event_fd,
        duration_sec=1.0,
        timeout=0.05,
        success_for=lambda _p: 0,
        log=lambda _m: None,
    )
    adapter.close()

    assert result.timed_out
    assert result.completed_submits == 0
    assert result.submit_latencies == []
    # Both submits still own their slots; the caller must not reuse them.
    assert outstanding == 2


def test_undrained_warmup_is_rejected() -> None:
    """A warmup that timed out must not hand its slots to the measured run.

    The measured window reissues the same buffer slots, so a still-live
    warmup submit could write into a buffer the measured window is using.
    """
    with pytest.raises(WarmupNotDrainedError, match="still outstanding"):
        _require_drained_warmup("Store", 3)


def test_drained_warmup_is_accepted() -> None:
    _require_drained_warmup("Store", 0)  # must not raise


def test_foreign_completions_are_ignored() -> None:
    """Harvest may surface task ids this window never issued.

    A prior window that ended on a timeout can leave completions queued
    in the adapter. Attributing them here would inflate the count.
    """
    adapter = _FakeAdapter(service_sec=0.01)
    result = _sustained(in_flight=2)

    def harvest(pending: set[int]) -> dict[int, int]:
        real = adapter.harvest(pending)
        # Inject an id outside the pending set.
        return {**real, 999_999: 4}

    run_sustained_window(
        result,
        submit=lambda _i, _s: adapter.submit(result.num_keys),
        harvest=harvest,
        event_fd=adapter.event_fd,
        duration_sec=0.2,
        timeout=10.0,
        success_for=lambda payload: int(payload),
        log=lambda _msg: None,
    )
    adapter.join()

    assert result.completed_submits > 0
    assert result.completed_submits == len(result.submit_latencies)
    assert result.completed_submits <= adapter.submitted
