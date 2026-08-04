# SPDX-License-Identifier: Apache-2.0
"""Benchmark runners for L2 adapter ops.

Two measurement modes are available, both driven by a single producer
thread (the adapter internally is free to use threads / coroutines /
async I/O).

**Rounds mode** (:attr:`BenchMode.ROUNDS`, the default). Each round
issues ``in_flight`` submits sequentially, then waits for ``in_flight``
completion notifications before recording the round duration. This
matches the real-world usage pattern where multiple producers submit
tasks and the L2 adapter's worker coroutine processes them. The cost is
a drain barrier at every round edge: the worker pool has nothing queued
while the producer rebuilds keys and buffers for the next round.

**Sustained mode** (:attr:`BenchMode.SUSTAINED`, ``duration_sec > 0``).
``in_flight`` submits are issued once, then exactly one replacement
submit is issued per completion until the deadline expires, after which
the remaining window drains. Concurrency stays at ``in_flight`` for the
whole window instead of sawtoothing to zero, so this is the mode to use
for a steady-state throughput number.

Both modes record per-submit observed latencies. See
:attr:`BenchResult.submit_latencies` for what that measurement does and
does not mean.
"""

# Future
from __future__ import annotations

# Standard
from typing import Any, Callable
import time

# First Party
from lmcache.native_storage_ops import Bitmap
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
from lmcache.v1.distributed.internal_api import L2StoreResult
from lmcache.v1.memory_management import MemoryObj

# Local
from .data import wait_eventfd
from .result import BenchMode, BenchResult

# Logger callable type: takes a single string and prints / logs it.
LogFn = Callable[[str], None]

# Provider callable signatures used by the runners. They are invoked at
# the start of every round and must return ``in_flight`` lists, one per
# in-flight submit, of length ``num_keys`` each.
KeyProvider = Callable[[int], list[list[ObjectKey]]]
ObjProvider = Callable[[int], list[list[MemoryObj]]]

# Sustained-mode providers, keyed by *submit index* (keys) and *window
# slot* (buffers). Slots are recycled on completion, so a slot's buffers
# are never concurrently owned by two outstanding submits.
SubmitKeyProvider = Callable[[int], list[ObjectKey]]
SlotObjProvider = Callable[[int], list[MemoryObj]]

# Issues one submit and returns its task id. Takes (submit_index, slot).
SubmitFn = Callable[[int, int], int]
# Drains whatever has completed out of the pending task-id set.
HarvestFn = Callable[[set[int]], dict[int, Any]]
# Maps one harvested completion payload to a success key count.
SuccessFn = Callable[[Any], int]

# TODO: bench passes a placeholder layout_desc; a real layout may be
# required here in the future (e.g. when benchmarking the P2P adapter).
_PLACEHOLDER_LAYOUT_DESC = MemoryLayoutDesc(shapes=[], dtypes=[])

_STORE_TIMEOUT_SEC = 120.0
_LOOKUP_TIMEOUT_SEC = 60.0
_LOAD_TIMEOUT_SEC = 120.0


def _bitmap_count(bitmap: Bitmap | None) -> int:
    """Count how many bits are set in *bitmap*. Returns 0 when None."""
    if bitmap is None:
        return 0
    return bitmap.popcount()


def _store_success_keys(res: L2StoreResult, num_keys: int) -> int:
    """Number of keys a store submit landed: all of them, or none."""
    return num_keys if res.is_successful() else 0


def _record_round_latencies(
    result: BenchResult,
    task_ids: list[int],
    observed_at: dict[int, float],
    submitted_at: dict[int, float],
) -> int:
    """Append this round's per-submit latencies to *result*.

    Args:
        result: Result object to append to.
        task_ids: Task ids issued this round, in submit order.
        observed_at: Harvest timestamp per completed task id. Tasks
            missing from this dict never completed and are skipped.
        submitted_at: Submit timestamp per task id, captured immediately
            *before* each individual submit call. A single round-wide
            origin would charge every submit from the end of the submit
            loop, hiding the loop's own span from the early submits and
            underestimating their latency.

    Returns:
        How many latencies were appended, so a timed-out round is
        accounted exactly rather than assumed to be ``in_flight``.
    """
    appended = 0
    for task_id in task_ids:
        if task_id in observed_at and task_id in submitted_at:
            result.submit_latencies.append(observed_at[task_id] - submitted_at[task_id])
            appended += 1
    return appended


