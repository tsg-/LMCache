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
import torch

# First Party
from lmcache.cli.commands.bench.l2_adapter_bench import runner as runner_module
from lmcache.cli.commands.bench.l2_adapter_bench.result import (
    BenchMode,
    BenchResult,
)
from lmcache.cli.commands.bench.l2_adapter_bench.data import make_object_keys
from lmcache.cli.commands.bench.l2_adapter_bench.runner import (
    StoreFreshnessUnknownError,
    SubmitSuccess,
    WarmupNotDrainedError,
    _record_round_latencies,
    bench_mixed_sustained,
    _require_drained_warmup,
    count_existing_keys,
    require_empty_store_namespace,
    run_sustained_mixed_window,
    run_sustained_window,
)
from lmcache.cli.commands.bench.l2_adapter_bench.data import wait_eventfds
from lmcache.cli.commands.bench.l2_adapter_bench.metrics import (
    PHASE_MEASURED,
    BenchMetricsState,
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


_NO_SUCCESS = SubmitSuccess(objects=0, payload_bytes=0)


def _int_success(payload: Any) -> SubmitSuccess:
    """Read an int payload as a key count at the fake 1 MiB object size."""
    keys = int(payload)
    return SubmitSuccess(objects=keys, payload_bytes=keys * _MB)


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
        success_for=_int_success,
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
            success_for=lambda _p: _NO_SUCCESS,
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
            success_for=lambda _p: _NO_SUCCESS,
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
            success_for=lambda _p: _NO_SUCCESS,
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
    assert result.success_counts == []
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
        success_for=_int_success,
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


def test_drain_tail_starts_at_the_refill_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first completion after the deadline belongs to the drain tail."""
    timestamps = iter([0.0, 0.0, 1.2])
    monkeypatch.setattr(runner_module.time, "perf_counter", lambda: next(timestamps))
    monkeypatch.setattr(runner_module, "wait_eventfd", lambda *_args, **_kwargs: True)
    result = _sustained(in_flight=1, num_keys=1)

    _next, outstanding = run_sustained_window(
        result,
        submit=lambda _index, _slot: 1,
        harvest=lambda _pending: {1: 1},
        event_fd=0,
        duration_sec=1.0,
        timeout=1.0,
        success_for=_int_success,
        log=lambda _message: None,
    )

    assert outstanding == 0
    assert result.sustained_window_sec == pytest.approx(1.2)
    assert result.sustained_drain_sec == pytest.approx(0.2)


def test_phase_timestamps_bracket_the_window() -> None:
    """``phase_started_at``/``phase_ended_at`` are real wall-clock marks.

    A live observer (the --serve-metrics collector) reads these to tell
    how long a phase has been open, or that it has closed, without
    knowing the configured duration -- so they must be plausible
    ``time.time()`` values, not left at their zero default, and ordered.
    """
    adapter = _FakeAdapter(service_sec=0.01)
    result = _sustained(in_flight=4)
    before = time.time()

    _run(adapter, result, duration_sec=0.1)
    adapter.join()

    after = time.time()
    assert before <= result.phase_started_at <= result.phase_ended_at <= after
    assert result.phase_ended_at - result.phase_started_at >= 0.1


def test_phase_end_stays_zero_until_the_window_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The end mark must not be predicted from the configured duration.

    Checked via the timeout path specifically: a run that times out with
    submits still outstanding still closes its window (the function
    returns), so ``phase_ended_at`` must be set even though the window
    never drained cleanly. ``wait_eventfd`` is faked to time out
    immediately rather than polling a real fd, matching
    ``test_drain_tail_starts_at_the_refill_deadline``.
    """
    monkeypatch.setattr(runner_module, "wait_eventfd", lambda *_args, **_kwargs: False)
    result = _sustained(in_flight=1, num_keys=1)
    assert result.phase_ended_at == 0.0

    run_sustained_window(
        result,
        submit=lambda _index, _slot: 1,
        harvest=lambda _pending: {},
        event_fd=0,
        duration_sec=0.05,
        timeout=0.01,
        success_for=_int_success,
        log=lambda _message: None,
    )

    assert result.timed_out is True
    assert result.phase_ended_at > 0.0


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
        success_for=lambda _p: _NO_SUCCESS,
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
        success_for=_int_success,
        log=lambda _msg: None,
    )
    adapter.join()

    assert result.completed_submits > 0
    assert result.completed_submits == len(result.submit_latencies)
    assert result.completed_submits <= adapter.submitted


# ---------------------------------------------------------------------------
# Mixed sustained window
# ---------------------------------------------------------------------------


class _FakeStoreResult:
    """Minimal store completion with the interface the runner consumes."""

    def __init__(self, successful: bool) -> None:
        self._successful = successful

    def is_successful(self) -> bool:
        return self._successful


