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
from lmcache.lmcache_native import Bitmap
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
from lmcache.v1.distributed.internal_api import L2StoreResult
from lmcache.v1.memory_management import MemoryObj

# Local
from .data import verify_round_trip, wait_eventfd, wait_eventfds
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

# Receives a result as soon as it exists, BEFORE the runner starts
# mutating it. Lets a caller observe progress live -- the metrics
# endpoint reads the same object on every scrape. Sustained mode passes
# the discarded warmup result to a SEPARATE hook, so an observer can
# distinguish it instead of having it folded into the measured series.
ResultHook = Callable[[BenchResult], None]


def _discard_result_hook(result: BenchResult) -> None:
    """Default :data:`ResultHook`: observe nothing."""


# TODO: bench passes a placeholder layout_desc; a real layout may be
# required here in the future (e.g. when benchmarking the P2P adapter).
_PLACEHOLDER_LAYOUT_DESC = MemoryLayoutDesc(shapes=[], dtypes=[])

_STORE_TIMEOUT_SEC = 120.0
_LOOKUP_TIMEOUT_SEC = 60.0
_LOAD_TIMEOUT_SEC = 120.0
_MIXED_WRITE_VERIFY_SAMPLES = 3


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
            result.record_latency(observed_at[task_id] - submitted_at[task_id])
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


