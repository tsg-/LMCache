# SPDX-License-Identifier: Apache-2.0
"""Microbenchmark: raw batch submit vs. string submit, adapter side only.

Measures the Python work a 56-key submit of 64 KiB chunks does before the
native call (key encoding and memoryview building vs. ``_pack_raw``), with a
no-op client.
"""

# Standard
import time

# Third Party
import pytest
import torch

# First Party
from lmcache.v1.distributed.api import (
    MemoryResidency,
    MemorySegment,
    MemoryTags,
    ObjectKey,
)
from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (
    NativeConnectorL2Adapter,
    _obj_to_memoryview,
    _object_key_to_string,
)
from lmcache.v1.memory_management import (
    MemoryFormat,
    MemoryObjMetadata,
    TensorMemoryObj,
)
from lmcache.v1.platform import create_event_notifier

_NUM_KEYS = 56
_CHUNK_BYTES = 64 * 1024
_ROUNDS = 200


class _NoopClient:
    def __init__(self) -> None:
        self._efd = create_event_notifier()

    def event_fd(self) -> int:
        return self._efd.fileno()

    def drain_completions(self) -> list:
        return []

    def submit_batch_set(self, keys, memoryviews) -> int:
        return 1

    def submit_batch_set_raw(self, **kwargs) -> int:
        return 1

    def close(self) -> None:
        self._efd.close()


def _make_obj() -> TensorMemoryObj:
    raw = torch.empty(_CHUNK_BYTES, dtype=torch.uint8)
    meta = MemoryObjMetadata(
        shape=torch.Size([_CHUNK_BYTES]),
        dtype=torch.uint8,
        address=0,
        phy_size=_CHUNK_BYTES,
        fmt=MemoryFormat.KV_2LTD,
        ref_count=1,
    )
    return TensorMemoryObj(raw, meta, parent_allocator=None)


@pytest.mark.slow
def test_raw_pack_faster_than_string_path() -> None:
    client = _NoopClient()
    adapter = NativeConnectorL2Adapter(
        client, MemoryTags(MemorySegment.STORAGE, MemoryResidency.REMOTE)
    )
    try:
        keys = [
            ObjectKey(
                chunk_hash=ObjectKey.IntHash2Bytes(i),
                model_name="mixtral-8x7b",
                kv_rank=0,
            )
            for i in range(_NUM_KEYS)
        ]
        objs = [_make_obj() for _ in range(_NUM_KEYS)]

        def string_path() -> None:
            [_object_key_to_string(k) for k in keys]
            [_obj_to_memoryview(o, False) for o in objs]

        def raw_path() -> None:
            assert adapter._pack_raw(keys, objs) is not None

        def per_submit_us(fn) -> float:
            for _ in range(20):
                fn()
            best = float("inf")
            for _ in range(5):
                start = time.perf_counter()
                for _ in range(_ROUNDS):
                    fn()
                best = min(best, (time.perf_counter() - start) / _ROUNDS)
            return best * 1e6

        string_us = per_submit_us(string_path)
        raw_us = per_submit_us(raw_path)
        print(
            f"\n56-key 64 KiB submit: string path {string_us:.1f} us, "
            f"raw path {raw_us:.1f} us ({string_us / raw_us:.1f}x)"
        )
        assert raw_us < string_us
    finally:
        adapter.close()
        client.close()