def _wait_store_finished(
    adapter, task_ids: list[int], timeout: float
) -> tuple[dict[int, L2StoreResult], dict[int, float]]:
    """Wait for all store tasks to finish.

    Returns ``(completed, observed_at)`` where ``completed`` is the
    accumulated ``{task_id: L2StoreResult}`` dict and ``observed_at``
    maps each task id to the ``perf_counter`` timestamp at which the
    producer thread harvested it. ``pop_completed_store_tasks`` consumes
    the adapter's completion dict, so we must accumulate the results here
    for the caller to use. On timeout, returns whatever was harvested so
    far (possibly empty or partial); the caller can detect timeout by
    comparing ``len(completed)`` against ``len(task_ids)``.

    Every task harvested in the same wakeup shares one timestamp -- see
    :attr:`BenchResult.submit_latencies`.
    """
    unfinished = len(task_ids)
    efd = adapter.get_store_event_fd()
    completed: dict[int, L2StoreResult] = {}
    observed_at: dict[int, float] = {}
    while unfinished > 0:
        if not wait_eventfd(efd, timeout=timeout):
            return completed, observed_at
        batch = adapter.pop_completed_store_tasks()
        now = time.perf_counter()
        for task_id in batch:
            observed_at[task_id] = now
        completed.update(batch)
        unfinished -= len(batch)
    return completed, observed_at


def _wait_load_finished(
    adapter, task_ids: list[int], timeout: float
) -> tuple[dict[int, Bitmap], dict[int, float]]:
    """Wait for all load tasks to finish.

    Returns ``({task_id: bitmap}, {task_id: harvest_timestamp})``.
    ``query_load_result`` consumes the per-task result, so we cache the
    bitmaps here for the caller. Already-finished tasks are removed from
    the pending set so subsequent wakeups don't re-query them. On
    timeout, returns whatever was harvested so far; the caller can detect
    timeout by comparing ``len(results)`` against ``len(task_ids)``.
    """
    pending = set(task_ids)
    efd = adapter.get_load_event_fd()
    results: dict[int, Bitmap] = {}
    observed_at: dict[int, float] = {}
    while pending:
        if not wait_eventfd(efd, timeout=timeout):
            return results, observed_at
        now = time.perf_counter()
        for task_id in list(pending):
            bitmap = adapter.query_load_result(task_id)
            if bitmap is not None:
                results[task_id] = bitmap
                observed_at[task_id] = now
                pending.remove(task_id)
    return results, observed_at


def _wait_lookup_finished(
    adapter, task_ids: list[int], timeout: float
) -> tuple[dict[int, Bitmap], dict[int, float]]:
    """Wait for all lookup-and-lock tasks to finish.

    Returns ``({task_id: bitmap}, {task_id: harvest_timestamp})``.
    ``query_lookup_and_lock_result`` consumes the per-task result, so we
    cache the bitmaps here for the caller. Already-finished tasks are
    removed from the pending set so subsequent wakeups don't re-query
    them. On timeout, returns whatever was harvested so far; the caller
    can detect timeout by comparing ``len(results)`` against
    ``len(task_ids)``.
    """
    pending = set(task_ids)
    efd = adapter.get_lookup_and_lock_event_fd()
    results: dict[int, Bitmap] = {}
    observed_at: dict[int, float] = {}
    while pending:
        if not wait_eventfd(efd, timeout=timeout):
            return results, observed_at
        now = time.perf_counter()
        for task_id in list(pending):
            bitmap = adapter.query_lookup_and_lock_result(task_id)
            if bitmap is not None:
                results[task_id] = bitmap
                observed_at[task_id] = now
                pending.remove(task_id)
    return results, observed_at


class StoreNamespaceNotEmptyError(RuntimeError):
    """A store benchmark was asked to write keys that already exist."""


