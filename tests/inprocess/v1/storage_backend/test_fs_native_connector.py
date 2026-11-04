# SPDX-License-Identifier: Apache-2.0
"""Tests for the native C++ filesystem connector."""

# Standard
from pathlib import Path
from typing import Any, Iterator
import array
import ctypes
import hashlib
import os
import select
import shutil
import struct
import subprocess
import sys
import textwrap
import time

# Third Party
import pytest

# First Party
from lmcache.v1.distributed.api import ObjectKey
from lmcache.v1.distributed.key_codec import (
    encode_fs_filename,
    encode_storage_key,
    encode_tags,
)


def _import_fs_client() -> type:
    try:
        # First Party
        from lmcache.lmcache_fs import LMCacheFSClient
    except ImportError as exc:
        pytest.skip(f"native FS extension is not available: {exc}")
    return LMCacheFSClient


def _buffer_addr(buf: memoryview) -> int:
    return ctypes.addressof(ctypes.c_char.from_buffer(buf))


def _aligned_memoryview(
    size: int,
    alignment: int,
) -> tuple[bytearray, memoryview]:
    raw = bytearray(size + alignment)
    raw_addr = ctypes.addressof(ctypes.c_char.from_buffer(raw))
    offset = (-raw_addr) % alignment
    view = memoryview(raw)[offset : offset + size]
    assert _buffer_addr(view) % alignment == 0
    return raw, view


def _misaligned_memoryview(
    size: int,
    alignment: int,
) -> tuple[bytearray, memoryview]:
    raw = bytearray(size + alignment + 1)
    for offset in range(1, alignment + 1):
        view = memoryview(raw)[offset : offset + size]
        if _buffer_addr(view) % alignment != 0:
            return raw, view
    raise AssertionError("failed to create misaligned memoryview")


def _fill(view: memoryview) -> None:
    view[:] = bytes(i % 251 for i in range(len(view)))


def _wait_for_completion(
    client: Any,
    future_id: int,
    timeout: float = 5.0,
) -> tuple[int, bool, str, list[bool] | None]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for completion in client.drain_completions():
            if completion[0] == future_id:
                return completion

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        select.select([client.event_fd()], [], [], min(remaining, 0.1))

    raise TimeoutError(f"timed out waiting for future {future_id}")


def _submit_and_wait(
    client: Any,
    method_name: str,
    key: str,
    view: memoryview,
) -> tuple[int, bool, str, list[bool] | None]:
    future_id = getattr(client, method_name)([key], [view])
    return _wait_for_completion(client, future_id)


def test_odirect_read_does_not_split_for_read_ahead(tmp_path) -> None:
    """O_DIRECT reads should ignore read_ahead_size and use one aligned read."""
    if not hasattr(os, "O_DIRECT"):
        pytest.skip("O_DIRECT is not available on this platform")

    LMCacheFSClient = _import_fs_client()
    block_size = os.statvfs(tmp_path).f_bsize
    if block_size <= 0:
        pytest.skip("filesystem block size is unavailable")

    size = block_size * 2
    key = "test_model@00000000@0@0123456789abcdef"
    _source_raw, source = _aligned_memoryview(size, block_size)
    _dest_raw, dest = _aligned_memoryview(size, block_size)
    _fill(source)

    client = LMCacheFSClient(str(tmp_path), 1, "", True, 1)
    try:
        store = _submit_and_wait(client, "submit_batch_set", key, source)
        if not store[1]:
            pytest.skip(f"O_DIRECT is unsupported on this filesystem: {store[2]}")

        load = _submit_and_wait(client, "submit_batch_get", key, dest)
        assert load[1], load[2]
        assert bytes(dest) == bytes(source)
    finally:
        client.close()


def test_odirect_fails_for_misaligned_buffer(tmp_path) -> None:
    """Misaligned buffers should fail instead of hiding O_DIRECT misuse."""
    LMCacheFSClient = _import_fs_client()
    block_size = os.statvfs(tmp_path).f_bsize
    if block_size <= 0:
        pytest.skip("filesystem block size is unavailable")

    size = block_size * 2
    key = "test_model@00000000@0@fedcba9876543210"
    _source_raw, source = _misaligned_memoryview(size, block_size)
    _fill(source)

    client = LMCacheFSClient(str(tmp_path), 1, "", True, 1)
    try:
        store = _submit_and_wait(client, "submit_batch_set", key, source)
        assert not store[1]
        assert "O_DIRECT buffer address is not aligned" in store[2]
    finally:
        client.close()


def _write_corpus(
    client: Any,
    keys: list[str],
    views: list[memoryview],
) -> None:
    """Store every key with the connector, failing the test on error."""
    future_id = client.submit_batch_set(keys, views)
    completion = _wait_for_completion(client, future_id)
    assert completion[1], completion[2]


def _aligned_pairs(
    count: int,
    size: int,
    block_size: int,
) -> tuple[list[memoryview], list[memoryview], list[bytearray]]:
    """Build `count` distinctly filled source/destination pairs.

    Args:
        count: Number of pairs to build.
        size: Bytes in each buffer.
        block_size: Alignment of every buffer's start address, for O_DIRECT.

    Returns:
        Sources, destinations, and the bytearrays that own both.
    """
    sources: list[memoryview] = []
    dests: list[memoryview] = []
    keep: list[bytearray] = []
    for index in range(count):
        source_raw, source = _aligned_memoryview(size, block_size)
        dest_raw, dest = _aligned_memoryview(size, block_size)
        keep.extend((source_raw, dest_raw))
        source[:] = bytes((i + index) % 251 for i in range(size))
        sources.append(source)
        dests.append(dest)
    return sources, dests, keep


def _has_io_uring() -> bool:
    """True when the extension carries the io_uring read engine."""
    _import_fs_client()
    # First Party
    from lmcache import lmcache_fs

    return bool(lmcache_fs.has_io_uring())


def _has_fault_injection() -> bool:
    """True when the extension was built with LMCACHE_FS_FAULT_INJECTION."""
    _import_fs_client()
    # First Party
    from lmcache import lmcache_fs

    return bool(lmcache_fs.has_fault_injection())


# The io_uring engine needs num_workers x io_depth + 1024 descriptors under
# the soft RLIMIT_NOFILE, and many hosts default the soft limit to 1024.
_RING_TEST_NOFILE = 65536


@pytest.fixture
def nofile_for_rings() -> Iterator[None]:
    """Raise the soft RLIMIT_NOFILE (up to the hard limit) for io_uring
    clients, as serving processes such as vLLM do, and restore it after."""
    try:
        # Standard
        import resource
    except ImportError:
        yield
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = _RING_TEST_NOFILE if hard == resource.RLIM_INFINITY else hard
    want = min(want, _RING_TEST_NOFILE)
    if soft != resource.RLIM_INFINITY and soft < want:
        resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


@pytest.fixture(params=["pool", "io_uring"])
def engine(request: pytest.FixtureRequest, nofile_for_rings: None) -> str:
    """Each read engine; io_uring is skipped when it is compiled out."""
    if request.param == "io_uring" and not _has_io_uring():
        pytest.skip("lmcache_fs was built without the io_uring engine")
    return request.param