class StoreFreshnessUnknownError(RuntimeError):
    """The existence probe could not answer, so freshness is unknown.

    Raised instead of assuming the namespace is empty. A timed-out lookup
    carries no information: the keys may or may not be there. Treating it
    as "absent" would let the benchmark write into an already-populated
    namespace, which is the exact condition
    :class:`StoreNamespaceNotEmptyError` exists to prevent.
    """


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
        Number of *keys* the adapter reported present. Zero only when the
        adapter positively reported every key absent.

    Raises:
        StoreFreshnessUnknownError: The lookup did not complete within
            *timeout*, so the answer is unknown rather than zero.
    """
    if not keys:
        return 0
    task_id = adapter.submit_lookup_and_lock_task(keys, _PLACEHOLDER_LAYOUT_DESC)
    results, _observed = _wait_lookup_finished(adapter, [task_id], timeout)
    bitmap = results.get(task_id)
    if bitmap is None:
        raise StoreFreshnessUnknownError(
            f"the existence probe for {len(keys)} keys did not complete "
            f"within {timeout:g}s, so whether this namespace is already "
            f"populated is unknown. A store run into an occupied namespace "
            f"measures existence checks while counting full payload bytes, "
            f"so this is not safe to assume away. Investigate why the "
            f"adapter's lookup stalled, then re-run."
        )
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
        StoreFreshnessUnknownError: The probe timed out, leaving freshness
            unknown. This gate fails closed -- an unanswered probe is not
            evidence of an empty namespace.
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
    window. Records one success and one observed latency per completion,
    then updates ``result.completed_submits``, ``result.sustained_window_sec``
    and ``result.sustained_drain_sec``. Sustained results retain exact
    running totals while bounding the retained latency sample.

    Also stamps ``result.phase_started_at`` (wall clock, before the first
    submit) and ``result.phase_ended_at`` (wall clock, once the window and
    its drain are fully done) so a live observer can tell how long this
    phase has been running, or that it has finished, without knowing its
    configured warmup/duration.

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

    result.phase_started_at = time.time()
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
            result.record_latency(now - submitted_at)
            result.record_success(success_for(payload))
            result.completed_submits += 1
            free_slots.append(slot)

        if now < deadline:
            while free_slots:
                slot = free_slots.pop()
                submitted_at = time.perf_counter()
                pending[submit(submit_index, slot)] = (submitted_at, slot)
                submit_index += 1
        elif refill_end == 0.0:
            refill_end = deadline

    result.sustained_window_sec = last_observed - t_start
    result.sustained_drain_sec = (
        max(0.0, last_observed - refill_end) if refill_end > 0.0 else 0.0
    )
    result.phase_ended_at = time.time()
    return submit_index, len(pending)


def run_sustained_mixed_window(
    adapter,
    load_result: BenchResult,
    store_result: BenchResult,
    read_write_ratio: tuple[int, int],
    load_keys_for_submit: SubmitKeyProvider,
    store_keys_for_submit: SubmitKeyProvider,
    load_objs_for_slot: SlotObjProvider,
    store_objs_for_slot: SlotObjProvider,
    duration_sec: float,
    timeout: float,
    log: LogFn,
    verify_write_samples: bool = True,
) -> bool:
    """Run a sustained read/write window under one global in-flight limit.

    The deterministic issue pattern begins with a store, then contains the
    requested number of loads and stores per cycle. Store and load task-id
    spaces are tracked separately because adapters are allowed to reuse task
    ids across operation types.

    Args:
        adapter: L2 adapter receiving store and load submits.
        load_result: Result to populate for successful load payloads.
        store_result: Result to populate for successful store payloads.
        read_write_ratio: Requested ``(read, write)`` payload ratio.
        load_keys_for_submit: Read-corpus keys for each load submit.
        store_keys_for_submit: Monotonic write-prefix keys for each store submit.
        load_objs_for_slot: Load buffers for a reusable global window slot.
        store_objs_for_slot: Store buffers for a reusable global window slot.
        duration_sec: Measured refill-window duration in seconds.
        timeout: Maximum wait for a completion event in seconds.
        log: Progress logger.
        verify_write_samples: Whether to read back a bounded sample of
            mixed-window stores before accepting the result.

    Returns:
        ``True`` when every completion was successful and the final successful
        payload ratio is within one percent of the requested ratio. A timeout,
        failed store, partial load, or ratio mismatch returns ``False``.

    Raises:
        ValueError: If the duration, in-flight limit, ratio, or result modes
            are invalid.
    """
    read_count, write_count = read_write_ratio
    if duration_sec <= 0:
        raise ValueError("duration_sec must be positive")
    if load_result.in_flight <= 0 or store_result.in_flight <= 0:
        raise ValueError("in_flight must be positive")
    if load_result.in_flight != store_result.in_flight:
        raise ValueError("mixed results must share the same in_flight limit")
    if load_result.mode is not BenchMode.SUSTAINED:
        raise ValueError("load_result requires BenchMode.SUSTAINED")
    if store_result.mode is not BenchMode.SUSTAINED:
        raise ValueError("store_result requires BenchMode.SUSTAINED")
    if read_count <= 0 or write_count <= 0:
        raise ValueError("read_write_ratio values must be positive")

    # Start every cycle with a store. This makes the bootstrap explicit and
    # still produces exactly READ:WRITE operations over every full cycle.
    issue_pattern = ["Store"] + ["Load"] * read_count + ["Store"] * (write_count - 1)
    pattern_index = 0
    next_load_index = 0
    next_store_index = 0
    # (operation, task id) -> (submit timestamp, reusable global slot)
    pending: dict[tuple[str, int], tuple[float, int]] = {}
    free_slots = list(range(load_result.in_flight))
    rejected = False
    # ``fs_native`` reports a successful store when it has accepted a task,
    # not when a later load proves the payload bytes. Keep a few source
    # buffers and their keys for a post-window readback into the distinct
    # load buffer pool.
    write_samples: list[tuple[list[ObjectKey], list[MemoryObj], int]] = []

    def issue(slot: int) -> None:
        """Issue the next deterministic operation into *slot*."""
        nonlocal next_load_index, next_store_index, pattern_index
        operation = issue_pattern[pattern_index % len(issue_pattern)]
        pattern_index += 1
        submitted_at = time.perf_counter()
        if operation == "Load":
            task_id = adapter.submit_load_task(
                load_keys_for_submit(next_load_index),
                load_objs_for_slot(slot),
            )
            next_load_index += 1
        else:
            keys = store_keys_for_submit(next_store_index)
            objects = store_objs_for_slot(slot)
            if len(write_samples) < _MIXED_WRITE_VERIFY_SAMPLES:
                write_samples.append((keys, objects, slot))
            task_id = adapter.submit_store_task(
                keys,
                objects,
            )
            next_store_index += 1
        pending[(operation, task_id)] = (submitted_at, slot)

    t_start = time.perf_counter()
    deadline = t_start + duration_sec
    for _ in range(load_result.in_flight):
        issue(free_slots.pop())

    event_fds = {
        "Load": adapter.get_load_event_fd(),
        "Store": adapter.get_store_event_fd(),
    }
    last_observed = t_start
    refill_end = 0.0

    while pending:
        ready_operations = wait_eventfds(event_fds, timeout=timeout)
        if not ready_operations:
            log(
                "  [Mixed] TIMEOUT after "
                f"{timeout:.0f}s with {len(pending)} submits outstanding"
            )
            load_result.timed_out = True
            store_result.timed_out = True
            rejected = True
            break

        completed: dict[tuple[str, int], Any] = {}
        if "Store" in ready_operations:
            for task_id, payload in adapter.pop_completed_store_tasks().items():
                key = ("Store", task_id)
                if key in pending:
                    completed[key] = payload
        if "Load" in ready_operations:
            for operation, task_id in list(pending):
                if operation != "Load":
                    continue
                bitmap = adapter.query_load_result(task_id)
                if bitmap is not None:
                    completed[(operation, task_id)] = bitmap
        if not completed:
            # A foreign completion or spurious event must not replenish a
            # slot this window still owns.
            continue

        now = time.perf_counter()
        last_observed = now
        for (operation, task_id), payload in completed.items():
            submitted_at, slot = pending.pop((operation, task_id))
            result = load_result if operation == "Load" else store_result
            result.record_latency(now - submitted_at)
            result.completed_submits += 1
            free_slots.append(slot)

            if operation == "Load":
                loaded = _bitmap_count(payload)
                result.record_success(loaded)
                if loaded != result.num_keys:
                    log(
                        f"  [Load] FAILED: loaded {loaded}/{result.num_keys} "
                        "keys in a mixed window"
                    )
                    rejected = True
            else:
                stored = _store_success_keys(payload, result.num_keys)
                result.record_success(stored)
                if stored != result.num_keys:
                    log(
                        f"  [Store] FAILED: stored {stored}/{result.num_keys} "
                        "keys in a mixed window"
                    )
                    rejected = True

        if not rejected and now < deadline:
            while free_slots and time.perf_counter() < deadline:
                issue(free_slots.pop())
        elif refill_end == 0.0:
            refill_end = deadline

    if verify_write_samples and not rejected and not pending:
        if not verify_mixed_write_samples(
            adapter,
            write_samples,
            load_objs_for_slot,
            timeout=timeout,
            log=log,
        ):
            rejected = True

    window_sec = last_observed - t_start
    drain_sec = max(0.0, last_observed - refill_end) if refill_end > 0.0 else 0.0
    # The two operation results must use the same denominator before their
    # per-direction goodputs can be summed.
    for result in (load_result, store_result):
        result.sustained_window_sec = window_sec
        result.sustained_drain_sec = drain_sec

    read_bytes = load_result.total_success_bytes
    write_bytes = store_result.total_success_bytes
    achieved_ratio = read_bytes / write_bytes if write_bytes else 0.0
    target_ratio = read_count / write_count
    within_tolerance = (
        write_bytes > 0 and target_ratio * 0.99 <= achieved_ratio <= target_ratio * 1.01
    )
    all_successful = (
        not load_result.timed_out
        and not store_result.timed_out
        and load_result.total_success == load_result.total_keys
        and store_result.total_success == store_result.total_keys
    )
    if not within_tolerance:
        log(
            f"  [Mixed] FAILED: achieved read:write ratio "
            f"{achieved_ratio:.4f}, expected {read_count}:{write_count} "
            "(within 1%)"
        )
    return not rejected and all_successful and within_tolerance


def verify_mixed_write_samples(
    adapter,
    samples: list[tuple[list[ObjectKey], list[MemoryObj], int]],
    load_objs_for_slot: SlotObjProvider,
    timeout: float,
    log: LogFn,
) -> bool:
    """Read back sampled mixed-window stores and compare their payload bytes.

    Args:
        adapter: L2 adapter that received the mixed-window stores.
        samples: ``(keys, expected_objects, slot)`` tuples captured before
            each sampled store was issued.
        load_objs_for_slot: Supplies the independent load buffers for a slot.
        timeout: Maximum seconds to wait for each sample completion.
        log: Progress logger.

    Returns:
        ``True`` only when each sample loads all keys and every loaded buffer
        equals the corresponding source buffer.
    """
    for keys, expected_objects, slot in samples:
        loaded_objects = load_objs_for_slot(slot)
        for loaded in loaded_objects:
            loaded.raw_data.fill_(0xA5)
        task_id = adapter.submit_load_task(keys, loaded_objects)
        if not wait_eventfd(adapter.get_load_event_fd(), timeout=timeout):
            log("  [Mixed Verify] TIMEOUT waiting for write-prefix readback")
            return False
        bitmap = adapter.query_load_result(task_id)
        if bitmap is None or _bitmap_count(bitmap) != len(keys):
            log(
                "  [Mixed Verify] FAILED: write-prefix sample did not "
                f"load all {len(keys)} keys"
            )
            return False
        if not verify_round_trip(keys, expected_objects, loaded_objects, log):
            log("  [Mixed Verify] FAILED: write-prefix payload mismatch")
            return False
    return True


def bench_mixed_sustained(
    adapter,
    in_flight: int,
    num_keys: int,
    data_size: int,
    duration_sec: float,
    read_write_ratio: tuple[int, int],
    load_keys_for_submit: SubmitKeyProvider,
    store_keys_for_submit: SubmitKeyProvider,
    load_objs_for_slot: SlotObjProvider,
    store_objs_for_slot: SlotObjProvider,
    log: LogFn,
    on_result: ResultHook = _discard_result_hook,
    verify_write_samples: bool = True,
) -> tuple[BenchResult, BenchResult, bool]:
    """Benchmark a sustained read/write payload mix.

    The benchmark holds one global ``in_flight`` window across both
    operations. It starts with a store and then follows the requested
    deterministic read/write issue ratio. Loads and stores receive separate
    results but share the same measured wall-clock denominator.

    Args:
        adapter: L2 adapter under test.
        in_flight: Total outstanding submits across both directions.
        num_keys: Keys per submit.
        data_size: Payload bytes per key.
        duration_sec: Measured window length in seconds.
        read_write_ratio: Requested ``(read, write)`` payload ratio.
        load_keys_for_submit: Read-corpus keys for each load submit.
        store_keys_for_submit: Monotonic write-prefix keys for each store.
        load_objs_for_slot: Load buffers for a global window slot.
        store_objs_for_slot: Store buffers for a global window slot.
        log: Progress logger.
        on_result: Receives each measured direction before submissions begin.
        verify_write_samples: Whether to read back a bounded sample of
            successful stores before accepting the result.

    Returns:
        ``(load_result, store_result, accepted)``. ``accepted`` is false on
        a failed store, missed load key, timeout, or achieved-ratio mismatch.
    """
    load_result = BenchResult(
        operation="Load",
        in_flight=in_flight,
        num_keys=num_keys,
        data_size_bytes=data_size,
        mode=BenchMode.SUSTAINED,
    )
    store_result = BenchResult(
        operation="Store",
        in_flight=in_flight,
        num_keys=num_keys,
        data_size_bytes=data_size,
        mode=BenchMode.SUSTAINED,
    )
    on_result(load_result)
    on_result(store_result)

    read_count, write_count = read_write_ratio
    log(
        "[Mixed] Sustained window for "
        f"{duration_sec:.1f}s at {in_flight} total in flight "
        f"(requested read:write {read_count}:{write_count})..."
    )
    accepted = run_sustained_mixed_window(
        adapter=adapter,
        load_result=load_result,
        store_result=store_result,
        read_write_ratio=read_write_ratio,
        load_keys_for_submit=load_keys_for_submit,
        store_keys_for_submit=store_keys_for_submit,
        load_objs_for_slot=load_objs_for_slot,
        store_objs_for_slot=store_objs_for_slot,
        duration_sec=duration_sec,
        timeout=max(_LOAD_TIMEOUT_SEC, _STORE_TIMEOUT_SEC),
        log=log,
        verify_write_samples=verify_write_samples,
    )
    _log_sustained_summary(load_result, log)
    _log_sustained_summary(store_result, log)
    if store_result.total_success_bytes:
        achieved_ratio = (
            load_result.total_success_bytes / store_result.total_success_bytes
        )
        log(
            f"  [Mixed] successful read:write payload ratio "
            f"{achieved_ratio:.4f} "
            "("
            f"{load_result.total_success_bytes} B:"
            f"{store_result.total_success_bytes} B"
            ")"
        )
    return load_result, store_result, accepted


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
    on_result: ResultHook = _discard_result_hook,
) -> BenchResult:
    """Benchmark ``submit_store_task`` in rounds mode.

    For each round, ``in_flight`` independent submits are issued; the
    round duration is the wall-clock time from the first submit until
    every submit of that round has completed.

    ``on_result`` receives the result before the first round runs, so a
    caller can observe it filling in.
    """
    result = BenchResult(
        operation="Store",
        in_flight=in_flight,
        num_keys=num_keys,
        data_size_bytes=data_size,
    )
    on_result(result)

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

        appended = _record_round_latencies(result, task_ids, observed_at, submitted_at)
        result.round_latency_counts.append(appended)
        result.completed_submits += appended

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
            result.timed_out_rounds += 1
            result.record_success(success_keys)
            continue

        result.round_starts.append(t0)
        result.round_durations.append(elapsed)
        result.record_success(success_keys)
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
    on_result: ResultHook = _discard_result_hook,
    on_warmup_result: ResultHook = _discard_result_hook,
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
        on_result: Receives the measured result before its window opens,
            so a caller can observe it live.
        on_warmup_result: Receives the discarded warmup result, if any,
            before its window opens. Separate from ``on_result`` so an
            observer can keep the two apart -- the warmup does real I/O
            that external counters will show.

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
        on_warmup_result(warmup)
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
    # A distinct result from the warmup, published under its own phase
    # label, so the discarded window can never be summed into the
    # measured series.
    on_result(result)
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
    on_result: ResultHook = _discard_result_hook,
) -> BenchResult:
    """Benchmark ``submit_lookup_and_lock_task`` in rounds mode.

    ``on_result`` receives the result before the first round runs, so a
    caller can observe it filling in.
    """
    result = BenchResult(
        operation="Lookup",
        in_flight=in_flight,
        num_keys=num_keys,
        data_size_bytes=0,  # lookup transfers no payload
        expected_max_hit_rate=expected_max_hit_rate,
        expected_hit_count=expected_hit_count,
    )
    on_result(result)

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

        appended = _record_round_latencies(result, task_ids, observed_at, submitted_at)
        result.round_latency_counts.append(appended)
        result.completed_submits += appended

        total_found = sum(_bitmap_count(results.get(tid)) for tid in task_ids)

        if timed_out:
            log(
                f"  [Lookup] Round {r + 1}: TIMEOUT "
                f"({len(results)}/{len(task_ids)} tasks completed, "
                f"found={total_found}/{in_flight * num_keys})"
            )
            result.timed_out_rounds += 1
            result.record_success(total_found)
            continue

        result.round_starts.append(t0)
        result.round_durations.append(elapsed)
        result.record_success(total_found)
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
    on_result: ResultHook = _discard_result_hook,
) -> BenchResult:
    """Benchmark ``submit_load_task`` in rounds mode.

    ``on_result`` receives the result before the first round runs, so a
    caller can observe it filling in.
    """
    result = BenchResult(
        operation="Load",
        in_flight=in_flight,
        num_keys=num_keys,
        data_size_bytes=data_size,
    )
    on_result(result)

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

        appended = _record_round_latencies(result, task_ids, observed_at, submitted_at)
        result.round_latency_counts.append(appended)
        result.completed_submits += appended

        total_loaded = sum(_bitmap_count(results.get(tid)) for tid in task_ids)

        if timed_out:
            log(
                f"  [Load] Round {r + 1}: TIMEOUT "
                f"({len(results)}/{len(task_ids)} tasks completed, "
                f"loaded={total_loaded}/{in_flight * num_keys})"
            )
            result.timed_out_rounds += 1
            result.record_success(total_loaded)
            continue

        result.round_starts.append(t0)
        result.round_durations.append(elapsed)
        result.record_success(total_loaded)
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
    on_result: ResultHook = _discard_result_hook,
    on_warmup_result: ResultHook = _discard_result_hook,
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
        on_result: Receives the measured result before its window opens,
            so a caller can observe it live.
        on_warmup_result: Receives the discarded warmup result, if any,
            before its window opens. Separate from ``on_result`` so an
            observer can keep the two apart -- the warmup does real I/O
            that external counters will show.

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
        on_warmup_result(warmup)
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
    # A distinct result from the warmup, published under its own phase
    # label, so the discarded window can never be summed into the
    # measured series.
    on_result(result)
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
