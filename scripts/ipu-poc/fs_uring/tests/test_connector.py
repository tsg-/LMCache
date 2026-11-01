# SPDX-License-Identifier: Apache-2.0
"""Exercise the real native connector and Linux filesystem."""

# Standard
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
import gc
import importlib
import mmap
import os
import select
import subprocess
import sys
import threading
import time

# Third Party
import pytest


def wait(client: Any, task: int) -> tuple:
    """Collect one task's completion, failing on a lost completion."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        ready = client.drain_completions()
        if ready:
            assert len(ready) == 1
            assert ready[0][0] == task
            return ready[0]
        select.select([client.event_fd()], [], [], 0.05)
    raise AssertionError("native task did not complete")


def key(index: int) -> str:
    """Produce the current four-field ObjectKey wire representation."""
    return f"model/test@00000000@0@{index:032x}"


def filename(index: int) -> str:
    """Return an independently specified existing filesystem filename."""
    return f"model-SEP-test@0x00000000@0@{index:032x}.data"


@pytest.fixture
def client_factory(tmp_path: Path) -> Iterator[Callable[..., Any]]:
    """Keep native clients alive until their queued reads have stopped."""
    if os.environ.get("LMCACHE_ASYNC_TEST_BASELINE") == "1":
        cls = importlib.import_module("lmcache_fs").LMCacheFSClient
    else:
        cls = importlib.import_module("lmcache_fs_uring").LMCacheFSUringClient
    clients = []

    def create(workers: int = 1, depth: int = 32, direct: bool = True) -> Any:
        if os.environ.get("LMCACHE_ASYNC_TEST_BASELINE") == "1":
            client = cls(str(tmp_path), workers, "", direct)
        else:
            client = cls(str(tmp_path), workers, "", direct, depth)
        clients.append(client)
        return client

    yield create
    for client in clients:
        client.close()


def buffers(count: int, size: int = 65536) -> list[mmap.mmap]:
    """Allocate page-aligned anonymous buffers held by the caller."""
    return [mmap.mmap(-1, size) for _ in range(count)]


def test_whole_mixtral_batch_issues_reads_before_waiting(
    tmp_path: Path, client_factory: Callable[..., Any]
) -> None:
    """A depth-one implementation fails even when its returned bytes match."""
    for i in range(56):
        (tmp_path / filename(i)).write_bytes(bytes([i]) * 65536)
    dest = buffers(56)
    client = client_factory()
    try:
        result = wait(
            client,
            client.submit_batch_get(
                [key(i) for i in range(56)], [memoryview(b) for b in dest]
            ),
        )
        assert result[1] and result[3] == [True] * 56, result
        assert all(b[:] == bytes([i]) * 65536 for i, b in enumerate(dest))
        stats = client.io_stats() if hasattr(client, "io_stats") else {}
        assert stats.get("max_worker_pending", 0) >= 32
        assert stats["submitted_reads"] == stats["completed_reads"] == 56
        assert stats["pending_reads"] == 0
    finally:
        client.close()
        for b in dest:
            b.close()


def test_missing_and_short_files_have_per_key_failure(
    tmp_path: Path, client_factory: Callable[..., Any]
) -> None:
    """Successful neighbors survive a missing file and an incomplete read."""
    (tmp_path / filename(0)).write_bytes(b"A" * 65536)
    (tmp_path / filename(2)).write_bytes(b"B" * 4096)
    dest = buffers(4)
    client = client_factory()
    try:
        result = wait(
            client,
            client.submit_batch_get(
                [key(i) for i in range(4)], [memoryview(b) for b in dest]
            ),
        )
        assert result[1], result
        assert result[3] == [True, False, False, False]
        assert dest[0][:] == b"A" * 65536
        assert client.io_stats()["pending_reads"] == 0
    finally:
        client.close()
        for b in dest:
            b.close()


def test_blocking_store_file_compatibility(
    tmp_path: Path, client_factory: Callable[..., Any]
) -> None:
    """The new reader must read files produced by the original connector."""
    original = importlib.import_module("lmcache_fs").LMCacheFSClient(
        str(tmp_path), 1, "", True
    )
    source, dest = buffers(2)
    source[:] = b"S" * 65536
    client = client_factory()
    try:
        assert wait(
            original, original.submit_batch_set([key(0)], [memoryview(source)])
        )[1]
        result = wait(client, client.submit_batch_get([key(0)], [memoryview(dest)]))
        assert result[1] and result[3] == [True]
        assert dest[:] == source[:]
        assert (tmp_path / filename(0)).is_file()
        source[:] = b"N" * 65536
        assert wait(client, client.submit_batch_set([key(1)], [memoryview(source)]))[1]
        assert wait(original, original.submit_batch_get([key(1)], [memoryview(dest)]))[
            3
        ] == [True]
        assert dest[:] == source[:]
    finally:
        original.close()
        client.close()
        source.close()
        dest.close()


def test_repeated_multiworker_batches_release_descriptors(
    tmp_path: Path, client_factory: Callable[..., Any]
) -> None:
    """Catch descriptor leaks, early buffer release and lost batch completions."""
    for i in range(56):
        (tmp_path / filename(i)).write_bytes(bytes([i]) * 65536)
    before = len(os.listdir("/proc/self/fd"))
    client = client_factory(workers=4)
    dest = [buffers(56) for _ in range(16)]
    tasks = [
        client.submit_batch_get(
            [key(i) for i in range(56)], [memoryview(b) for b in batch]
        )
        for batch in dest
    ]
    seen = {}
    deadline = time.monotonic() + 20
    while len(seen) < len(tasks) and time.monotonic() < deadline:
        for result in client.drain_completions():
            assert result[0] not in seen
            seen[result[0]] = result
        select.select([client.event_fd()], [], [], 0.05)
    assert set(seen) == set(tasks)
    assert all(r[1] and r[3] == [True] * 56 for r in seen.values())
    assert all(
        b[:] == bytes([i]) * 65536 for batch in dest for i, b in enumerate(batch)
    )
    client.close()
    for batch in dest:
        for b in batch:
            b.close()
    gc.collect()
    assert len(os.listdir("/proc/self/fd")) <= before


def test_close_drains_reads_before_buffers_are_released(
    tmp_path: Path, client_factory: Callable[..., Any]
) -> None:
    """Closing with queued work must leave no kernel access to freed buffers."""
    for i in range(56):
        (tmp_path / filename(i)).write_bytes(bytes([i]) * 65536)
    client = client_factory(workers=2)
    dest = [buffers(56) for _ in range(32)]
    for batch in dest:
        client.submit_batch_get(
            [key(i) for i in range(56)], [memoryview(b) for b in batch]
        )
    client.close()
    client.close()
    assert client.io_stats()["pending_reads"] == 0
    for batch in dest:
        for b in batch:
            b.close()
    with pytest.raises(RuntimeError, match="closed"):
        client.submit_batch_exists([key(0)])


def test_alignment_failure_is_not_reported_as_success(
    tmp_path: Path, client_factory: Callable[..., Any]
) -> None:
    """Misaligned O_DIRECT buffers must fail without a buffered fallback."""
    (tmp_path / filename(0)).write_bytes(b"A" * 65536)
    raw = bytearray(65537)
    dest = memoryview(raw)[1:]
    client = client_factory()
    result = wait(client, client.submit_batch_get([key(0)], [dest]))
    assert result[3] == [False]


def test_lookup_delete_and_buffered_short_read(
    tmp_path: Path, client_factory: Callable[..., Any]
) -> None:
    """Catch broken non-LOAD operations and EOF handling without O_DIRECT."""
    (tmp_path / filename(0)).write_bytes(b"A" * 100)
    client = client_factory(direct=False)
    dest = bytearray(200)
    assert wait(client, client.submit_batch_exists([key(0), key(1)]))[3] == [
        True,
        False,
    ]
    assert wait(client, client.submit_batch_get([key(0)], [memoryview(dest)]))[3] == [
        False
    ]
    assert wait(client, client.submit_batch_delete([key(0), key(1)]))[3] == [
        True,
        False,
    ]
    assert not (tmp_path / filename(0)).exists()


@pytest.mark.parametrize("kind", ["readonly", "strided", "nonbyte"])
def test_invalid_load_buffers_are_rejected_before_submission(
    tmp_path: Path, client_factory: Callable[..., Any], kind: str
) -> None:
    """Reject views that cannot safely receive byte-oriented native reads."""
    (tmp_path / filename(0)).write_bytes(b"A" * 65536)
    backing = mmap.mmap(-1, 131072)
    view = memoryview(backing)[:65536]
    if kind == "readonly":
        view = view.toreadonly()
    elif kind == "strided":
        view = memoryview(backing)[::2]
    else:
        view = view.cast("I")
    client = client_factory()
    try:
        try:
            task = client.submit_batch_get([key(0)], [view])
        except (RuntimeError, BufferError):
            pass
        else:
            wait(client, task)
            pytest.fail("unsafe memoryview was accepted")
    finally:
        client.close()
        view.release()
        backing.close()


def test_close_with_observed_outstanding_reads(
    tmp_path: Path, client_factory: Callable[..., Any]
) -> None:
    """Observe kernel-issued work before closing and releasing its buffers."""
    size = 8 * 1024 * 1024
    for i in range(16):
        with (tmp_path / filename(i)).open("wb") as f:
            f.truncate(size)
    dest = buffers(16, size)
    client = client_factory()
    client.submit_batch_get([key(i) for i in range(16)], [memoryview(b) for b in dest])
    deadline = time.monotonic() + 5
    observed = False
    while time.monotonic() < deadline:
        if client.io_stats()["pending_reads"] > 0:
            observed = True
            break
    assert observed, "test did not observe any outstanding kernel requests"
    client.close()
    assert client.io_stats()["pending_reads"] == 0
    assert client.io_stats()["submitted_reads"] == client.io_stats()["completed_reads"]
    for memory in dest:
        memory.close()


def test_ring_initialization_failure_is_synchronous_and_leak_free(
    tmp_path: Path,
) -> None:
    """Ring allocation failure must not silently strand background workers."""
    program = """
import os, resource, sys
from lmcache_fs_uring import LMCacheFSUringClient
before = len(os.listdir('/proc/self/fd'))
resource.setrlimit(resource.RLIMIT_NOFILE, (before + 4, before + 4))
try:
    client = LMCacheFSUringClient(sys.argv[1], 16)
except RuntimeError as error:
    assert 'io_uring_queue_init' in str(error), error
else:
    client.close()
    raise AssertionError('construction did not reject unavailable ring fds')
assert len(os.listdir('/proc/self/fd')) == before
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_completion_draining_can_race_with_close(
    client_factory: Callable[..., Any],
) -> None:
    """Catch notifier descriptor access during and after connector teardown."""
    client = client_factory()
    closed = threading.Event()
    errors = []

    def drain() -> None:
        try:
            while not closed.is_set():
                client.drain_completions()
                client.event_fd()
            for _ in range(20):
                assert client.drain_completions() == []
                assert client.event_fd() == -1
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=drain)
    thread.start()
    client.close()
    closed.set()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert not errors