def _expected_engine(engine: str, num_read_workers: int) -> str:
    """The engine a client reports for a forced engine and pool size."""
    if engine == "io_uring":
        return "io_uring"
    return "pool" if num_read_workers > 0 else "legacy"


@pytest.mark.parametrize(
    "num_read_workers,read_max_bytes_in_flight",
    [
        (0, 0),  # legacy path: one blocking read per object on the worker
        (1, 0),  # a pool of one, so every read still queues behind the last
        (4, 0),  # several reads of one batch in flight at once
        (8, 1),  # budget below one object: it must still be read
        (8, 1 << 17),  # a few objects in flight at a 4 KiB block size
        (8, 1 << 30),  # larger than the whole batch: never waits
    ],
)
def test_reads_return_identical_bytes(
    tmp_path: Path,
    engine: str,
    num_read_workers: int,
    read_max_bytes_in_flight: int,
) -> None:
    """Neither the engine nor the budget may change the bytes returned.

    A budget below one object size must still take that object.
    """
    LMCacheFSClient = _import_fs_client()
    block_size = os.statvfs(tmp_path).f_bsize
    if block_size <= 0:
        pytest.skip("filesystem block size is unavailable")

    # An odd number of blocks, so an object is never a round power of two.
    size = block_size * 7
    keys = [f"test_model@00000000@0@{i:016x}" for i in range(6)]
    sources, dests, keep_alive = _aligned_pairs(len(keys), size, block_size)

    writer = LMCacheFSClient(str(tmp_path), 2, "", True, 0)
    try:
        _write_corpus(writer, keys, sources)
    finally:
        writer.close()

    reader = LMCacheFSClient(
        str(tmp_path),
        2,
        "",
        True,
        0,
        num_read_workers,
        read_max_bytes_in_flight,
        read_engine=engine,
    )
    try:
        assert reader.read_engine() == _expected_engine(engine, num_read_workers)
        if read_max_bytes_in_flight:
            assert reader.read_budget_bytes() == read_max_bytes_in_flight
        future_id = reader.submit_batch_get(keys, dests)
        completion = _wait_for_completion(reader, future_id)
        assert completion[1], completion[2]
        per_key = completion[3]
        assert per_key is not None
        assert list(per_key) == [True] * len(keys)
        for index, dest in enumerate(dests):
            assert bytes(dest) == bytes(sources[index]), f"object {index} differs"
    finally:
        reader.close()


def _read_batches(
    reader: Any,
    keys: list[str],
    dests: list[memoryview],
    batch_size: int,
    timeout: float = 30.0,
) -> list[tuple[int, bool, str, list[bool] | None]]:
    """Submit every batch at once, then wait for all of their completions.

    Args:
        reader: An open LMCacheFSClient.
        keys: Keys to read, in batch order.
        dests: One destination per key.
        batch_size: Keys per submitted batch; the last may be shorter.
        timeout: Seconds to wait for every completion.

    Returns:
        The completions, in submission order.

    Raises:
        TimeoutError: If a batch does not complete in time.
    """
    future_ids = [
        reader.submit_batch_get(
            keys[start : start + batch_size], dests[start : start + batch_size]
        )
        for start in range(0, len(keys), batch_size)
    ]
    completions: dict[int, tuple[int, bool, str, list[bool] | None]] = {}
    deadline = time.monotonic() + timeout
    while len(completions) < len(future_ids):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"{len(future_ids) - len(completions)} batches did not complete"
            )
        for completion in reader.drain_completions():
            completions[completion[0]] = completion
        select.select([reader.event_fd()], [], [], min(remaining, 0.1))
    return [completions[future_id] for future_id in future_ids]