def count_existing_keys(adapter, keys: list[ObjectKey], timeout: float) -> int:
    """Count how many of *keys* the adapter already holds.

    Uses ``submit_lookup_and_lock_task`` and immediately unlocks, so it
    does not disturb the objects. Every adapter implements lookup, so this
    works without reaching into any backend's internals.

    Args:
        adapter: L2 adapter to probe.
        keys: Keys to test for existence.
        timeout: Seconds to wait for the lookup to complete.

    Returns:
        Number of *keys* the adapter reported present. Returns 0 when the
        lookup timed out -- a probe that could not answer must not block
        the benchmark.
    """
    if not keys:
        return 0
    task_id = adapter.submit_lookup_and_lock_task(keys, _PLACEHOLDER_LAYOUT_DESC)
    results, _observed = _wait_lookup_finished(adapter, [task_id], timeout)
    bitmap = results.get(task_id)
    if bitmap is None:
        return 0
    found = _bitmap_count(bitmap)
    adapter.submit_unlock(keys)
    return found


def require_empty_store_namespace(
    adapter,
    keys: list[ObjectKey],
    namespace: str,
    log: LogFn,
    timeout: float = _LOOKUP_TIMEOUT_SEC,
) -> None:
    """Fail if a store benchmark would target keys that already exist.

    A store submit whose key is already present is reported successful by
    backends that short-circuit on existence -- ``fs_native`` returns
    early from ``do_single_set`` when the file is there
    (csrc/storage_backends/fs/connector.cpp). The harness counts that as
    all keys transferred, so the run would advertise a full write rate
    having written nothing. Keys are a pure function of ``(namespace, key
    index)`` and the index restarts at zero every invocation, so a
    repeated store run hits this by default.

    Args:
        adapter: L2 adapter about to be benchmarked.
        keys: A sample of the keys the store phase will write. The first
            wave is enough -- if it is clean the namespace was not used
            at this geometry.
        namespace: Namespace those keys belong to, for the error message.
        log: Progress logger.
        timeout: Seconds to wait for the existence probe.

    Raises:
        StoreNamespaceNotEmptyError: Any of *keys* already exists.
    """
    found = count_existing_keys(adapter, keys, timeout)
    if found == 0:
        return
    raise StoreNamespaceNotEmptyError(
        f"{found} of the {len(keys)} keys this store run would write already "
        f"exist in namespace '{namespace}'. Backends that short-circuit an "
        f"existing key report success without writing, so the measured "
        f"throughput would be fictitious. Pass a fresh --key-prefix, or "
        f"clear the backing store."
    )


# ---------------------------------------------------------------------------
# Sustained-window driver
# ---------------------------------------------------------------------------


class WarmupNotDrainedError(RuntimeError):
    """A sustained warmup window ended with submits still outstanding.

    The measured window reuses the warmup's buffer slots, so proceeding
    would let an in-flight warmup submit write into a buffer the measured
    window has already reissued. Raised instead of silently continuing.
    """


def _require_drained_warmup(operation: str, outstanding: int) -> None:
    """Abort if a warmup window left submits outstanding.

    Args:
        operation: Operation name, for the message.
        outstanding: Submits still in flight when the warmup ended.

    Raises:
        WarmupNotDrainedError: If *outstanding* is nonzero.
    """
    if outstanding > 0:
        raise WarmupNotDrainedError(
            f"{operation} warmup window timed out with {outstanding} submits "
            f"still outstanding. Their window slots cannot be reused safely, "
            f"so the measured window is not run. Investigate the stall, or "
            f"lower --in-flight."
        )


