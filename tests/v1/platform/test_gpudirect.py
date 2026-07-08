# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the GPUDirect RDMA path.

All tests mock both CUDA (torch.cuda) and pyverbs so no GPU or RDMA hardware
is required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch, PropertyMock

import pytest
import torch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_transport(register_raises: bool = False) -> MagicMock:
    """Return a mock transport whose register_mr / deregister_mr can be configured."""
    transport = MagicMock()
    if register_raises:
        transport.register_mr.side_effect = RuntimeError("MR registration failed")
    else:
        mr = MagicMock()
        mr.rkey = 42
        mr.addr = 0xDEADBEEF
        mr.length = 64
        mr.deregister = None
        transport.register_mr.return_value = mr
    return transport


# ---------------------------------------------------------------------------
# is_gpudirect_available()
# ---------------------------------------------------------------------------

class TestIsGpuDirectAvailable:
    """Tests for the cheap availability check."""

    def test_false_when_no_env_var(self, monkeypatch):
        """Returns False when LMCACHE_RDMA_GPUDIRECT is not set."""
        monkeypatch.delenv("LMCACHE_RDMA_GPUDIRECT", raising=False)
        import lmcache.v1.platform.rdma.gpudirect as gd
        monkeypatch.setattr(gd, "_HAS_PYVERBS", True)
        with patch("torch.cuda.is_available", return_value=True):
            assert gd.is_gpudirect_available() is False

    def test_false_when_env_var_not_one(self, monkeypatch):
        """Returns False when env var is set to a value other than '1'."""
        monkeypatch.setenv("LMCACHE_RDMA_GPUDIRECT", "0")
        import lmcache.v1.platform.rdma.gpudirect as gd
        monkeypatch.setattr(gd, "_HAS_PYVERBS", True)
        with patch("torch.cuda.is_available", return_value=True):
            assert gd.is_gpudirect_available() is False

    def test_false_when_no_cuda(self, monkeypatch):
        """Returns False when env var is set but CUDA is unavailable."""
        monkeypatch.setenv("LMCACHE_RDMA_GPUDIRECT", "1")
        import lmcache.v1.platform.rdma.gpudirect as gd
        monkeypatch.setattr(gd, "_HAS_PYVERBS", True)
        with patch("torch.cuda.is_available", return_value=False):
            assert gd.is_gpudirect_available() is False

    def test_false_when_no_pyverbs(self, monkeypatch):
        """Returns False when pyverbs is not installed."""
        monkeypatch.setenv("LMCACHE_RDMA_GPUDIRECT", "1")
        import lmcache.v1.platform.rdma.gpudirect as gd
        monkeypatch.setattr(gd, "_HAS_PYVERBS", False)
        with patch("torch.cuda.is_available", return_value=True):
            assert gd.is_gpudirect_available() is False

    def test_true_when_all_preconditions_met(self, monkeypatch):
        """Returns True when env var='1', CUDA available, pyverbs present."""
        monkeypatch.setenv("LMCACHE_RDMA_GPUDIRECT", "1")
        import lmcache.v1.platform.rdma.gpudirect as gd
        monkeypatch.setattr(gd, "_HAS_PYVERBS", True)
        with patch("torch.cuda.is_available", return_value=True):
            assert gd.is_gpudirect_available() is True


# ---------------------------------------------------------------------------
# allocate_gpudirect_buffer()
# ---------------------------------------------------------------------------

class TestAllocateGpuDirectBuffer:
    """Tests for the factory function."""

    def test_returns_none_when_unavailable(self, monkeypatch):
        """Returns None (no exception) when GPUDirect is unavailable."""
        import lmcache.v1.platform.rdma.gpudirect as gd
        monkeypatch.setattr(gd, "is_gpudirect_available", lambda: False)
        result = gd.allocate_gpudirect_buffer(64, torch.float16, (4, 8), MagicMock())
        assert result is None

    def test_returns_none_on_mr_registration_failure(self, monkeypatch):
        """Returns None (no exception) when MR registration raises."""
        import lmcache.v1.platform.rdma.gpudirect as gd
        monkeypatch.setattr(gd, "is_gpudirect_available", lambda: True)
        transport = _make_mock_transport(register_raises=True)

        # Patch GpuDirectBuffer.__init__ to propagate from register_mr failure
        with patch("torch.cuda.is_available", return_value=True), \
             patch("torch.empty") as mock_empty:
            mock_t = MagicMock()
            mock_t.data_ptr.return_value = 0x1000
            mock_t.element_size.return_value = 2
            mock_empty.return_value = mock_t

            result = gd.allocate_gpudirect_buffer(64, torch.float16, (4, 8), transport)

        assert result is None

    def test_returns_buffer_when_available(self, monkeypatch):
        """Returns a GpuDirectBuffer instance when everything succeeds."""
        import lmcache.v1.platform.rdma.gpudirect as gd
        monkeypatch.setattr(gd, "is_gpudirect_available", lambda: True)
        transport = _make_mock_transport()

        with patch("torch.cuda.is_available", return_value=True), \
             patch("torch.empty") as mock_empty:
            mock_t = MagicMock()
            mock_t.data_ptr.return_value = 0xDEADBEEF
            mock_t.element_size.return_value = 2
            mock_empty.return_value = mock_t

            result = gd.allocate_gpudirect_buffer(64, torch.float16, (4, 8), transport)

        assert result is not None
        assert isinstance(result, gd.GpuDirectBuffer)