@pytest.mark.parametrize("io_depth", [1, 2, 64])
def test_uring_round_trip_batch_sizes(
    tmp_path: Path, io_depth: int, nofile_for_rings: None
) -> None:
    """Batches smaller than, equal to and three times the ring depth, with
    mixed object sizes, return every byte through the ring."""
    if not _has_io_uring():
        pytest.skip("lmcache_fs was built without the io_uring engine")
    LMCacheFSClient = _import_fs_client()
    block_size = os.statvfs(tmp_path).f_bsize
    sizes = [4 << 10, 64 << 10, 256 << 10, 1 << 20]
    counts = [1, io_depth, 3 * io_depth]
    total = sum(counts)
    keys = [f"test_model@00000000@0@{i:016x}" for i in range(total)]
    sources: list[memoryview] = []
    dests: list[memoryview] = []
    keep: list[bytearray] = []
    for index in range(total):
        size = sizes[index % len(sizes)]
        source_raw, source = _aligned_memoryview(size, block_size)
        dest_raw, dest = _aligned_memoryview(size, block_size)
        keep.extend((source_raw, dest_raw))
        source[:] = (bytes([index % 251]) * 64 + os.urandom(64)) * (size // 128)
        sources.append(source)
        dests.append(dest)

    writer = LMCacheFSClient(str(tmp_path), 2, "", True, 0)
    try:
        _write_corpus(writer, keys, sources)
    finally:
        writer.close()

    reader = LMCacheFSClient(
        str(tmp_path), 1, "", True, 0, read_engine="io_uring", io_depth=io_depth
    )
    try:
        assert reader.read_engine() == "io_uring"
        assert reader.io_depth() == io_depth
        start = 0
        for count in counts:
            done = _read_batches(
                reader, keys[start : start + count], dests[start : start + count], count
            )
            assert done[0][1], done[0][2]
            assert list(done[0][3] or []) == [True] * count
            start += count
        for index, dest in enumerate(dests):
            assert bytes(dest) == bytes(sources[index]), f"object {index} differs"
    finally:
        reader.close()


def test_budget_many_batches_progress(tmp_path: Path, engine: str) -> None:
    """Sixteen concurrent 56-object batches against a budget of four objects
    all finish, and bytes in flight never exceed the budget."""
    LMCacheFSClient = _import_fs_client()
    size = 64 << 10
    budget = 256 << 10
    batches = 16
    batch_size = 56
    count = batches * batch_size
    keys = [f"test_model@00000000@0@{i:016x}" for i in range(count)]
    sources, dests, _keep_alive = _aligned_pairs(count, size, 4096)
    writer = LMCacheFSClient(str(tmp_path), 4)
    try:
        _write_corpus(writer, keys, sources)
    finally:
        writer.close()

    reader = LMCacheFSClient(
        str(tmp_path), 8, "", False, 0, 16, budget, read_engine=engine
    )
    try:
        assert reader.read_engine() == engine
        assert reader.read_budget_bytes() == budget
        for done in _read_batches(reader, keys, dests, batch_size, timeout=60.0):
            assert done[1], done[2]
            assert list(done[3] or []) == [True] * batch_size
        assert 0 < reader.read_peak_bytes() <= budget
        for index, dest in enumerate(dests):
            assert bytes(dest) == bytes(sources[index]), f"object {index} differs"
    finally:
        reader.close()


def _fill_large(view: memoryview, seed: int) -> None:
    """Fill a large buffer quickly with bytes that differ at every MiB, so a
    misplaced chunk is caught, without a Python loop per byte."""
    chunk = os.urandom(1 << 20)
    step = 1 << 20
    for offset in range(0, len(view), step):
        end = min(offset + step, len(view))
        view[offset:end] = chunk[: end - offset]
        struct.pack_into("<QQ", view, offset, seed, offset)


def _digest(view: memoryview) -> bytes:
    return hashlib.sha256(view).digest()


def test_mixed_path_batch_round_trip(tmp_path: Path, engine: str) -> None:
    """One batch mixing an O_DIRECT read, a buffered read of an unaligned
    length and a 1.5 GiB read (the synchronous fallback under io_uring)
    returns every key correctly."""
    large = 3 << 29  # 1.5 GiB
    if shutil.disk_usage(tmp_path).free < large + (2 << 30):
        pytest.skip("needs about 3.5 GiB free under the test directory")
    LMCacheFSClient = _import_fs_client()
    block_size = os.statvfs(tmp_path).f_bsize
    sizes = [block_size * 5, block_size * 3 + 100, large]
    keys = [f"test_model@00000000@0@{i:016x}" for i in range(len(sizes))]
    sources: list[memoryview] = []
    dests: list[memoryview] = []
    keep: list[bytearray] = []
    for index, size in enumerate(sizes):
        source_raw, source = _aligned_memoryview(size, block_size)
        dest_raw, dest = _aligned_memoryview(size, block_size)
        keep.extend((source_raw, dest_raw))
        _fill_large(source, index + 1)
        sources.append(source)
        dests.append(dest)

    writer = LMCacheFSClient(str(tmp_path), 3, "", True, 0)
    try:
        future_id = writer.submit_batch_set(keys, sources)
        stored = _wait_for_completion(writer, future_id, timeout=120.0)
        assert stored[1], stored[2]
    finally:
        writer.close()

    reader = LMCacheFSClient(str(tmp_path), 2, "", True, 0, 4, read_engine=engine)
    try:
        assert reader.read_engine() == engine
        future_id = reader.submit_batch_get(keys, dests)
        done = _wait_for_completion(reader, future_id, timeout=120.0)
        assert done[1], done[2]
        assert list(done[3] or []) == [True] * len(keys)
        for index, dest in enumerate(dests):
            assert _digest(dest) == _digest(sources[index]), f"object {index} differs"
    finally:
        reader.close()


def test_missing_key_fails_only_that_key(tmp_path: Path, engine: str) -> None:
    """A missing object fails alone, and the same worker's next batch loads."""
    LMCacheFSClient = _import_fs_client()
    size = 4 << 10
    present = "test_model@00000000@0@aaaaaaaaaaaaaaaa"
    absent = "test_model@00000000@0@bbbbbbbbbbbbbbbb"
    sources, dests, _keep_alive = _aligned_pairs(6, size, 4096)

    writer = LMCacheFSClient(str(tmp_path), 1)
    try:
        _write_corpus(writer, [present], [sources[0]])
    finally:
        writer.close()

    # One worker, so both batches run on the same ring.
    reader = LMCacheFSClient(str(tmp_path), 1, "", False, 0, 4, read_engine=engine)
    try:
        first = _read_batches(reader, [present, absent, present], dests[:3], 3)[0]
        assert list(first[3] or []) == [True, False, True]
        second = _read_batches(reader, [present] * 3, dests[3:], 3)[0]
        assert second[1], second[2]
        assert list(second[3] or []) == [True, True, True]
        for dest in (dests[0], dests[2], *dests[3:]):
            assert bytes(dest) == bytes(sources[0])
    finally:
        reader.close()


def test_alignment_both_misaligned_reads_buffered(tmp_path: Path, engine: str) -> None:
    """A read whose address and length are both unaligned is buffered and
    succeeds; an aligned length at an unaligned address fails that key
    where O_DIRECT exists."""
    LMCacheFSClient = _import_fs_client()
    block_size = os.statvfs(tmp_path).f_bsize
    odd = block_size * 2 + 17
    even = block_size * 2
    keys = [
        "test_model@00000000@0@cccccccccccccccc",
        "test_model@00000000@0@dddddddddddddddd",
    ]
    _odd_src_raw, odd_src = _misaligned_memoryview(odd, block_size)
    _even_src_raw, even_src = _aligned_memoryview(even, block_size)
    _odd_dst_raw, odd_dst = _misaligned_memoryview(odd, block_size)
    _even_dst_raw, even_dst = _misaligned_memoryview(even, block_size)
    _fill(odd_src)
    _fill(even_src)

    writer = LMCacheFSClient(str(tmp_path), 1, "", True, 0)
    try:
        _write_corpus(writer, keys, [odd_src, even_src])
    finally:
        writer.close()

    reader = LMCacheFSClient(str(tmp_path), 1, "", True, 0, 4, read_engine=engine)
    try:
        done = _read_batches(reader, keys, [odd_dst, even_dst], 2)[0]
        expect_even = not hasattr(os, "O_DIRECT")
        assert list(done[3] or []) == [True, expect_even]
        assert bytes(odd_dst) == bytes(odd_src)
    finally:
        reader.close()


def test_auto_without_ring_falls_back_to_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nofile_for_rings: None
) -> None:
    """auto uses the read pool when no ring can be created.

    Without the engine compiled in this always holds.  With it, the ring
    failure is injected, which needs a build with LMCACHE_FS_FAULT_INJECTION=1
    (``LMCACHE_FS_FAULT_INJECTION=1 bash scripts/ipu-poc/build_lmcache_fs.sh .``).
    """
    LMCacheFSClient = _import_fs_client()
    if _has_io_uring():
        if not _has_fault_injection():
            pytest.skip("needs a LMCACHE_FS_FAULT_INJECTION=1 build of lmcache_fs")
        monkeypatch.setenv("LMCACHE_FS_FAULT", "queue_init_error")
    reader = LMCacheFSClient(str(tmp_path), 2, "", False, 0, 4, read_engine="auto")
    try:
        assert reader.read_engine() == "pool"
        assert reader.io_depth() == 0
        assert reader.read_budget_bytes() == 1536 << 20
    finally:
        reader.close()


def test_forced_io_uring_without_ring_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, nofile_for_rings: None
) -> None:
    """io_uring fails construction when no ring can be created.

    With the engine compiled in, the failure is injected and needs a
    LMCACHE_FS_FAULT_INJECTION=1 build, as above.
    """
    LMCacheFSClient = _import_fs_client()
    if _has_io_uring():
        if not _has_fault_injection():
            pytest.skip("needs a LMCACHE_FS_FAULT_INJECTION=1 build of lmcache_fs")
        monkeypatch.setenv("LMCACHE_FS_FAULT", "queue_init_error")
    with pytest.raises(RuntimeError, match="io_uring"):
        LMCacheFSClient(str(tmp_path), 2, "", False, 0, 4, read_engine="io_uring")


