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
        IPURdmaWrapper.wrap(src)
        # After wrap, data_ptr may have changed (SHM migration on stub)
        data_ptr = src.data_ptr()

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


class TestPostWrite:
    """StubRdmaTransport.post_write() pushes local -> remote via memcpy."""

    def test_write_copies_local_to_remote(self, _isolate_transport):
        transport = _isolate_transport
        src = torch.randn(16, dtype=torch.float32)
        dst = torch.zeros(16, dtype=torch.float32)

        src_nbytes = src.numel() * src.element_size()
        dst_nbytes = dst.numel() * dst.element_size()
        local_mr = transport.register_mr(src.data_ptr(), src_nbytes)
        remote_mr = transport.register_mr(dst.data_ptr(), dst_nbytes)

        from lmcache.v1.platform.ipu.rdma_transport import RegisteredBuffer

        future = transport.post_write(
            local_buf=RegisteredBuffer(
                addr=src.data_ptr(), length=src_nbytes, mr=local_mr
            ),
            remote_addr=dst.data_ptr(),
            rkey=remote_mr.rkey,
            length=src_nbytes,
        )
        assert transport.poll_completion(future, timeout_ms=100)
        assert torch.equal(dst, src)


class TestCrossProcessSHM:
    """Cross-process RDMA simulation via POSIX SHM (LMCache-d2e fix)."""

    def test_wrap_produces_shm_name_on_stub(self, _isolate_transport):
        """wrap() migrates tensor to SHM and carries the name in wrapper."""
        src = torch.randn(32, dtype=torch.float32)
        wrapper = IPURdmaWrapper.wrap(src)
        assert wrapper.shm_name != ""
        assert wrapper.shm_name.startswith("/lmcache_rdma_")

    def test_shm_name_survives_pickle(self, _isolate_transport):
        """shm_name is preserved across ZMQ serialization (pickle)."""
        src = torch.randn(16, dtype=torch.float32)
        wrapper = IPURdmaWrapper.wrap(src)
        restored = pickle.loads(pickle.dumps(wrapper))
        assert restored.shm_name == wrapper.shm_name

    def test_map_remote_mr_resolves_cross_process_read(self, _isolate_transport):
        """Simulates cross-process STORE: server maps client's SHM, reads."""
        transport = _isolate_transport

        # Client side: wrap tensor (migrates to SHM)
        src = torch.arange(24, dtype=torch.float32)
        wrapper = IPURdmaWrapper.wrap(src)

        # Server side: map the remote SHM and post_read into a local buffer
        transport.map_remote_mr(
            wrapper.rkey, wrapper.shm_name,
            wrapper.remote_addr, wrapper.length,
        )
        dst_buf = transport.allocate_buffer(wrapper.length)
        future = transport.post_read(
            local_buf=dst_buf,
            remote_addr=wrapper.remote_addr,
            rkey=wrapper.rkey,
            length=wrapper.length,
        )
        assert transport.poll_completion(future, timeout_ms=1000)

        # Verify data arrived correctly
        import ctypes
        buf_type = ctypes.c_uint8 * wrapper.length
        c_buf = buf_type.from_address(dst_buf.addr)
        result = torch.frombuffer(c_buf, dtype=torch.float32).clone()
        assert torch.equal(result, src)

        transport.unmap_remote_mr(wrapper.rkey)
        transport.free_buffer(dst_buf)

    def test_map_remote_mr_resolves_cross_process_write(self, _isolate_transport):
        """Simulates cross-process RETRIEVE: server writes into client's SHM."""
        transport = _isolate_transport

        # Client side: allocate a destination buffer in SHM
        dst = torch.zeros(16, dtype=torch.float32)
        wrapper = IPURdmaWrapper.wrap(dst)

        # Server side: map the remote SHM, write local data into it
        transport.map_remote_mr(
            wrapper.rkey, wrapper.shm_name,
            wrapper.remote_addr, wrapper.length,
        )
        src_data = torch.arange(16, dtype=torch.float32)
        src_nbytes = src_data.numel() * src_data.element_size()
        src_mr = transport.register_mr(src_data.data_ptr(), src_nbytes)

        from lmcache.v1.platform.ipu.rdma_transport import RegisteredBuffer

        future = transport.post_write(
            local_buf=RegisteredBuffer(
                addr=src_data.data_ptr(), length=src_nbytes, mr=src_mr,
            ),
            remote_addr=wrapper.remote_addr,
            rkey=wrapper.rkey,
            length=src_nbytes,
        )
        assert transport.poll_completion(future, timeout_ms=1000)

        # Client side: the tensor is SHM-backed, so writes are visible
        assert torch.equal(dst, src_data)

        transport.unmap_remote_mr(wrapper.rkey)
        transport.deregister_mr(src_mr)

    def test_map_remote_mr_partial_offset(self, _isolate_transport):
        """post_read with an offset into a mapped remote SHM segment."""
        transport = _isolate_transport

        src = torch.arange(32, dtype=torch.float32)
        wrapper = IPURdmaWrapper.wrap(src)

        transport.map_remote_mr(
            wrapper.rkey, wrapper.shm_name,
            wrapper.remote_addr, wrapper.length,
        )

        # Read only the second half
        half_len = wrapper.length // 2
        dst_buf = transport.allocate_buffer(half_len)
        future = transport.post_read(
            local_buf=dst_buf,
            remote_addr=wrapper.remote_addr + half_len,
            rkey=wrapper.rkey,
            length=half_len,
        )
        assert transport.poll_completion(future, timeout_ms=1000)

        import ctypes
        buf_type = ctypes.c_uint8 * half_len
        c_buf = buf_type.from_address(dst_buf.addr)
        result = torch.frombuffer(c_buf, dtype=torch.float32).clone()
        expected = torch.arange(16, 32, dtype=torch.float32)
        assert torch.equal(result, expected)

        transport.unmap_remote_mr(wrapper.rkey)
        transport.free_buffer(dst_buf)

    def test_no_shm_name_when_verbs_backend(self):
        """wrap() does NOT migrate to SHM when a non-stub transport is active."""
        from unittest.mock import MagicMock
        from lmcache.v1.platform.ipu.rdma_transport import MrInfo

        mock_transport = MagicMock()
        mock_transport.register_mr.return_value = MrInfo(
            rkey=99, addr=0x1000, length=64, handle=None, deregister=None,
        )
        set_rdma_transport(mock_transport)

        src = torch.randn(16, dtype=torch.float32)
        wrapper = IPURdmaWrapper.wrap(src)
        assert wrapper.shm_name == ""