class _FakeBitmap:
    """Minimal load bitmap with the interface the runner consumes."""

    def __init__(self, success_keys: int) -> None:
        self._success_keys = success_keys

    def popcount(self) -> int:
        return self._success_keys

    def get_indices_list(self) -> list[int]:
        return list(range(self._success_keys))


class _MixedFakeAdapter:
    """Asynchronously completes store and load tasks on separate eventfds."""

    def __init__(
        self,
        *,
        service_sec: float = 0.002,
        store_successful: bool = True,
        load_success_keys: int | None = None,
        stall_operation: str | None = None,
        corrupt_load_payload: bool = False,
    ) -> None:
        self._store_notifier = create_event_notifier()
        self._load_notifier = create_event_notifier()
        self._service_sec = service_sec
        self._store_successful = store_successful
        self._load_success_keys = load_success_keys
        self._stall_operation = stall_operation
        self._corrupt_load_payload = corrupt_load_payload
        self._lock = threading.Lock()
        self._next_ids = {"Store": 0, "Load": 0}
        self._store_done: dict[int, _FakeStoreResult] = {}
        self._load_done: dict[int, _FakeBitmap] = {}
        self._stored_payloads: dict[tuple[int, ...], list[torch.Tensor]] = {}
        self._threads: list[threading.Thread] = []
        self._outstanding = 0
        self.max_outstanding = 0
        self.submissions: list[tuple[str, int]] = []
        self.completion_order: list[str] = []

    def get_store_event_fd(self) -> int:
        return self._store_notifier.fileno()

    def get_load_event_fd(self) -> int:
        return self._load_notifier.fileno()

    def submit_store_task(self, keys: list[Any], objects: list[Any]) -> int:
        return self._submit("Store", keys, objects)

    def submit_load_task(self, keys: list[Any], objects: list[Any]) -> int:
        return self._submit("Load", keys, objects)

    def _submit(self, operation: str, keys: list[Any], objects: list[Any]) -> int:
        with self._lock:
            task_id = self._next_ids[operation]
            self._next_ids[operation] += 1
            self._outstanding += 1
            self.max_outstanding = max(self.max_outstanding, self._outstanding)
            self.submissions.append((operation, task_id))
        if operation == self._stall_operation:
            return task_id
        thread = threading.Thread(
            target=self._complete,
            args=(operation, task_id, keys, objects),
            daemon=True,
        )
        self._threads.append(thread)
        thread.start()
        return task_id

    def _complete(
        self,
        operation: str,
        task_id: int,
        keys: list[Any],
        objects: list[Any],
    ) -> None:
        # Stores are deliberately slower, which forces cross-operation
        # completion reordering and exercises the two-FD poll path.
        time.sleep(self._service_sec * (2 if operation == "Store" else 1))
        with self._lock:
            if operation == "Store":
                if all(hasattr(obj, "raw_data") for obj in objects):
                    self._stored_payloads[tuple(map(id, keys))] = [
                        obj.raw_data.clone() for obj in objects
                    ]
                self._store_done[task_id] = _FakeStoreResult(self._store_successful)
            else:
                stored = self._stored_payloads.get(tuple(map(id, keys)))
                if stored is not None:
                    for obj, payload in zip(objects, stored, strict=True):
                        obj.raw_data.copy_(payload)
                if self._corrupt_load_payload:
                    for obj in objects:
                        obj.raw_data.fill_(0xA5)
                loaded = (
                    len(keys)
                    if self._load_success_keys is None
                    else self._load_success_keys
                )
                self._load_done[task_id] = _FakeBitmap(loaded)
            self._outstanding -= 1
            self.completion_order.append(operation)
        notifier = self._store_notifier if operation == "Store" else self._load_notifier
        notifier.notify()

    def pop_completed_store_tasks(self) -> dict[int, _FakeStoreResult]:
        with self._lock:
            completed = self._store_done
            self._store_done = {}
        return completed

    def query_load_result(self, task_id: int) -> _FakeBitmap | None:
        with self._lock:
            return self._load_done.pop(task_id, None)

    def close(self) -> None:
        for thread in self._threads:
            thread.join(timeout=5.0)
        self._store_notifier.close()
        self._load_notifier.close()


def _mixed_result(operation: str, in_flight: int = 6, num_keys: int = 1) -> BenchResult:
    return BenchResult(
        operation=operation,
        in_flight=in_flight,
        num_keys=num_keys,
        data_size_bytes=_MB,
        mode=BenchMode.SUSTAINED,
    )