def run_sustained_window(
    result: BenchResult,
    submit: SubmitFn,
    harvest: HarvestFn,
    event_fd: int,
    duration_sec: float,
    timeout: float,
    success_for: SuccessFn,
    log: LogFn,
    first_submit_index: int = 0,
) -> tuple[int, int]:
    """Hold ``result.in_flight`` outstanding submits for *duration_sec*.

    Issues ``result.in_flight`` submits, then exactly one replacement per
    completion until the deadline passes, then drains the remaining
    window. Populates ``result.submit_latencies``,
    ``result.success_counts`` (one entry per *submit*, not per round),
    ``result.completed_submits``, ``result.sustained_window_sec`` and
    ``result.sustained_drain_sec``.

    ``sustained_window_sec`` spans the first submit to the last observed
    completion, so the ramp-down tail is charged to the run. That makes
    the derived throughput conservative;
    ``result.sustained_drain_sec`` reports how much of the window was
    tail so an over-short run is visible rather than silent.

    Args:
        result: Result object to populate. ``result.mode`` must be
            :attr:`BenchMode.SUSTAINED`.
        submit: Issues one submit given ``(submit_index, slot)`` and
            returns its task id. Task ids must be unique while
            outstanding.
        harvest: Given the set of outstanding task ids, returns the
            subset that has completed, mapped to its result payload.
        event_fd: Completion-notification fd for this operation.
        duration_sec: How long to keep refilling the window. Must be
            positive.
        timeout: Per-wait timeout in seconds. A wait that expires ends
            the run and sets ``result.timed_out``.
        success_for: Maps a harvested payload to a success key count.
        log: Progress logger.
        first_submit_index: Starting submit index, so a warmup pass can
            hand its end state to the measured pass.

    Returns:
        ``(next unused submit index, submits still outstanding)``. The
        second element is nonzero only when the run ended on a timeout;
        those submits still own their window slots, so the caller must
        not reuse the slots or the buffers behind them.

    Raises:
        ValueError: If ``duration_sec`` is not positive, ``in_flight`` is
            not positive, or ``result.mode`` is not
            :attr:`BenchMode.SUSTAINED`.
    """
    if duration_sec <= 0:
        raise ValueError("duration_sec must be positive")
    if result.in_flight <= 0:
        raise ValueError("in_flight must be positive")
    if result.mode is not BenchMode.SUSTAINED:
        raise ValueError("run_sustained_window requires BenchMode.SUSTAINED")

    in_flight = result.in_flight
    submit_index = first_submit_index
    # task_id -> (submit timestamp, window slot)
    pending: dict[int, tuple[float, int]] = {}
    free_slots = list(range(in_flight))

    t_start = time.perf_counter()
    deadline = t_start + duration_sec
    for _ in range(in_flight):
        slot = free_slots.pop()
        # Timestamp before the call so the recorded latency includes the
        # submit itself, keeping it an upper bound on service time.
        submitted_at = time.perf_counter()
        pending[submit(submit_index, slot)] = (submitted_at, slot)
        submit_index += 1

    last_observed = t_start
    refill_end = 0.0

    while pending:
        if not wait_eventfd(event_fd, timeout=timeout):
            log(
                f"  [{result.operation}] TIMEOUT after "
                f"{timeout:.0f}s with {len(pending)} submits outstanding"
            )
            result.timed_out = True
            break
        # ``harvest`` may hand back task ids this window never issued
        # (e.g. a prior window that ended on a timeout left completions
        # queued in the adapter). Drop them rather than mis-attribute.
        completed = {
            tid: payload
            for tid, payload in harvest(set(pending)).items()
            if tid in pending
        }
        if not completed:
            # Spurious wakeup, or only foreign completions. Nothing to
            # account; go back to waiting.
            continue
        now = time.perf_counter()
        last_observed = now
        for task_id, payload in completed.items():
            submitted_at, slot = pending.pop(task_id)
            result.submit_latencies.append(now - submitted_at)
            result.success_counts.append(success_for(payload))
            result.completed_submits += 1
            free_slots.append(slot)

        if now < deadline:
            while free_slots:
                slot = free_slots.pop()
                submitted_at = time.perf_counter()
                pending[submit(submit_index, slot)] = (submitted_at, slot)
                submit_index += 1
        elif refill_end == 0.0:
            refill_end = now

    result.sustained_window_sec = last_observed - t_start
    result.sustained_drain_sec = (
        max(0.0, last_observed - refill_end) if refill_end > 0.0 else 0.0
    )
    return submit_index, len(pending)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