@pytest.mark.parametrize(
    "read_engine,io_depth", [("bogus", 64), ("pool", 0), ("pool", 4097)]
)
def test_bad_engine_arguments_are_rejected(
    tmp_path: Path, read_engine: str, io_depth: int
) -> None:
    """An unknown engine or an io_depth outside [1, 4096] is a ValueError."""
    LMCacheFSClient = _import_fs_client()
    with pytest.raises(ValueError, match="read_engine|io_depth"):
        LMCacheFSClient(str(tmp_path), 1, read_engine=read_engine, io_depth=io_depth)


def _run_child(code: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run `code` in a fresh interpreter that can import lmcache.

    Resource limits are process-wide, so tests that lower them run here.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    env["FS_TEST_DIR"] = str(tmp_path)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_nofile_limit_lowers_depth_or_raises(tmp_path: Path) -> None:
    """io_uring and auto raise the soft RLIMIT_NOFILE toward num_workers x
    io_depth + 1024, never past the hard limit and never down, and pool
    leaves it alone.  When even the raised limit is short, auto lowers the
    depth (or uses the pool below a depth of 16) and a forced io_uring
    fails construction naming the numbers."""
    if not _has_io_uring():
        pytest.skip("lmcache_fs was built without the io_uring engine")
    child = _run_child(
        """
        import os, resource
        from lmcache.lmcache_fs import LMCacheFSClient
        base = os.environ["FS_TEST_DIR"]
        N = resource.RLIMIT_NOFILE
        def soft():
            return resource.getrlimit(N)[0]
        def show(tag, workers, engine, depth, *pool):
            c = LMCacheFSClient(base, workers, "", False, 0, *pool,
                                read_engine=engine, io_depth=depth)
            print(tag, c.read_engine(), c.io_depth(), soft())
            c.close()
        _, hard = resource.getrlimit(N)
        # Hard limit above the need: the soft limit is raised to the need.
        resource.setrlimit(N, (2048, hard))
        show("raise", 4, "auto", 512)
        # Already enough: never lowered.
        show("keep", 1, "auto", 64)
        # pool never touches it.
        resource.setrlimit(N, (1100, hard))
        show("pool", 64, "pool", 64, 4)
        # Hard limit below the need: raised to the hard limit, then fitted.
        resource.setrlimit(N, (1100, 2048))
        show("fit", 4, "auto", 4096)
        try:
            LMCacheFSClient(base, 4, read_engine="io_uring", io_depth=4096)
            print("forced ok")
        except RuntimeError as e:
            print("forced raised", e)
        # Fitted depth 16 is the auto floor; 12 is below it.
        show("floor16", 64, "auto", 64)
        show("floor12", 80, "auto", 64, 4)
        # A forced io_uring takes any depth that fits, here 12 at 80 x 12.
        show("forced12", 80, "io_uring", 12)
        """,
        tmp_path,
    )
    assert child.returncode == 0, child.stderr
    lines = child.stdout.splitlines()
    assert lines[0] == "raise io_uring 512 3072"
    assert lines[1] == "keep io_uring 64 3072"
    assert lines[2] == "pool pool 0 1100"
    assert lines[3] == "fit io_uring 256 2048"
    assert lines[4].startswith("forced raised")
    assert "RLIMIT_NOFILE" in lines[4] and "2048" in lines[4] and "4096" in lines[4]
    assert lines[5] == "floor16 io_uring 16 2048"
    assert lines[6] == "floor12 pool 0 2048"
    assert lines[7] == "forced12 io_uring 12 2048"
    assert "raised the RLIMIT_NOFILE soft limit from 2048 to 3072" in child.stderr
    assert "below the auto floor of 16" in child.stderr


def test_emfile_at_open_throttles_instead_of_failing(tmp_path: Path) -> None:
    """When opens hit EMFILE while reads are in flight, the ring stops
    refilling until one completes rather than failing keys."""
    if not _has_io_uring() or not os.path.isdir("/proc/self/fd"):
        pytest.skip("needs the io_uring engine and /proc/self/fd")
    child = _run_child(
        """
        import ctypes, os, resource, select, time
        from lmcache.lmcache_fs import LMCacheFSClient
        base = os.environ["FS_TEST_DIR"]
        n, size = 64, 64 << 10
        srcs = [bytearray(os.urandom(size)) for _ in range(n)]
        dsts = [bytearray(size) for _ in range(n)]
        keys = [f"test_model@00000000@0@{i:016x}" for i in range(n)]
        def wait(c, fid):
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                for comp in c.drain_completions():
                    if comp[0] == fid:
                        return comp
                select.select([c.event_fd()], [], [], 0.1)
            raise TimeoutError
        w = LMCacheFSClient(base, 2)
        assert wait(w, w.submit_batch_set(keys, [memoryview(s) for s in srcs]))[1]
        w.close()
        _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, 65536), hard))
        c = LMCacheFSClient(base, 1, read_engine="io_uring", io_depth=64)
        top = max(int(f) for f in os.listdir("/proc/self/fd"))
        resource.setrlimit(resource.RLIMIT_NOFILE, (top + 4, hard))
        comp = wait(c, c.submit_batch_get(keys, [memoryview(d) for d in dsts]))
        print(comp[1], sum(comp[3]), all(d == s for d, s in zip(dsts, srcs)))
        c.close()
        """,
        tmp_path,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.split() == ["True", "64", "True"]


def test_pooled_read_tolerates_one_missing_object(tmp_path: Path) -> None:
    """A missing object fails alone; its batch-mates still load."""
    LMCacheFSClient = _import_fs_client()
    block_size = os.statvfs(tmp_path).f_bsize
    if block_size <= 0:
        pytest.skip("filesystem block size is unavailable")

    size = block_size * 4
    present = "test_model@00000000@0@aaaaaaaaaaaaaaaa"
    absent = "test_model@00000000@0@bbbbbbbbbbbbbbbb"
    _source_raw, source = _aligned_memoryview(size, block_size)
    _first_raw, first = _aligned_memoryview(size, block_size)
    _second_raw, second = _aligned_memoryview(size, block_size)
    # A third buffer, so no two keys of the batch share a destination:
    # concurrent reads into one buffer would be a data race even when the
    # bytes happen to match.
    _third_raw, third = _aligned_memoryview(size, block_size)
    _fill(source)

    writer = LMCacheFSClient(str(tmp_path), 1, "", True, 0)
    try:
        _write_corpus(writer, [present], [source])
    finally:
        writer.close()

    reader = LMCacheFSClient(str(tmp_path), 2, "", True, 0, 4)
    try:
        future_id = reader.submit_batch_get(
            [present, absent, present], [first, second, third]
        )
        completion = _wait_for_completion(reader, future_id)
        per_key = completion[3]
        assert per_key is not None
        assert list(per_key) == [True, False, True]
        assert bytes(first) == bytes(source)
        assert bytes(third) == bytes(source)
    finally:
        reader.close()


def test_the_default_budget_is_used_when_none_is_configured(tmp_path: Path) -> None:
    """Zero selects the documented default rather than "no budget"."""
    LMCacheFSClient = _import_fs_client()
    reader = LMCacheFSClient(str(tmp_path), 2, "", False, 0, 8)
    try:
        assert reader.read_budget_bytes() == 1536 << 20
    finally:
        reader.close()

    # The legacy path has no budget at all.
    legacy = LMCacheFSClient(str(tmp_path), 2)
    try:
        assert legacy.read_budget_bytes() == 0
    finally:
        legacy.close()

    # A budget configured without num_read_workers throttles nothing, so it
    # must not be reported as if it did.
    unused = LMCacheFSClient(str(tmp_path), 2, "", False, 0, 0, 1 << 20)
    try:
        assert unused.read_budget_bytes() == 0
    finally:
        unused.close()


def _pooled_round_trip(
    tmp_path: Path,
    reader: Any,
    size: int,
    count: int,
    batch_size: int,
) -> None:
    """Store `count` objects of `size` bytes, then read them back through
    `reader` in concurrent batches of `batch_size`, checking every byte.

    Args:
        tmp_path: Directory the reader serves.
        reader: An LMCacheFSClient with a read pool, rooted at tmp_path.
        size: Bytes per object.
        count: Number of objects; a multiple of batch_size.
        batch_size: Objects per submitted batch; all batches are in flight
            together.
    """
    LMCacheFSClient = _import_fs_client()
    keys = [f"test_model@00000000@0@{i:016x}" for i in range(count)]
    sources, dests, _keep_alive = _aligned_pairs(count, size, 4096)
    writer = LMCacheFSClient(str(tmp_path), 2)
    try:
        _write_corpus(writer, keys, sources)
    finally:
        writer.close()

    future_ids = [
        reader.submit_batch_get(
            keys[start : start + batch_size], dests[start : start + batch_size]
        )
        for start in range(0, count, batch_size)
    ]
    # One drain can return several batches' completions, so collect them all
    # rather than waiting for each future in turn.
    completions: dict[int, tuple[int, bool, str, list[bool] | None]] = {}
    deadline = time.monotonic() + 10.0
    while len(completions) < len(future_ids) and time.monotonic() < deadline:
        for completion in reader.drain_completions():
            completions[completion[0]] = completion
        select.select([reader.event_fd()], [], [], 0.1)
    assert sorted(completions) == sorted(future_ids)
    for completion in completions.values():
        assert completion[1], completion[2]
        assert completion[3] is not None
        assert list(completion[3]) == [True] * batch_size
    for index, dest in enumerate(dests):
        assert bytes(dest) == bytes(sources[index]), f"object {index} differs"


def test_pool_peak_bytes_never_exceed_small_budget(tmp_path: Path) -> None:
    """Concurrent batches share one budget: bytes in flight stay within it."""
    LMCacheFSClient = _import_fs_client()
    budget = 1 << 20
    reader = LMCacheFSClient(str(tmp_path), 4, "", False, 0, 64, budget)
    try:
        assert reader.read_peak_bytes() == 0
        # 32 objects of 256 KiB, read as 4 concurrent batches
        _pooled_round_trip(tmp_path, reader, 256 << 10, 32, 8)
        assert 0 < reader.read_peak_bytes() <= budget
    finally:
        reader.close()


def test_oversized_read_admitted_under_two_read_rule(tmp_path: Path) -> None:
    """Objects larger than the budget still load, at most two at a time."""
    LMCacheFSClient = _import_fs_client()
    reader = LMCacheFSClient(str(tmp_path), 2, "", False, 0, 8, 64 << 10)
    try:
        # Three 1 MiB objects against a 64 KiB budget: each is admitted only
        # while fewer than two reads are in flight.
        _pooled_round_trip(tmp_path, reader, 1 << 20, 3, 3)
        assert reader.read_peak_bytes() == 2 << 20
    finally:
        reader.close()

    legacy = LMCacheFSClient(str(tmp_path), 2)
    try:
        assert legacy.read_peak_bytes() == 0
    finally:
        legacy.close()


def test_negative_num_read_workers_is_rejected(tmp_path: Path) -> None:
    """A negative num_read_workers is a configuration error, not a silent fallback."""
    LMCacheFSClient = _import_fs_client()
    with pytest.raises(RuntimeError, match="num_read_workers must be >= 0"):
        LMCacheFSClient(str(tmp_path), 1, "", False, 0, -1)


def test_sharded_layout_round_trip(tmp_path) -> None:
    """Sharded objects live one level down and read back through the client."""
    LMCacheFSClient = _import_fs_client()
    key = "test_model@00000000@0@00112233445566778899"
    size = 8192
    _source_raw, source = _aligned_memoryview(size, 4096)
    _dest_raw, dest = _aligned_memoryview(size, 4096)
    _fill(source)

    client = LMCacheFSClient(str(tmp_path), 2, num_shard_dirs=16)
    try:
        shards = sorted(p.name for p in tmp_path.iterdir())
        assert shards == [f"{i:x}" for i in range(16)]

        assert _submit_and_wait(client, "submit_batch_set", key, source)[1]
        stored = [p for p in tmp_path.rglob("*.data")]
        assert len(stored) == 1
        assert stored[0].parent.parent == tmp_path

        load = _submit_and_wait(client, "submit_batch_get", key, dest)
        assert load[1], load[2]
        assert bytes(dest) == bytes(source)

        exists = _wait_for_completion(client, client.submit_batch_exists([key]))
        assert exists[3] == [True]

        deleted = _wait_for_completion(client, client.submit_batch_delete([key]))
        assert deleted[1], deleted[2]
        assert not list(tmp_path.rglob("*.data"))
    finally:
        client.close()


def test_shard_count_must_match_to_find_objects(tmp_path) -> None:
    """A flat reader cannot see objects a sharded writer stored."""
    LMCacheFSClient = _import_fs_client()
    key = "test_model@00000000@0@aabbccddeeff00112233"
    _source_raw, source = _aligned_memoryview(4096, 4096)
    _fill(source)

    writer = LMCacheFSClient(str(tmp_path), 1, num_shard_dirs=4)
    try:
        assert _submit_and_wait(writer, "submit_batch_set", key, source)[1]
    finally:
        writer.close()

    flat = LMCacheFSClient(str(tmp_path), 1, num_shard_dirs=0)
    try:
        exists = _wait_for_completion(flat, flat.submit_batch_exists([key]))
        assert exists[3] == [False]
    finally:
        flat.close()


def _store(client: Any, key: str, view: memoryview) -> None:
    assert _submit_and_wait(client, "submit_batch_set", key, view)[1]


def _pooled(directory: Path) -> list[Path]:
    """Pooled files parked in *directory* (they sit beside the live objects)."""
    return sorted(directory.glob(".pool-*"))


def _data_path(base: Path, key_hash: str) -> Path:
    return next(p for p in base.rglob("*.data") if key_hash in p.name)


def _shard_name(key: str, num_shard_dirs: int) -> str:
    """Mirror the connector layout: FNV-1a of the on-disk filename."""
    model, rank, group, chunk = key.split("@")
    filename = f"{model}@0x{rank}@{group}@{chunk}.data"
    h = 14695981039346656037
    for byte in filename.encode():
        h = ((h ^ byte) * 1099511628211) % (1 << 64)
    width = max(1, len(f"{num_shard_dirs - 1:x}"))
    return f"{h % num_shard_dirs:0{width}x}"


def test_recycling_reuses_a_deleted_file_after_quarantine(tmp_path) -> None:
    """A delete parks the file; a same-shard store after 1 s overwrites it."""
    LMCacheFSClient = _import_fs_client()
    size = 8192
    shards = 2
    keys = [f"test_model@00000000@0@{i:016x}" for i in range(1, 64)]
    first = keys[0]
    same = [k for k in keys[1:] if _shard_name(k, shards) == _shard_name(first, shards)]
    early, late = same[0], same[1]
    client = LMCacheFSClient(
        str(tmp_path), 2, num_shard_dirs=shards, recycle_max_files=100
    )
    try:
        _raw, view = _aligned_memoryview(size, 4096)
        _fill(view)
        _store(client, first, view)
        stored = _data_path(tmp_path, first.rsplit("@", 1)[1])
        assert stored.parent.name == _shard_name(first, shards)
        inode = stored.stat().st_ino
        deleted = _wait_for_completion(client, client.submit_batch_delete([first]))
        assert deleted[1], deleted[2]
        # Parked in the object's own shard, so delete and reuse renames stay
        # within one directory.
        assert [p.stat().st_ino for p in _pooled(stored.parent)] == [inode]

        # Inside the quarantine a store in the same shard gets a fresh file.
        _store(client, early, view)
        assert _data_path(tmp_path, early.rsplit("@", 1)[1]).stat().st_ino != inode

        time.sleep(1.2)
        _raw2, fresh = _aligned_memoryview(size, 4096)
        fresh[:] = b"\x5a" * size
        _store(client, late, fresh)
        assert _data_path(tmp_path, late.rsplit("@", 1)[1]).stat().st_ino == inode
        assert not _pooled(stored.parent)

        _raw3, dest = _aligned_memoryview(size, 4096)
        assert _submit_and_wait(client, "submit_batch_get", late, dest)[1]
        assert bytes(dest) == b"\x5a" * size
    finally:
        client.close()


def test_recycling_adopts_pool_files_left_by_an_earlier_client(tmp_path) -> None:
    """Pool files survive a restart and are reused without a new quarantine."""
    LMCacheFSClient = _import_fs_client()
    size = 8192
    key = "test_model@00000000@0@" + "a" * 16
    _raw, view = _aligned_memoryview(size, 4096)
    _fill(view)

    client = LMCacheFSClient(str(tmp_path), 1, recycle_max_files=10)
    try:
        _store(client, key, view)
        inode = _data_path(tmp_path, "a" * 16).stat().st_ino
        assert _wait_for_completion(client, client.submit_batch_delete([key]))[1]
    finally:
        client.close()

    client = LMCacheFSClient(str(tmp_path), 1, recycle_max_files=10)
    try:
        other = "test_model@00000000@0@" + "b" * 16
        _store(client, other, view)
        assert _data_path(tmp_path, "b" * 16).stat().st_ino == inode
        assert not _pooled(tmp_path)
    finally:
        client.close()


def test_dedicated_store_and_delete_lanes_serve_requests(tmp_path) -> None:
    """Store/delete lanes behave like the shared pool from the caller side."""
    LMCacheFSClient = _import_fs_client()
    size = 8192
    client = LMCacheFSClient(
        str(tmp_path), 2, num_shard_dirs=4, store_workers=2, delete_workers=2
    )
    try:
        keys = [f"test_model@00000000@0@{i:016x}" for i in (1, 2)]
        _raw, view = _aligned_memoryview(size, 4096)
        _fill(view)
        for key in keys:
            _store(client, key, view)
        _raw2, dest = _aligned_memoryview(size, 4096)
        assert _submit_and_wait(client, "submit_batch_get", keys[0], dest)[1]
        assert bytes(dest) == bytes(view)
        assert _wait_for_completion(client, client.submit_batch_delete([keys[0]]))[1]
        exists = _wait_for_completion(client, client.submit_batch_exists(keys))
        assert exists[3] == [False, True]
    finally:
        client.close()


def test_negative_lane_worker_count_is_rejected(tmp_path) -> None:
    LMCacheFSClient = _import_fs_client()
    with pytest.raises(ValueError, match="store_workers"):
        LMCacheFSClient(str(tmp_path), 1, store_workers=-1)


def test_recycling_unlinks_once_the_pool_is_full(tmp_path: Path) -> None:
    """Deletes past recycle_max_files fall back to unlinking."""
    LMCacheFSClient = _import_fs_client()
    _raw, view = _aligned_memoryview(4096, 4096)
    _fill(view)
    keys = [f"test_model@00000000@0@{i:016x}" for i in (1, 2, 3)]
    client = LMCacheFSClient(str(tmp_path), 1, recycle_max_files=2)
    try:
        for key in keys:
            _store(client, key, view)
        deleted = _wait_for_completion(client, client.submit_batch_delete(keys))
        assert deleted[1], deleted[2]
        assert len(_pooled(tmp_path)) == 2
        assert not list(tmp_path.glob("*.data"))
    finally:
        client.close()


def test_recycling_drops_a_pooled_file_of_another_size(tmp_path: Path) -> None:
    """A store never reuses a pooled file whose size differs from the object."""
    LMCacheFSClient = _import_fs_client()
    _big_raw, big = _aligned_memoryview(8192, 4096)
    _small_raw, small = _aligned_memoryview(4096, 4096)
    _fill(big)
    small[:] = b"\x3c" * 4096
    first = "test_model@00000000@0@" + "1" * 16
    second = "test_model@00000000@0@" + "2" * 16
    client = LMCacheFSClient(str(tmp_path), 1, recycle_max_files=10)
    try:
        _store(client, first, big)
        inode = _data_path(tmp_path, "1" * 16).stat().st_ino
        assert _wait_for_completion(client, client.submit_batch_delete([first]))[1]
        time.sleep(1.2)

        _store(client, second, small)
        stored = _data_path(tmp_path, "2" * 16)
        assert stored.stat().st_ino != inode
        assert stored.stat().st_size == 4096
        assert not _pooled(tmp_path)

        _dest_raw, dest = _aligned_memoryview(4096, 4096)
        assert _submit_and_wait(client, "submit_batch_get", second, dest)[1]
        assert bytes(dest) == b"\x3c" * 4096
    finally:
        client.close()


@pytest.mark.skipif(not hasattr(os, "O_DIRECT"), reason="needs O_DIRECT")
def test_misaligned_store_keeps_the_pooled_file_reusable(tmp_path: Path) -> None:
    """A rejected O_DIRECT store leaves an eligible pooled file in the pool."""
    LMCacheFSClient = _import_fs_client()
    block_size = os.statvfs(tmp_path).f_bsize
    size = block_size * 2
    _raw, view = _aligned_memoryview(size, block_size)
    _fill(view)
    _bad_raw, bad = _misaligned_memoryview(size, block_size)
    first = "test_model@00000000@0@" + "a" * 16
    client = LMCacheFSClient(str(tmp_path), 1, use_odirect=True, recycle_max_files=10)
    try:
        _store(client, first, view)
        inode = _data_path(tmp_path, "a" * 16).stat().st_ino
        assert _wait_for_completion(client, client.submit_batch_delete([first]))[1]
        time.sleep(1.2)

        rejected = _submit_and_wait(
            client, "submit_batch_set", "test_model@00000000@0@" + "b" * 16, bad
        )
        assert not rejected[1]
        assert [p.stat().st_ino for p in _pooled(tmp_path)] == [inode]

        _store(client, "test_model@00000000@0@" + "c" * 16, view)
        assert _data_path(tmp_path, "c" * 16).stat().st_ino == inode
    finally:
        client.close()


# Read paths for the delayed-read test: (read_engine, num_read_workers,
# read_ahead_size).  A read_ahead_size below the object size sends io_uring
# reads through its synchronous fallback.
_RECYCLE_READ_PATHS = {
    "legacy": ("pool", 0, 0),
    "pool": ("pool", 2, 0),
    "io_uring": ("io_uring", 0, 0),
    "io_uring_fallback": ("io_uring", 0, 4096),
}


@pytest.mark.parametrize("read_path", sorted(_RECYCLE_READ_PATHS))
def test_delayed_read_never_returns_new_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    nofile_for_rings: None,
    read_path: str,
) -> None:
    """A load that opened K1 and is still reading after K1 is deleted and the
    quarantine has passed returns K1's bytes or fails, never those of K2,
    a same-size object stored meanwhile."""
    LMCacheFSClient = _import_fs_client()
    if not _has_fault_injection():
        pytest.skip("needs a LMCACHE_FS_FAULT_INJECTION=1 build")
    read_engine, num_read_workers, read_ahead_size = _RECYCLE_READ_PATHS[read_path]
    if read_engine == "io_uring" and not _has_io_uring():
        pytest.skip("lmcache_fs was built without the io_uring engine")
    # Every read sleeps this long between opening its file and reading it.
    monkeypatch.setenv("LMCACHE_FS_FAULT_DELAY_MS", "1500")
    size = 8192
    first = "test_model@00000000@0@" + "1" * 16
    second = "test_model@00000000@0@" + "2" * 16
    old = b"\x11" * size
    new = b"\x22" * size
    client = LMCacheFSClient(
        str(tmp_path),
        4,
        read_ahead_size=read_ahead_size,
        num_read_workers=num_read_workers,
        recycle_max_files=8,
        read_engine=read_engine,
    )
    try:
        _raw, view = _aligned_memoryview(size, 4096)
        view[:] = old
        _store(client, first, view)
        _dest_raw, dest = _aligned_memoryview(size, 4096)
        load = client.submit_batch_get([first], [dest])
        time.sleep(0.1)  # the load has opened K1 and is sleeping
        assert _wait_for_completion(client, client.submit_batch_delete([first]))[1]
        time.sleep(1.2)  # past the quarantine, before the load reads
        _raw2, fresh = _aligned_memoryview(size, 4096)
        fresh[:] = new
        _store(client, second, fresh)
        # The load still holds K1's file, so K2 got a new one; if the load had
        # already finished, K2 would have taken the parked file.
        assert len(_pooled(tmp_path)) == 1, "the parked file was reused"

        loaded = _wait_for_completion(client, load)
        if loaded[1]:
            assert bytes(dest) == old, "the load returned bytes other than K1's"
        # K2 is intact whichever file it went to.
        _dest2_raw, dest2 = _aligned_memoryview(size, 4096)
        assert _submit_and_wait(client, "submit_batch_get", second, dest2)[1]
        assert bytes(dest2) == new
    finally:
        client.close()


def test_second_recycler_runs_without_recycling(
    tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """A second recycling client on a base_path the first holds logs it,
    runs with recycling disabled and never reuses the first's parked files."""
    LMCacheFSClient = _import_fs_client()
    size = 8192
    _raw, view = _aligned_memoryview(size, 4096)
    _fill(view)
    keys = [f"test_model@00000000@0@{i:016x}" for i in (1, 2, 3)]
    first = LMCacheFSClient(str(tmp_path), 1, recycle_max_files=8)
    try:
        assert first.recycling_enabled()
        _store(first, keys[0], view)
        assert _wait_for_completion(first, first.submit_batch_delete([keys[0]]))[1]
        parked = _pooled(tmp_path)
        assert len(parked) == 1

        second = LMCacheFSClient(str(tmp_path), 1, recycle_max_files=8)
        try:
            assert not second.recycling_enabled()
            assert "recycling disabled for" in capfd.readouterr().err
            time.sleep(1.2)  # past the quarantine of the first's file
            _store(second, keys[1], view)
            # Its stores leave the first's parked file alone, and its deletes
            # unlink instead of parking.
            assert _pooled(tmp_path) == parked
            assert _wait_for_completion(second, second.submit_batch_delete([keys[1]]))[
                1
            ]
            assert _pooled(tmp_path) == parked
            assert not list(tmp_path.glob("*.data"))
        finally:
            second.close()

        # The first still recycles its own file.
        _store(first, keys[2], view)
        assert not _pooled(tmp_path)
    finally:
        first.close()


_RAW_SIZE = 4096
_MAX_U64 = 2**64 - 1


def _raw_keys(
    salt: str, tags: dict[str, str], rank: int, group: int, hash_len: int
) -> list[ObjectKey]:
    return [
        ObjectKey(
            (bytes([0x80 + i, 0xAB, 0xFF - i, 0xCD]) * hash_len)[:hash_len],
            "test_model",
            rank,
            group,
            salt,
            tags=tags,
        )
        for i in range(3)
    ]


def _raw_call(
    client: Any,
    method: str,
    keys: list[ObjectKey],
    views: list[memoryview],
    **overrides: Any,
) -> int:
    """Submit ``keys`` through a raw method, letting tests override arguments."""
    ident = keys[0].identity
    args: dict[str, Any] = {
        "model_name": ident.model_name,
        "cache_salt": ident.cache_salt,
        "tags_token": encode_tags(ident.tags),
        "kv_ranks": array.array("Q", [k.kv_rank for k in keys]),
        "object_group_ids": array.array("Q", [k.object_group_id for k in keys]),
        "chunk_hashes": b"".join(k.chunk_hash for k in keys),
        "hash_len": len(ident.chunk_hash),
        "buf_ptrs": array.array("Q", [_buffer_addr(v) for v in views]),
        "buf_lens": array.array("Q", [len(v) for v in views]),
    }
    args.update(overrides)
    return getattr(client, method)(*args.values())


def _raw_views(n: int = 3) -> tuple[list[bytearray], list[memoryview]]:
    raws = [bytearray(_RAW_SIZE) for _ in range(n)]
    views = [memoryview(r) for r in raws]
    for i, v in enumerate(views):
        v[:] = bytes([i + 7]) * _RAW_SIZE
    return raws, views


@pytest.mark.parametrize("hash_len", [8, 16, 32])
@pytest.mark.parametrize("group", [0, _MAX_U64])
@pytest.mark.parametrize("rank", [0, _MAX_U64])
@pytest.mark.parametrize("tags", [{}, {"lora": "a1"}])
@pytest.mark.parametrize("salt", ["", "tenant"])
def test_raw_store_writes_canonical_filenames(
    tmp_path: Path,
    salt: str,
    tags: dict[str, str],
    rank: int,
    group: int,
    hash_len: int,
) -> None:
    """The raw path names files exactly as the Python key codec does."""
    LMCacheFSClient = _import_fs_client()
    keys = _raw_keys(salt, tags, rank, group, hash_len)
    _raws, views = _raw_views()
    client = LMCacheFSClient(str(tmp_path), 2, num_shard_dirs=0)
    try:
        done = _wait_for_completion(
            client, _raw_call(client, "submit_batch_set_raw", keys, views)
        )
        assert done[1], done[2]
        on_disk = sorted(p.name for p in tmp_path.glob("*.data"))
        assert on_disk == sorted(encode_fs_filename(k) for k in keys)
    finally:
        client.close()


def test_raw_store_string_load_round_trip(tmp_path: Path) -> None:
    """Objects stored through the raw path load through the string path."""
    LMCacheFSClient = _import_fs_client()
    keys = _raw_keys("tenant", {"lora": "a1"}, 3, 5, 16)
    _raws, views = _raw_views()
    client = LMCacheFSClient(str(tmp_path), 2, num_shard_dirs=0)
    try:
        done = _wait_for_completion(
            client, _raw_call(client, "submit_batch_set_raw", keys, views)
        )
        assert done[1], done[2]
        dest = [bytearray(_RAW_SIZE) for _ in keys]
        fid = client.submit_batch_get(
            [encode_storage_key(k) for k in keys], [memoryview(d) for d in dest]
        )
        done = _wait_for_completion(client, fid)
        assert done[1], done[2]
        assert [bytes(d) for d in dest] == [bytes(v) for v in views]
    finally:
        client.close()


def test_string_store_raw_load_round_trip(tmp_path: Path) -> None:
    """Objects stored through the string path load through the raw path."""
    LMCacheFSClient = _import_fs_client()
    keys = _raw_keys("", {"lora": "a1"}, 3, 5, 16)
    _raws, views = _raw_views()
    client = LMCacheFSClient(str(tmp_path), 2, num_shard_dirs=0)
    try:
        fid = client.submit_batch_set([encode_storage_key(k) for k in keys], views)
        done = _wait_for_completion(client, fid)
        assert done[1], done[2]
        dest = [bytearray(_RAW_SIZE) for _ in keys]
        dviews = [memoryview(d) for d in dest]
        done = _wait_for_completion(
            client, _raw_call(client, "submit_batch_get_raw", keys, dviews)
        )
        assert done[1], done[2]
        assert [bytes(d) for d in dest] == [bytes(v) for v in views]
    finally:
        client.close()


def _bad_overrides(bad: str, keys: list[ObjectKey]) -> dict[str, Any]:
    ranks = [k.kv_rank for k in keys]
    be = (ctypes.c_uint64.__ctype_be__ * len(keys))(*ranks)
    two_d = memoryview(array.array("Q", ranks * 2)).cast("B").cast("Q", (2, len(keys)))
    strided = memoryview(array.array("Q", ranks * 2))[::2]
    return {
        "len_mismatch": {"object_group_ids": array.array("Q", [0, 0])},
        "fmt_d": {"kv_ranks": array.array("d", [float(r) for r in ranks])},
        "fmt_q": {"kv_ranks": array.array("q", ranks)},
        "itemsize_4": {"kv_ranks": array.array("I", ranks)},
        "fmt_be": {"kv_ranks": memoryview(be)},
        "two_d": {"kv_ranks": two_d},
        "noncontig": {"kv_ranks": strided},
        "empty": {
            "kv_ranks": array.array("Q"),
            "object_group_ids": array.array("Q"),
            "buf_ptrs": array.array("Q"),
            "buf_lens": array.array("Q"),
            "chunk_hashes": b"",
        },
        "hash_len_0": {"hash_len": 0},
        "hash_len_mismatch": {"hash_len": 7},
        "at_in_model": {"model_name": "a@b"},
        "at_in_salt": {"cache_salt": "a@b"},
        "at_in_token": {"tags_token": "x1-a@b"},
    }[bad]


@pytest.mark.parametrize(
    "bad",
    [
        "len_mismatch",
        "fmt_d",
        "fmt_q",
        "itemsize_4",
        "fmt_be",
        "two_d",
        "noncontig",
        "empty",
        "hash_len_0",
        "hash_len_mismatch",
        "at_in_model",
        "at_in_salt",
        "at_in_token",
    ],
)
@pytest.mark.parametrize("method", ["submit_batch_get_raw", "submit_batch_set_raw"])
def test_raw_submit_rejects_bad_arguments(
    tmp_path: Path, method: str, bad: str
) -> None:
    """Malformed raw arguments raise ValueError and submit nothing."""
    LMCacheFSClient = _import_fs_client()
    keys = _raw_keys("", {}, 1, 0, 16)
    _raws, views = _raw_views()
    client = LMCacheFSClient(str(tmp_path), 2, num_shard_dirs=0)
    try:
        with pytest.raises(ValueError):
            _raw_call(client, method, keys, views, **_bad_overrides(bad, keys))
        time.sleep(0.05)
        assert client.drain_completions() == []
        assert not list(tmp_path.rglob("*.data"))
    finally:
        client.close()


def test_raw_submit_accepts_unaligned_arrays(tmp_path: Path) -> None:
    """Array buffers at odd addresses are copied, not dereferenced in place."""
    LMCacheFSClient = _import_fs_client()
    keys = _raw_keys("", {}, 1, 2, 16)
    _raws, views = _raw_views()

    def unaligned(values: list[int]) -> memoryview:
        raw = bytearray(8 * len(values) + 1)
        raw[1:] = array.array("Q", values).tobytes()
        view = memoryview(raw)[1:].cast("Q")
        assert ctypes.addressof(ctypes.c_char.from_buffer(raw)) % 8 == 0
        return view

    client = LMCacheFSClient(str(tmp_path), 2, num_shard_dirs=0)
    try:
        # raw+1 is 8-byte unaligned whenever the bytearray start is aligned.
        done = _wait_for_completion(
            client,
            _raw_call(
                client,
                "submit_batch_set_raw",
                keys,
                views,
                kv_ranks=unaligned([k.kv_rank for k in keys]),
                object_group_ids=unaligned([k.object_group_id for k in keys]),
                buf_ptrs=unaligned([_buffer_addr(v) for v in views]),
                buf_lens=unaligned([len(v) for v in views]),
            ),
        )
        assert done[1], done[2]
        on_disk = sorted(p.name for p in tmp_path.glob("*.data"))
        assert on_disk == sorted(encode_fs_filename(k) for k in keys)
    finally:
        client.close()


def test_module_reports_io_uring_support() -> None:
    _import_fs_client()
    # First Party
    from lmcache import lmcache_fs

    assert isinstance(lmcache_fs.has_io_uring(), bool)
    if sys.platform == "linux" and os.environ.get("LMCACHE_EXPECT_IO_URING") == "1":
        assert lmcache_fs.has_io_uring()