# ---------------------------------------------------------------------------
# GpuDirectBuffer: addr and mr properties
# ---------------------------------------------------------------------------

class TestGpuDirectBufferProperties:
    """Tests for addr / mr duck-typing properties."""

    def _make_buffer(self):
        """Return a GpuDirectBuffer with mocked CUDA tensor."""
        import lmcache.v1.platform.rdma.gpudirect as gd
        transport = _make_mock_transport()

        with patch("torch.cuda.is_available", return_value=True), \
             patch("torch.empty") as mock_empty:
            mock_t = MagicMock()
            mock_t.data_ptr.return_value = 0xDEADBEEF
            mock_t.element_size.return_value = 2
            mock_empty.return_value = mock_t

            buf = gd.GpuDirectBuffer(
                length=64,
                dtype=torch.float16,
                shape=(4, 8),
                device=0,
                transport=transport,
            )
        return buf

    def test_addr_equals_data_ptr(self):
        """GpuDirectBuffer.addr returns the CUDA tensor's data_ptr()."""
        buf = self._make_buffer()
        assert buf.addr == 0xDEADBEEF

    def test_mr_addr_matches_data_ptr(self):
        """GpuDirectBuffer.mr.addr equals the registered pointer."""
        buf = self._make_buffer()
        assert buf.mr.addr == 0xDEADBEEF

    def test_mr_rkey_is_set(self):
        """GpuDirectBuffer.mr.rkey is populated from transport.register_mr()."""
        buf = self._make_buffer()
        assert buf.mr.rkey == 42


# ---------------------------------------------------------------------------
# GpuDirectBuffer: to_tensor()
# ---------------------------------------------------------------------------

class TestGpuDirectBufferToTensor:
    """to_tensor() must return a CUDA tensor without a host-memory copy."""

    def test_to_tensor_returns_cuda_tensor(self):
        """to_tensor() returns a tensor that reports is_cuda=True (mocked)."""
        import lmcache.v1.platform.rdma.gpudirect as gd
        transport = _make_mock_transport()

        # Build a mock CUDA tensor
        mock_cuda_tensor = MagicMock(spec=torch.Tensor)
        mock_cuda_tensor.data_ptr.return_value = 0xDEADBEEF
        mock_cuda_tensor.element_size.return_value = 2
        # .view() returns another mock tensor; .as_strided on that returns final
        mock_view = MagicMock(spec=torch.Tensor)
        mock_cuda_tensor.view.return_value = mock_view
        mock_strided = MagicMock(spec=torch.Tensor)
        type(mock_strided).is_cuda = PropertyMock(return_value=True)

        with patch("torch.cuda.is_available", return_value=True), \
             patch("torch.empty", return_value=mock_cuda_tensor), \
             patch("torch.as_strided", return_value=mock_strided):
            buf = gd.GpuDirectBuffer(
                length=64,
                dtype=torch.float16,
                shape=(4, 8),
                device=0,
                transport=transport,
            )
            result = buf.to_tensor()

        assert result.is_cuda is True


# ---------------------------------------------------------------------------
# GpuDirectBuffer: close()
# ---------------------------------------------------------------------------

class TestGpuDirectBufferClose:
    """close() must deregister the MR exactly once."""

    def test_close_calls_deregister_mr(self):
        """transport.deregister_mr is called when close() is invoked."""
        import lmcache.v1.platform.rdma.gpudirect as gd
        transport = _make_mock_transport()

        with patch("torch.cuda.is_available", return_value=True), \
             patch("torch.empty") as mock_empty:
            mock_t = MagicMock()
            mock_t.data_ptr.return_value = 0x1234
            mock_t.element_size.return_value = 2
            mock_empty.return_value = mock_t

            buf = gd.GpuDirectBuffer(
                length=64,
                dtype=torch.float16,
                shape=(4, 8),
                device=0,
                transport=transport,
            )

        buf.close()
        transport.deregister_mr.assert_called_once_with(buf.mr)

    def test_close_is_idempotent(self):
        """Calling close() twice does not call deregister_mr twice."""
        import lmcache.v1.platform.rdma.gpudirect as gd
        transport = _make_mock_transport()

        with patch("torch.cuda.is_available", return_value=True), \
             patch("torch.empty") as mock_empty:
            mock_t = MagicMock()
            mock_t.data_ptr.return_value = 0x5678
            mock_t.element_size.return_value = 2
            mock_empty.return_value = mock_t

            buf = gd.GpuDirectBuffer(
                length=64,
                dtype=torch.float16,
                shape=(4, 8),
                device=0,
                transport=transport,
            )

        buf.close()
        buf.close()
        assert transport.deregister_mr.call_count == 1


# ---------------------------------------------------------------------------
# RdmaWrapper.to_tensor_direct()
# ---------------------------------------------------------------------------