def bench_store(
    adapter,
    in_flight: int,
    num_keys: int,
    data_size: int,
    rounds: int,
    keys_for_round: KeyProvider,
    objs_for_round: ObjProvider,
    log: LogFn,
) -> BenchResult:
    """Benchmark ``submit_store_task`` in rounds mode.

    For each round, ``in_flight`` independent submits are issued; the
    round duration is the wall-clock time from the first submit until
    every submit of that round has completed.
    """
    result = BenchResult(
        operation="Store",
        in_flight=in_flight,
        num_keys=num_keys,
        data_size_bytes=data_size,
    )

    for r in range(rounds):
        keys_batches = keys_for_round(r)
        obj_batches = objs_for_round(r)
        assert len(keys_batches) == in_flight
        assert len(obj_batches) == in_flight

        t0 = time.perf_counter()
        task_ids: list[int] = []
        submitted_at: dict[int, float] = {}
        for i in range(in_flight):
            # Stamp before each submit so the recorded latency covers
            # that submit's own call and every submit is charged from its
            # own origin rather than the end of the loop.
            issued = time.perf_counter()
            task_id = adapter.submit_store_task(keys_batches[i], obj_batches[i])
            task_ids.append(task_id)
            submitted_at[task_id] = issued

        completed, observed_at = _wait_store_finished(
            adapter, task_ids, _STORE_TIMEOUT_SEC
        )
        t1 = time.perf_counter()
        elapsed = t1 - t0
        timed_out = len(completed) < len(task_ids)

        result.round_latency_counts.append(
            _record_round_latencies(result, task_ids, observed_at, submitted_at)
        )

        success_keys = sum(
            len(keys_batches[i])
            for i, tid in enumerate(task_ids)
            if completed.get(tid, L2StoreResult(False, 0)).is_successful()
        )

        if timed_out:
            log(
                f"  [Store] Round {r + 1}: TIMEOUT "
                f"({len(completed)}/{len(task_ids)} tasks completed, "
                f"success_keys={success_keys}/{in_flight * num_keys})"
            )
            result.round_starts.append(t0)
            result.round_durations.append(float("inf"))
            result.success_counts.append(success_keys)
            result.timed_out = True
            continue

        result.round_starts.append(t0)
        result.round_durations.append(elapsed)
        result.success_counts.append(success_keys)
        log(
            f"  [Store] Round {r + 1}: {elapsed * 1000:.2f} ms, "
            f"success_keys={success_keys}/{in_flight * num_keys}"
        )

    return result


def bench_store_sustained(
    adapter,
    in_flight: int,
    num_keys: int,
    data_size: int,
    duration_sec: float,
    warmup_sec: float,
    keys_for_submit: SubmitKeyProvider,
    objs_for_slot: SlotObjProvider,
    log: LogFn,
) -> BenchResult:
    """Benchmark ``submit_store_task`` in sustained-window mode.

    Args:
        adapter: L2 adapter under test.
        in_flight: Outstanding submits held for the whole window.
        num_keys: Keys per submit.
        data_size: Payload bytes per key.
        duration_sec: Measured window length in seconds.
        warmup_sec: Discarded window run first; skipped when <= 0.
        keys_for_submit: Keys for a given submit index.
        objs_for_slot: Store buffers for a given window slot. Store
            source buffers are read-only, so slot reuse is safe.
        log: Progress logger.

    Returns:
        A :class:`BenchResult` in :attr:`BenchMode.SUSTAINED`.
    """

    def _submit(submit_index: int, slot: int) -> int:
        return adapter.submit_store_task(
            keys_for_submit(submit_index), objs_for_slot(slot)
        )

    def _harvest(_pending: set[int]) -> dict[int, L2StoreResult]:
        return adapter.pop_completed_store_tasks()

    def _success(payload: L2StoreResult) -> int:
        return _store_success_keys(payload, num_keys)

    next_index = 0
    if warmup_sec > 0:
        log(f"[Store] Sustained warmup for {warmup_sec:.1f}s (discarded)...")
        warmup = BenchResult(
            operation="Store",
            in_flight=in_flight,
            num_keys=num_keys,
            data_size_bytes=data_size,
            mode=BenchMode.SUSTAINED,
        )
        next_index, outstanding = run_sustained_window(
            warmup,
            submit=_submit,
            harvest=_harvest,
            event_fd=adapter.get_store_event_fd(),
            duration_sec=warmup_sec,
            timeout=_STORE_TIMEOUT_SEC,
            success_for=_success,
            log=log,
        )
        _require_drained_warmup("Store", outstanding)

    result = BenchResult(
        operation="Store",
        in_flight=in_flight,
        num_keys=num_keys,
        data_size_bytes=data_size,
        mode=BenchMode.SUSTAINED,
    )
    log(f"[Store] Sustained window for {duration_sec:.1f}s at {in_flight} in flight...")
    run_sustained_window(
        result,
        submit=_submit,
        harvest=_harvest,
        event_fd=adapter.get_store_event_fd(),
        duration_sec=duration_sec,
        timeout=_STORE_TIMEOUT_SEC,
        success_for=_success,
        log=log,
        first_submit_index=next_index,
    )
    _log_sustained_summary(result, log)
    return result


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def bench_lookup(
    adapter,
    in_flight: int,
    num_keys: int,
    rounds: int,
    keys_for_round: KeyProvider,
    log: LogFn,
    expected_max_hit_rate: float = 0.0,
    expected_hit_count: int = 0,
) -> BenchResult:
    """Benchmark ``submit_lookup_and_lock_task`` in rounds mode."""
    result = BenchResult(
        operation="Lookup",
        in_flight=in_flight,
        num_keys=num_keys,
        data_size_bytes=0,  # lookup transfers no payload
        expected_max_hit_rate=expected_max_hit_rate,
        expected_hit_count=expected_hit_count,
    )

    log(
        "bench_lookup uses a placeholder MemoryLayoutDesc; this may need a "
        "real layout for layout-sensitive adapters"
    )

    for r in range(rounds):
        keys_batches = keys_for_round(r)
        assert len(keys_batches) == in_flight

        t0 = time.perf_counter()
        task_ids: list[int] = []
        submitted_at: dict[int, float] = {}
        for i in range(in_flight):
            issued = time.perf_counter()
            task_id = adapter.submit_lookup_and_lock_task(
                keys_batches[i], _PLACEHOLDER_LAYOUT_DESC
            )
            task_ids.append(task_id)
            submitted_at[task_id] = issued

        results, observed_at = _wait_lookup_finished(
            adapter, task_ids, _LOOKUP_TIMEOUT_SEC
        )
        t1 = time.perf_counter()
        elapsed = t1 - t0
        timed_out = len(results) < len(task_ids)

        result.round_latency_counts.append(
            _record_round_latencies(result, task_ids, observed_at, submitted_at)
        )

        total_found = sum(_bitmap_count(results.get(tid)) for tid in task_ids)

        if timed_out:
            log(
                f"  [Lookup] Round {r + 1}: TIMEOUT "
                f"({len(results)}/{len(task_ids)} tasks completed, "
                f"found={total_found}/{in_flight * num_keys})"
            )
            result.round_starts.append(t0)
            result.round_durations.append(float("inf"))
            result.success_counts.append(total_found)
            result.timed_out = True
            continue

        result.round_starts.append(t0)
        result.round_durations.append(elapsed)
        result.success_counts.append(total_found)
        log(
            f"  [Lookup] Round {r + 1}: {elapsed * 1000:.2f} ms, "
            f"found={total_found}/{in_flight * num_keys}"
        )

    return result


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------


