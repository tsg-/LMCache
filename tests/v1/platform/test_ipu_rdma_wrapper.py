# SPDX-License-Identifier: Apache-2.0
"""Tests for ``lmcache.v1.platform.ipu.rdma_wrapper``.

Validates the full wrap() → serialize → to_tensor() round-trip using
StubRdmaTransport (memcpy, no RDMA hardware required).
"""

import gc
import pickle
import weakref

import pytest
import torch

from lmcache.v1.platform.ipu.rdma_transport import (
    StubRdmaTransport,
    set_rdma_transport,
)
from lmcache.v1.platform.ipu.rdma_wrapper import (
    IPURdmaWrapper,
    _REGISTERED_MRS,
)


@pytest.fixture(autouse=True)
def _isolate_transport():
    """Inject a fresh StubRdmaTransport for each test."""
    transport = StubRdmaTransport()
    set_rdma_transport(transport)
    yield transport
    set_rdma_transport(None)
    _REGISTERED_MRS.clear()


class TestWrapBasic:
    """wrap() produces a valid wrapper with correct descriptor fields."""

    def test_wrap_random_tensor(self):
        src = torch.randn(4, 8, dtype=torch.float32)
        wrapper = IPURdmaWrapper.wrap(src)

        assert wrapper.rkey > 0
        assert wrapper.remote_addr == src.data_ptr()
        assert wrapper.length == src.numel() * src.element_size()
        assert wrapper.dtype == torch.float32
        assert wrapper.shape == (4, 8)
        assert wrapper.stride == src.stride()
        assert wrapper.storage_offset == 0

    def test_wrap_rejects_non_contiguous(self):
        src = torch.randn(4, 8, dtype=torch.float32)[:, ::2]
        assert not src.is_contiguous()
        with pytest.raises(ValueError, match="contiguous"):
            IPURdmaWrapper.wrap(src)

    def test_wrap_reuses_mr_for_same_tensor(self):
        src = torch.randn(16, dtype=torch.float32)
        w1 = IPURdmaWrapper.wrap(src)
        w2 = IPURdmaWrapper.wrap(src)
        assert w1.rkey == w2.rkey

    def test_wrap_different_tensors_get_different_rkeys(self):
        t1 = torch.randn(8, dtype=torch.float32)
        t2 = torch.randn(8, dtype=torch.float32)
        w1 = IPURdmaWrapper.wrap(t1)
        w2 = IPURdmaWrapper.wrap(t2)
        assert w1.rkey != w2.rkey


class TestPickleRoundTrip:
    """Wrapper survives pickle serialization (simulates ZMQ wire)."""

    def test_pickle_preserves_fields(self):
        src = torch.randn(2, 3, 4, dtype=torch.bfloat16)
        wrapper = IPURdmaWrapper.wrap(src)

        data = pickle.dumps(wrapper)
        restored = pickle.loads(data)

        assert restored.rkey == wrapper.rkey
        assert restored.remote_addr == wrapper.remote_addr
        assert restored.length == wrapper.length
        assert restored.dtype == wrapper.dtype
        assert restored.shape == wrapper.shape
        assert restored.stride == wrapper.stride
        assert restored.storage_offset == wrapper.storage_offset

    def test_pickle_preserves_type(self):
        src = torch.randn(4, dtype=torch.float32)
        wrapper = IPURdmaWrapper.wrap(src)
        restored = pickle.loads(pickle.dumps(wrapper))
        assert type(restored) is IPURdmaWrapper