class TestToTensorDirect:
    """Tests for the to_tensor_direct() method on RdmaWrapper."""

    def _make_wrapper(self):
        """Return a minimal RdmaWrapper without RDMA hardware."""
        from lmcache.v1.platform.rdma.rdma_transport import MrInfo
        from lmcache.v1.platform.rdma.rdma_wrapper import RdmaWrapper

        mr = MrInfo(rkey=7, addr=0xABCD, length=64, handle=None)
        mock_tensor = MagicMock(spec=torch.Tensor)
        mock_tensor.dtype = torch.float16
        mock_tensor.shape = torch.Size([4, 8])
        mock_tensor.stride.return_value = (8, 1)
        mock_tensor.storage_offset.return_value = 0
        wrapper = RdmaWrapper.__new__(RdmaWrapper)
        wrapper.rkey = mr.rkey
        wrapper.remote_addr = mr.addr
        wrapper.length = mr.length
        wrapper.handle = (mr.rkey, mr.addr, mr.length)
        wrapper.dtype = torch.float16
        wrapper.shape = (4, 8)
        wrapper.stride = (8, 1)
        wrapper.storage_offset = 0
        wrapper.device_uuid = "rdma"
        wrapper.shm_name = ""
        return wrapper

    def test_fallback_when_gpudirect_unavailable(self, monkeypatch):
        """to_tensor_direct() calls to_tensor() when GPUDirect is unavailable."""
        wrapper = self._make_wrapper()

        expected = torch.zeros(4, 8, dtype=torch.float16)
        mock_to_tensor = MagicMock(return_value=expected)
        monkeypatch.setattr(wrapper, "to_tensor", mock_to_tensor)

        with patch(
            "lmcache.v1.platform.rdma.gpudirect.allocate_gpudirect_buffer",
            return_value=None,
        ), patch(
            "lmcache.v1.platform.rdma.rdma_wrapper.get_rdma_transport",
        ):
            result = wrapper.to_tensor_direct()

        mock_to_tensor.assert_called_once()
        assert result is expected

    def test_uses_gpu_buffer_when_available(self, monkeypatch):
        """to_tensor_direct() uses post_read with the GPU buffer as local_buf."""
        from lmcache.v1.platform.rdma.rdma_transport import RdmaFuture

        wrapper = self._make_wrapper()

        # Build a mock GPU buffer
        gpu_buf = MagicMock()
        gpu_tensor = MagicMock(spec=torch.Tensor)
        gpu_buf.to_tensor.return_value = gpu_tensor

        # Mock untyped_storage for the weakref.finalize call
        mock_storage = MagicMock()
        gpu_tensor.untyped_storage.return_value = mock_storage

        future = RdmaFuture()
        future.set_complete(success=True)

        mock_transport = MagicMock()
        mock_transport.post_read.return_value = future
        mock_transport.poll_completion.return_value = True

        with patch(
            "lmcache.v1.platform.rdma.gpudirect.allocate_gpudirect_buffer",
            return_value=gpu_buf,
        ), patch(
            "lmcache.v1.platform.rdma.rdma_wrapper.get_rdma_transport",
            return_value=mock_transport,
        ):
            result = wrapper.to_tensor_direct()

        # post_read must have been called with gpu_buf as local_buf
        mock_transport.post_read.assert_called_once_with(
            local_buf=gpu_buf,
            remote_addr=wrapper.remote_addr,
            rkey=wrapper.rkey,
            length=wrapper.length,
        )
        assert result is gpu_tensor

    def test_fallback_on_timeout(self, monkeypatch):
        """to_tensor_direct() falls back to to_tensor() after RDMA Read timeout."""
        from lmcache.v1.platform.rdma.rdma_transport import RdmaFuture

        wrapper = self._make_wrapper()

        expected = torch.zeros(4, 8, dtype=torch.float16)
        mock_to_tensor = MagicMock(return_value=expected)
        monkeypatch.setattr(wrapper, "to_tensor", mock_to_tensor)

        gpu_buf = MagicMock()
        future = RdmaFuture()  # not completed

        mock_transport = MagicMock()
        mock_transport.post_read.return_value = future
        mock_transport.poll_completion.return_value = False
        mock_transport.drain_on_timeout.return_value = True

        with patch(
            "lmcache.v1.platform.rdma.gpudirect.allocate_gpudirect_buffer",
            return_value=gpu_buf,
        ), patch(
            "lmcache.v1.platform.rdma.rdma_wrapper.get_rdma_transport",
            return_value=mock_transport,
        ):
            result = wrapper.to_tensor_direct()

        gpu_buf.close.assert_called_once()
        mock_to_tensor.assert_called_once()
        assert result is expected

    def test_returns_empty_tensor_for_zero_length(self, monkeypatch):
        """to_tensor_direct() returns an empty tensor when length == 0."""
        wrapper = self._make_wrapper()
        wrapper.length = 0

        with patch(
            "lmcache.v1.platform.rdma.rdma_wrapper.get_rdma_transport",
        ):
            result = wrapper.to_tensor_direct()

        assert result.shape == torch.Size([4, 8])
        assert result.dtype == torch.float16