def bench_load(
    adapter,
    in_flight: int,
    num_keys: int,
    data_size: int,
    rounds: int,
    keys_for_round: KeyProvider,
    objs_for_round: ObjProvider,
    log: LogFn,
) -> BenchResult:
    """Benchmark ``submit_load_task`` in rounds mode."""
    result = BenchResult(
        operation="Load",
        in_flight=in_flight,
        num_keys=num_keys,
        data_size_bytes=data_size,
    )

    for r in range(rounds):
        keys_batches = keys_for_round(r)
        obj_batches = objs_for_round(r)
        assert len(keys_batches) == in_flight
        assert len(obj_batches) == in_flight

        # Reset all load buffers before each round to ensure fresh reads.
        for objs in obj_batches:
            for obj in objs:
                obj.raw_data.zero_()

        t0 = time.perf_counter()
        task_ids: list[int] = []
        submitted_at: dict[int, float] = {}
        for i in range(in_flight):
            issued = time.perf_counter()
            task_id = adapter.submit_load_task(keys_batches[i], obj_batches[i])
            task_ids.append(task_id)
            submitted_at[task_id] = issued

        results, observed_at = _wait_load_finished(adapter, task_ids, _LOAD_TIMEOUT_SEC)
        t1 = time.perf_counter()
        elapsed = t1 - t0
        timed_out = len(results) < len(task_ids)

        result.round_latency_counts.append(
            _record_round_latencies(result, task_ids, observed_at, submitted_at)
        )

        total_loaded = sum(_bitmap_count(results.get(tid)) for tid in task_ids)

        if timed_out:
            log(
                f"  [Load] Round {r + 1}: TIMEOUT "
                f"({len(results)}/{len(task_ids)} tasks completed, "
                f"loaded={total_loaded}/{in_flight * num_keys})"
            )
            result.round_starts.append(t0)
            result.round_durations.append(float("inf"))
            result.success_counts.append(total_loaded)
            result.timed_out = True
            continue

        result.round_starts.append(t0)
        result.round_durations.append(elapsed)
        result.success_counts.append(total_loaded)
        log(
            f"  [Load] Round {r + 1}: {elapsed * 1000:.2f} ms, "
            f"loaded={total_loaded}/{in_flight * num_keys}"
        )

    return result