def _run_mixed(
    adapter: _MixedFakeAdapter,
    *,
    ratio: tuple[int, int] = (5, 1),
    in_flight: int = 6,
    num_keys: int = 1,
    duration_sec: float = 0.2,
    timeout: float = 1.0,
) -> tuple[BenchResult, BenchResult, bool]:
    load_result = _mixed_result("Load", in_flight, num_keys)
    store_result = _mixed_result("Store", in_flight, num_keys)
    store_objects = [
        [_MixedMemoryObject(slot + key) for key in range(num_keys)]
        for slot in range(in_flight)
    ]
    load_objects = [
        [_MixedMemoryObject(0xA5) for _ in range(num_keys)] for _ in range(in_flight)
    ]
    accepted = run_sustained_mixed_window(
        adapter=adapter,
        load_result=load_result,
        store_result=store_result,
        read_write_ratio=ratio,
        load_keys_for_submit=lambda _index: [object()] * num_keys,
        store_keys_for_submit=lambda _index: [object()] * num_keys,
        load_objs_for_slot=lambda slot: load_objects[slot],
        store_objs_for_slot=lambda slot: store_objects[slot],
        duration_sec=duration_sec,
        timeout=timeout,
        log=lambda _message: None,
    )
    return load_result, store_result, accepted


class _MixedMemoryObject:
    """Tensor-backed object sufficient for mixed sample verification tests."""

    def __init__(self, fill: int) -> None:
        self.raw_data = torch.full((8,), fill, dtype=torch.uint8)

    def get_physical_size(self) -> int:
        return self.raw_data.numel()


def test_wait_eventfds_returns_every_ready_operation() -> None:
    store_notifier = create_event_notifier()
    load_notifier = create_event_notifier()
    try:
        store_notifier.notify()
        load_notifier.notify()

        assert wait_eventfds(
            {
                "Store": store_notifier.fileno(),
                "Load": load_notifier.fileno(),
            },
            timeout=0.1,
        ) == {"Store", "Load"}
    finally:
        store_notifier.close()
        load_notifier.close()


def test_wait_eventfds_rejects_duplicate_eventfds() -> None:
    """One consumed notification cannot safely identify two operations."""
    notifier = create_event_notifier()
    try:
        with pytest.raises(ValueError, match="distinct completion eventfd"):
            wait_eventfds(
                {"Store": notifier.fileno(), "Load": notifier.fileno()},
                timeout=0.1,
            )
    finally:
        notifier.close()


@pytest.mark.parametrize("ratio", [(5, 1), (1, 1)])
def test_mixed_window_holds_one_global_limit_and_hits_requested_ratio(
    ratio: tuple[int, int],
) -> None:
    adapter = _MixedFakeAdapter()
    try:
        load_result, store_result, accepted = _run_mixed(
            adapter, ratio=ratio, duration_sec=0.5
        )
    finally:
        adapter.close()

    assert accepted
    assert adapter.max_outstanding == 6
    assert adapter.completion_order[0] == "Load"
    assert load_result.total_success == load_result.total_keys
    assert store_result.total_success == store_result.total_keys
    assert load_result.sustained_window_sec == store_result.sustained_window_sec
    assert load_result.sustained_drain_sec == store_result.sustained_drain_sec
    assert load_result.total_success / store_result.total_success == pytest.approx(
        ratio[0] / ratio[1], rel=0.01
    )


def test_mixed_window_accepts_overlapping_task_ids() -> None:
    """Task id zero is valid concurrently for one store and one load."""
    adapter = _MixedFakeAdapter()
    try:
        load_result, store_result, accepted = _run_mixed(adapter, duration_sec=1.0)
    finally:
        adapter.close()

    assert accepted
    assert ("Store", 0) in adapter.submissions
    assert ("Load", 0) in adapter.submissions
    assert load_result.completed_submits > 0
    assert store_result.completed_submits > 0


def test_mixed_window_rejects_a_failed_store() -> None:
    adapter = _MixedFakeAdapter(store_successful=False)
    try:
        _load_result, store_result, accepted = _run_mixed(adapter)
    finally:
        adapter.close()

    assert not accepted
    assert store_result.total_success == 0


def test_mixed_window_rejects_a_partial_load() -> None:
    adapter = _MixedFakeAdapter(load_success_keys=1)
    try:
        load_result, _store_result, accepted = _run_mixed(adapter, num_keys=2)
    finally:
        adapter.close()

    assert not accepted
    assert load_result.total_success < load_result.total_keys


