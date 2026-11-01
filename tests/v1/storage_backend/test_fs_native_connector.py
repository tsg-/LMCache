# SPDX-License-Identifier: Apache-2.0
"""Tests for the native C++ filesystem connector."""

# Standard
from typing import Any
import ctypes
import os
import select
import time

# Third Party
import pytest


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
    key = "test_model@00000000@0123456789abcdef"
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
    key = "test_model@00000000@fedcba9876543210"
    _source_raw, source = _misaligned_memoryview(size, block_size)
    _fill(source)

    client = LMCacheFSClient(str(tmp_path), 1, "", True, 1)
    try:
        store = _submit_and_wait(client, "submit_batch_set", key, source)
        assert not store[1]
        assert "O_DIRECT buffer address is not aligned" in store[2]
    finally:
        client.close()


def test_sharded_layout_round_trip(tmp_path) -> None:
    """Sharded objects live one level down and read back through the client."""
    LMCacheFSClient = _import_fs_client()
    key = "test_model@00000000@00112233445566778899"
    size = 8192
    _source_raw, source = _aligned_memoryview(size, 4096)
    _dest_raw, dest = _aligned_memoryview(size, 4096)
    _fill(source)

    client = LMCacheFSClient(str(tmp_path), 2, "", False, 0, 16)
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
    key = "test_model@00000000@aabbccddeeff00112233"
    _source_raw, source = _aligned_memoryview(4096, 4096)
    _fill(source)

    writer = LMCacheFSClient(str(tmp_path), 1, "", False, 0, 4)
    try:
        assert _submit_and_wait(writer, "submit_batch_set", key, source)[1]
    finally:
        writer.close()

    flat = LMCacheFSClient(str(tmp_path), 1, "", False, 0, 0)
    try:
        exists = _wait_for_completion(flat, flat.submit_batch_exists([key]))
        assert exists[3] == [False]
    finally:
        flat.close()


def _store(client: Any, key: str, view: memoryview) -> None:
    assert _submit_and_wait(client, "submit_batch_set", key, view)[1]


def _data_path(base, key_hash: str):
    return next(p for p in base.rglob("*.data") if key_hash in p.name)


def _shard_name(key: str, num_shard_dirs: int) -> str:
    """Mirror the connector layout: FNV-1a of the on-disk filename."""
    model, rank, chunk = key.split("@")
    filename = f"{model}@0x{rank}@{chunk}.data"
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
    keys = [f"test_model@00000000@{i:016x}" for i in range(1, 64)]
    first = keys[0]
    same = [k for k in keys[1:] if _shard_name(k, shards) == _shard_name(first, shards)]
    early, late = same[0], same[1]
    client = LMCacheFSClient(str(tmp_path), 2, "", False, 0, shards, 100)
    try:
        _raw, view = _aligned_memoryview(size, 4096)
        _fill(view)
        _store(client, first, view)
        stored = _data_path(tmp_path, first.rsplit("@", 1)[1])
        assert stored.parent.name == _shard_name(first, shards)
        inode = stored.stat().st_ino
        deleted = _wait_for_completion(client, client.submit_batch_delete([first]))
        assert deleted[1], deleted[2]
        pool = stored.parent / ".pool"
        assert [p.stat().st_ino for p in pool.iterdir()] == [inode]

        # Inside the quarantine a store in the same shard gets a fresh file.
        _store(client, early, view)
        assert _data_path(tmp_path, early.rsplit("@", 1)[1]).stat().st_ino != inode

        time.sleep(1.2)
        _raw2, fresh = _aligned_memoryview(size, 4096)
        fresh[:] = b"\x5a" * size
        _store(client, late, fresh)
        assert _data_path(tmp_path, late.rsplit("@", 1)[1]).stat().st_ino == inode
        assert not list(pool.iterdir())

        _raw3, dest = _aligned_memoryview(size, 4096)
        assert _submit_and_wait(client, "submit_batch_get", late, dest)[1]
        assert bytes(dest) == b"\x5a" * size
    finally:
        client.close()


def test_recycling_adopts_pool_files_left_by_an_earlier_client(tmp_path) -> None:
    """Pool files survive a restart and are reused without a new quarantine."""
    LMCacheFSClient = _import_fs_client()
    size = 8192
    key = "test_model@00000000@" + "a" * 16
    _raw, view = _aligned_memoryview(size, 4096)
    _fill(view)

    client = LMCacheFSClient(str(tmp_path), 1, "", False, 0, 0, 10)
    try:
        _store(client, key, view)
        inode = _data_path(tmp_path, "a" * 16).stat().st_ino
        assert _wait_for_completion(client, client.submit_batch_delete([key]))[1]
    finally:
        client.close()

    client = LMCacheFSClient(str(tmp_path), 1, "", False, 0, 0, 10)
    try:
        other = "test_model@00000000@" + "b" * 16
        _store(client, other, view)
        assert _data_path(tmp_path, "b" * 16).stat().st_ino == inode
        assert not list((tmp_path / ".pool").iterdir())
    finally:
        client.close()


def test_dedicated_store_and_delete_lanes_serve_requests(tmp_path) -> None:
    """Store/delete lanes behave like the shared pool from the caller side."""
    LMCacheFSClient = _import_fs_client()
    size = 8192
    client = LMCacheFSClient(str(tmp_path), 2, "", False, 0, 4, 0, 2, 2)
    try:
        keys = [f"test_model@00000000@{i:016x}" for i in (1, 2)]
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
        LMCacheFSClient(str(tmp_path), 1, "", False, 0, 0, 0, -1, 0)