def bench_load_sustained(
    adapter,
    in_flight: int,
    num_keys: int,
    data_size: int,
    duration_sec: float,
    warmup_sec: float,
    keys_for_submit: SubmitKeyProvider,
    objs_for_slot: SlotObjProvider,
    log: LogFn,
) -> BenchResult:
    """Benchmark ``submit_load_task`` in sustained-window mode.

    Load buffers are **not** zeroed between submits. Rounds mode zeroes
    them so a silent no-op load cannot masquerade as success, but in a
    sliding window there is no safe moment to do so, and the per-submit
    success bitmap already distinguishes a miss from a hit. Use rounds
    mode with ``--no-skip-verify`` for byte-level integrity checking.

    Args:
        adapter: L2 adapter under test.
        in_flight: Outstanding submits held for the whole window.
        num_keys: Keys per submit.
        data_size: Payload bytes per key.
        duration_sec: Measured window length in seconds.
        warmup_sec: Discarded window run first; skipped when <= 0.
        keys_for_submit: Keys for a given submit index. Must map into
            keys that were previously stored, or every load misses.
        objs_for_slot: Load buffers for a given window slot.
        log: Progress logger.

    Returns:
        A :class:`BenchResult` in :attr:`BenchMode.SUSTAINED`.
    """

    def _submit(submit_index: int, slot: int) -> int:
        return adapter.submit_load_task(
            keys_for_submit(submit_index), objs_for_slot(slot)
        )

    def _harvest(pending: set[int]) -> dict[int, Bitmap]:
        out: dict[int, Bitmap] = {}
        for task_id in pending:
            bitmap = adapter.query_load_result(task_id)
            if bitmap is not None:
                out[task_id] = bitmap
        return out

    next_index = 0
    if warmup_sec > 0:
        log(f"[Load] Sustained warmup for {warmup_sec:.1f}s (discarded)...")
        warmup = BenchResult(
            operation="Load",
            in_flight=in_flight,
            num_keys=num_keys,
            data_size_bytes=data_size,
            mode=BenchMode.SUSTAINED,
        )
        next_index, outstanding = run_sustained_window(
            warmup,
            submit=_submit,
            harvest=_harvest,
            event_fd=adapter.get_load_event_fd(),
            duration_sec=warmup_sec,
            timeout=_LOAD_TIMEOUT_SEC,
            success_for=_bitmap_count,
            log=log,
        )
        _require_drained_warmup("Load", outstanding)

    result = BenchResult(
        operation="Load",
        in_flight=in_flight,
        num_keys=num_keys,
        data_size_bytes=data_size,
        mode=BenchMode.SUSTAINED,
    )
    log(f"[Load] Sustained window for {duration_sec:.1f}s at {in_flight} in flight...")
    run_sustained_window(
        result,
        submit=_submit,
        harvest=_harvest,
        event_fd=adapter.get_load_event_fd(),
        duration_sec=duration_sec,
        timeout=_LOAD_TIMEOUT_SEC,
        success_for=_bitmap_count,
        log=log,
        first_submit_index=next_index,
    )
    _log_sustained_summary(result, log)
    return result


def _log_sustained_summary(result: BenchResult, log: LogFn) -> None:
    """Log the one-line outcome of a sustained window.

    Quotes the successful-bytes rate, not the requested rate: a window
    where keys missed would otherwise advertise the request rate as
    throughput on the very line an operator reads first.
    """
    rate = f"{result.success_throughput_mbps:.1f} MB/s"
    if result.total_success != result.total_keys:
        rate += f" ({result.aggregate_throughput_mbps:.1f} MB/s requested)"
    log(
        f"  [{result.operation}] {result.completed_submits} submits in "
        f"{result.sustained_window_sec:.2f}s "
        f"(drain tail {result.sustained_drain_sec:.2f}s), "
        f"{result.total_success}/{result.total_keys} keys, {rate}"
    )
