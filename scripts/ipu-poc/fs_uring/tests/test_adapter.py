# SPDX-License-Identifier: Apache-2.0
"""Run the real LMCache native_plugin factory and adapter with CPU memory."""

# Standard
from pathlib import Path
import mmap
import time

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
from lmcache.v1.distributed.l2_adapters.factory import create_l2_adapter_from_registry
from lmcache.v1.distributed.l2_adapters.native_plugin_l2_adapter import (
    NativePluginL2AdapterConfig,
)
from lmcache.v1.memory_management import (
    MemoryObj,
    MemoryObjMetadata,
    TensorMemoryObj,
)


def objects(count: int) -> tuple[list[mmap.mmap], list[MemoryObj]]:
    """Build real page-aligned CPU MemoryObjs without a GPU allocator."""
    backing = [mmap.mmap(-1, 65536) for _ in range(count)]
    result: list[MemoryObj] = []
    for memory in backing:
        tensor = torch.frombuffer(memory, dtype=torch.uint8)
        metadata = MemoryObjMetadata(
            shape=tensor.shape,
            dtype=torch.uint8,
            address=tensor.data_ptr(),
            phy_size=65536,
            ref_count=1,
        )
        result.append(TensorMemoryObj(tensor, metadata, None))
    return backing, result


@pytest.mark.parametrize("salt", ["", "test-tenant"])
def test_native_plugin_store_lookup_load_and_partial_failure(
    tmp_path: Path, salt: str
) -> None:
    """Verify factory loading, real MemoryObjs, demux events and bitmaps."""
    config = NativePluginL2AdapterConfig.from_dict(
        {
            "module_path": "lmcache_fs_uring",
            "class_name": "LMCacheFSUringClient",
            "adapter_params": {
                "base_path": str(tmp_path),
                "num_workers": 2,
                "use_odirect": True,
                "io_depth": 32,
            },
        }
    )
    adapter = create_l2_adapter_from_registry(config)
    source, source_objects = objects(56)
    dest, dest_objects = objects(56)
    keys = [
        ObjectKey(bytes.fromhex(f"{i:032x}"), "model/test", 0, 0, salt)
        for i in range(56)
    ]
    try:
        for i, memory in enumerate(source):
            memory[:] = bytes([i]) * 65536
        task = adapter.submit_store_task(keys, source_objects)
        deadline = time.monotonic() + 10
        result = None
        while result is None and time.monotonic() < deadline:
            result = adapter.pop_completed_store_tasks().get(task)
            time.sleep(0.005)
        assert result is not None
        assert result.is_successful()
        assert result.bytes_transferred() == 56 * 65536
        task = adapter.submit_load_task(keys, dest_objects)
        deadline = time.monotonic() + 10
        loaded = None
        while loaded is None and time.monotonic() < deadline:
            loaded = adapter.query_load_result(task)
            time.sleep(0.005)
        assert loaded is not None and loaded.popcount() == 56
        assert all(memory[:] == source[i][:] for i, memory in enumerate(dest))
        suffix = f"@{salt}" if salt else ""
        expected = tmp_path / (
            "model-SEP-test@0x00000000@0@00000000000000000000000000000000"
            + suffix
            + ".data"
        )
        assert expected.exists()
        expected.unlink()
        task = adapter.submit_load_task(keys, dest_objects)
        deadline = time.monotonic() + 10
        loaded = None
        while loaded is None and time.monotonic() < deadline:
            loaded = adapter.query_load_result(task)
            time.sleep(0.005)
        assert loaded is not None and loaded.popcount() == 55
        assert not loaded.test(0)
        task = adapter.submit_lookup_and_lock_task(
            keys, {0: MemoryLayoutDesc([torch.Size([65536])], [torch.uint8])}
        )
        deadline = time.monotonic() + 10
        found = None
        while found is None and time.monotonic() < deadline:
            found = adapter.query_lookup_and_lock_result(task)
            time.sleep(0.005)
        assert found is not None and found.popcount() == 55
        adapter.submit_unlock(keys)
    finally:
        adapter.close()
        source_objects.clear()
        dest_objects.clear()
        for memory in source + dest:
            memory.close()
