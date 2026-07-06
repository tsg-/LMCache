# SPDX-License-Identifier: Apache-2.0
"""IPU RDMA KV-cache IPC wrapper.

Mirrors the GPU-mode CUDA-IPC and CPU-mode POSIX-SHM zero-copy semantics
for inter-host transfer via RDMA.  The initiator's IPU serves RDMA Read
responses from registered DRAM; the target's IPU posts RDMA Reads to pull
KV pages.  CPU never touches data bytes — only control plane.

Self-registers an ``"ipu"`` factory with
:mod:`lmcache.v1.platform._registry` at import time via the
``device_type`` ClassVar, so the multiprocess adapter dispatches by
``tensor.device.type`` without any if/elif chain.
"""

from __future__ import annotations

from typing import ClassVar
import ctypes
import threading
import weakref

import torch

from lmcache.logging import init_logger
from lmcache.v1.platform.base_ipc_wrapper import DeviceIPCWrapper
from lmcache.v1.platform.ipu.rdma_transport import (
    MrInfo,
    RegisteredBuffer,
    get_rdma_transport,
)

logger = init_logger(__name__)


_REGISTERED_MRS: dict[int, tuple[weakref.ref, MrInfo]] = {}
_MR_LOCK = threading.Lock()


def _deregister_on_gc(data_ptr: int, mr: MrInfo) -> None:
    """Weak-reference finalizer: deregister MR when tensor is collected."""
    with _MR_LOCK:
        entry = _REGISTERED_MRS.get(data_ptr)
        if entry is not None:
            _, registered_mr = entry
            if registered_mr is mr:
                _REGISTERED_MRS.pop(data_ptr, None)
    if mr.deregister is not None:
        try:
            mr.deregister()
        except Exception:
            pass


class IPURdmaWrapper(DeviceIPCWrapper):
    """IPC wrapper for IPU-registered DRAM exposed via RDMA.

    Used in the symmetric IPU deployment where both initiator and target
    have an IPU.  The initiator registers its KV buffer as an RDMA MR
    (via ``wrap()``), and the target pulls data via RDMA Read (via
    ``to_tensor()``).

    The wrapper is serialized across the ZMQ control channel (via pickle,
    inherited from DeviceIPCWrapper).  Only the descriptor travels over
    ZMQ; actual KV data moves via RDMA on the IPU data plane.
    """

    device_type: ClassVar[str] = "ipu"
    _is_default_wrapper: ClassVar[bool] = True

    @classmethod
    def wrap(cls, tensor: torch.Tensor) -> "IPURdmaWrapper":
        """Register the tensor's backing memory as an RDMA MR and wrap.

        Args:
            tensor: A contiguous tensor in IPU-registered host DRAM.

        Returns:
            A new IPURdmaWrapper carrying the RDMA descriptor.
        """
        if not tensor.is_contiguous():
            raise ValueError("IPURdmaWrapper requires a contiguous tensor")

        data_ptr = tensor.data_ptr()
        nbytes = tensor.numel() * tensor.element_size()

        mr_info = cls._get_or_register_mr(tensor, data_ptr, nbytes)
        return cls(tensor, mr_info)

    @classmethod
    def _get_or_register_mr(
        cls, tensor: torch.Tensor, data_ptr: int, nbytes: int
    ) -> MrInfo:
        """Look up or register an MR for the given data pointer."""
        with _MR_LOCK:
            entry = _REGISTERED_MRS.get(data_ptr)
            if entry is not None:
                ref, mr = entry
                if ref() is tensor:
                    return mr
                _REGISTERED_MRS.pop(data_ptr, None)

            transport = get_rdma_transport()
            mr = transport.register_mr(data_ptr, nbytes)
            _REGISTERED_MRS[data_ptr] = (weakref.ref(tensor), mr)
            weakref.finalize(tensor, _deregister_on_gc, data_ptr, mr)
            logger.debug(
                "Registered RDMA MR: rkey=%d addr=0x%x len=%d",
                mr.rkey, mr.addr, mr.length,
            )
            return mr

    def __init__(self, tensor: torch.Tensor, mr_info: MrInfo) -> None:
        self.rkey = mr_info.rkey
        self.remote_addr = mr_info.addr
        self.length = mr_info.length

        self.handle = (self.rkey, self.remote_addr, self.length)
        self.dtype = tensor.dtype
        self.shape = tuple(tensor.shape)
        self.stride = tuple(tensor.stride())
        self.storage_offset = int(tensor.storage_offset())
        self.device_uuid = "ipu"

    def to_tensor(self) -> torch.Tensor:
        """Pull data from the remote MR via RDMA Read.

        Allocates a local registered buffer, posts an RDMA Read to copy
        data from the remote address (initiator's IPU-registered DRAM)
        into the local buffer, and returns the buffer as a tensor.
        """
        if self.length == 0:
            return torch.empty(self.shape, dtype=self.dtype)

        transport = get_rdma_transport()
        buf = transport.allocate_buffer(self.length)

        future = transport.post_read(
            local_buf=buf,
            remote_addr=self.remote_addr,
            rkey=self.rkey,
            length=self.length,
        )

        if not transport.poll_completion(future, timeout_ms=5000):
            flushed = transport.drain_on_timeout()
            if flushed:
                transport.free_buffer(buf)
            else:
                transport.release_buffer_tracking(buf)
                logger.error(
                    "RDMA buffer quarantined (leak): addr=0x%x len=%d",
                    buf.addr, buf.length,
                )
            raise RuntimeError(
                f"RDMA Read timed out: rkey={self.rkey} "
                f"remote_addr=0x{self.remote_addr:x} length={self.length}"
            )

        # Transfer buffer ownership from transport to tensor
        transport.release_buffer_tracking(buf)

        buf_type = ctypes.c_uint8 * self.length
        c_buf = buf_type.from_address(buf.addr)
        flat = torch.frombuffer(c_buf, dtype=torch.uint8)
        typed = flat.view(self.dtype)
        out = torch.as_strided(typed, self.shape, self.stride, self.storage_offset)

        # Pin buffer lifetime to tensor storage via captured callback
        storage = out.untyped_storage()
        free_fn = transport.free_buffer
        weakref.finalize(storage, free_fn, buf)

        return out