class TestToTensor:
    """to_tensor() pulls data via RDMA Read (stub memcpy) correctly."""

    def test_round_trip_content_equality(self):
        src = torch.randn(4, 8, dtype=torch.float32)
        wrapper = IPURdmaWrapper.wrap(src)
        restored = pickle.loads(pickle.dumps(wrapper))

        result = restored.to_tensor()
        assert torch.equal(result, src)

    def test_round_trip_shape_preserved(self):
        src = torch.randn(2, 3, 5, dtype=torch.float32)
        wrapper = IPURdmaWrapper.wrap(src)
        restored = pickle.loads(pickle.dumps(wrapper))

        result = restored.to_tensor()
        assert result.shape == src.shape
        assert result.stride() == src.stride()

    @pytest.mark.parametrize("dtype", [
        torch.float32,
        torch.float16,
        torch.bfloat16,
        torch.int32,
        torch.uint8,
    ])
    def test_round_trip_dtypes(self, dtype):
        src = torch.ones(16, dtype=dtype)
        wrapper = IPURdmaWrapper.wrap(src)
        restored = pickle.loads(pickle.dumps(wrapper))

        result = restored.to_tensor()
        assert result.dtype == dtype
        assert torch.equal(result, src)

    def test_round_trip_large_tensor(self):
        """256KB page (IPU DMA sweet spot)."""
        nbytes = 256 * 1024
        numel = nbytes // 4  # float32
        src = torch.randn(numel, dtype=torch.float32)
        wrapper = IPURdmaWrapper.wrap(src)
        restored = pickle.loads(pickle.dumps(wrapper))

        result = restored.to_tensor()
        assert torch.equal(result, src)
        assert result.numel() * result.element_size() == nbytes

    def test_round_trip_multidimensional(self):
        """Simulates a KV page shape: [num_heads, tokens, head_dim]."""
        src = torch.randn(8, 256, 128, dtype=torch.float32)
        wrapper = IPURdmaWrapper.wrap(src)
        restored = pickle.loads(pickle.dumps(wrapper))

        result = restored.to_tensor()
        assert result.shape == (8, 256, 128)
        assert torch.equal(result, src)

    def test_empty_tensor(self):
        src = torch.empty(0, dtype=torch.float32)
        wrapper = IPURdmaWrapper.wrap(src)
        restored = pickle.loads(pickle.dumps(wrapper))

        result = restored.to_tensor()
        assert result.shape == (0,)
        assert result.dtype == torch.float32


class TestBufferLifetime:
    """Buffer keep-alive and GC behavior."""

    def test_result_tensor_valid_after_to_tensor(self):
        src = torch.randn(16, dtype=torch.float32)
        wrapper = IPURdmaWrapper.wrap(src)
        restored = pickle.loads(pickle.dumps(wrapper))

        result = restored.to_tensor()
        assert result.data_ptr() != 0
        assert torch.equal(result, src)

    def test_buffer_freed_on_tensor_gc(self, _isolate_transport):
        transport = _isolate_transport
        src = torch.randn(16, dtype=torch.float32)
        wrapper = IPURdmaWrapper.wrap(src)
        restored = pickle.loads(pickle.dumps(wrapper))

        result = restored.to_tensor()
        buf_ref = weakref.ref(result.untyped_storage())
        del result
        gc.collect()
        # Storage should be collected (weakref returns None)
        assert buf_ref() is None

    def test_mr_deregistered_on_source_tensor_gc(self, _isolate_transport):
        transport = _isolate_transport
        src = torch.randn(8, dtype=torch.float32)
        data_ptr = src.data_ptr()
        IPURdmaWrapper.wrap(src)

        assert data_ptr in _REGISTERED_MRS

        del src
        gc.collect()
        # MR entry should be cleaned up by weakref finalizer
        assert data_ptr not in _REGISTERED_MRS

    def test_stale_mr_entry_replaced_on_id_reuse(self):
        """A GC'd tensor whose data_ptr is recycled must not reuse stale MR."""
        t1 = torch.randn(8, dtype=torch.float32)
        w1 = IPURdmaWrapper.wrap(t1)
        rkey1 = w1.rkey

        del t1
        gc.collect()

        t2 = torch.randn(8, dtype=torch.float32)
        w2 = IPURdmaWrapper.wrap(t2)
        # May or may not get same data_ptr, but if it does, rkey must differ
        if w2.remote_addr == w1.remote_addr:
            assert w2.rkey != rkey1