def test_mixed_window_rejects_a_corrupt_write_readback() -> None:
    """A full completion bitmap does not substitute for byte integrity."""
    adapter = _MixedFakeAdapter(corrupt_load_payload=True)
    store_objects = [_MixedMemoryObject(0x33)]
    load_objects = [_MixedMemoryObject(0xA5)]
    try:
        load_result = _mixed_result("Load")
        store_result = _mixed_result("Store")
        accepted = run_sustained_mixed_window(
            adapter=adapter,
            load_result=load_result,
            store_result=store_result,
            read_write_ratio=(5, 1),
            load_keys_for_submit=lambda _index: [object()],
            store_keys_for_submit=lambda _index: [object()],
            load_objs_for_slot=lambda _slot: load_objects,
            store_objs_for_slot=lambda _slot: store_objects,
            duration_sec=0.1,
            timeout=1.0,
            log=lambda _message: None,
        )
    finally:
        adapter.close()

    assert load_result.total_success == load_result.total_keys
    assert store_result.total_success == store_result.total_keys
    assert not accepted


@pytest.mark.parametrize("stalled_operation", ["Store", "Load"])
def test_mixed_window_rejects_a_timeout_from_either_direction(
    stalled_operation: str,
) -> None:
    adapter = _MixedFakeAdapter(stall_operation=stalled_operation)
    try:
        load_result, store_result, accepted = _run_mixed(
            adapter,
            duration_sec=0.03,
            timeout=0.03,
        )
    finally:
        adapter.close()

    assert not accepted
    assert load_result.timed_out
    assert store_result.timed_out


def test_mixed_window_registers_both_measured_operation_series() -> None:
    adapter = _MixedFakeAdapter()
    state = BenchMetricsState()
    store_objects = [[_MixedMemoryObject(slot)] for slot in range(6)]
    load_objects = [[_MixedMemoryObject(0xA5)] for _ in range(6)]
    try:
        load_result, store_result, accepted = bench_mixed_sustained(
            adapter,
            in_flight=6,
            num_keys=1,
            data_size=_MB,
            duration_sec=0.5,
            read_write_ratio=(5, 1),
            load_keys_for_submit=lambda _index: [object()],
            store_keys_for_submit=lambda _index: [object()],
            load_objs_for_slot=lambda slot: load_objects[slot],
            store_objs_for_slot=lambda slot: store_objects[slot],
            log=lambda _message: None,
            on_result=lambda result: state.register(
                result.operation, result, PHASE_MEASURED
            ),
        )
    finally:
        adapter.close()

    assert accepted
    assert {key for key, _result in state.snapshot()} == {
        ("Load", PHASE_MEASURED),
        ("Store", PHASE_MEASURED),
    }
    assert load_result.completed_submits > 0
    assert store_result.completed_submits > 0


# ---------------------------------------------------------------------------
# Store-freshness probe -- must fail closed
# ---------------------------------------------------------------------------


class _StallingLookupAdapter:
    """Accepts a lookup-and-lock submit and never completes it.

    Models an adapter whose lookup path is wedged: the task id comes back,
    but the eventfd is never signalled, so the probe cannot learn whether
    the keys exist.
    """

    def __init__(self) -> None:
        self._notifier = create_event_notifier()
        self.unlocked = False

    def submit_lookup_and_lock_task(self, keys: Any, layout_desc: Any) -> int:
        return 0

    def get_lookup_and_lock_event_fd(self) -> int:
        return self._notifier.fileno()

    def query_lookup_and_lock_result(self, task_id: int) -> None:
        return None

    def submit_unlock(self, keys: Any) -> None:
        self.unlocked = True

    def close(self) -> None:
        self._notifier.close()


def test_a_timed_out_freshness_probe_raises() -> None:
    """An unanswered probe must not be read as "namespace is empty".

    A timeout means freshness is UNKNOWN. Returning 0 would let a store
    run proceed into a possibly-populated namespace, where backends that
    short-circuit an existing key report success without writing -- the
    exact fiction this gate exists to prevent. Fail closed.
    """
    adapter = _StallingLookupAdapter()
    keys = make_object_keys(2, model_name="stalled")

    with pytest.raises(StoreFreshnessUnknownError, match="did not complete"):
        count_existing_keys(adapter, keys, timeout=0.05)

    adapter.close()


def test_require_empty_store_namespace_propagates_the_timeout() -> None:
    """The gate the CLI calls must surface the timeout, not swallow it."""
    adapter = _StallingLookupAdapter()
    keys = make_object_keys(2, model_name="stalled")

    with pytest.raises(StoreFreshnessUnknownError):
        require_empty_store_namespace(
            adapter,
            keys=keys,
            namespace="stalled",
            log=lambda _msg: None,
            timeout=0.05,
        )

    adapter.close()


def test_an_empty_key_list_needs_no_probe() -> None:
    """No keys means nothing to collide with; must not raise."""
    adapter = _StallingLookupAdapter()

    assert count_existing_keys(adapter, [], timeout=0.05) == 0

    adapter.close()
