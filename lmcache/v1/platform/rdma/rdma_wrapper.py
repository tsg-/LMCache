# SPDX-License-Identifier: Apache-2.0
"""RDMA KV-cache IPC wrapper.

Mirrors the GPU-mode CUDA-IPC and CPU-mode POSIX-SHM zero-copy semantics
for inter-host transfer via RDMA.  The initiator serves RDMA Read responses
from registered DRAM; the target posts RDMA Reads to pull KV pages.  CPU
never touches data bytes — only control plane.

Self-registers an ``"rdma"`` factory with
:mod:`lmcache.v1.platform._registry` at import time via the
``device_type`` ClassVar, so the multiprocess adapter dispatches by
``tensor.device.type`` without any if/elif chain.
"""

from __future__ import annotations

from typing import ClassVar
import ctypes
import itertools
import os
import threading
import weakref

import torch

from lmcache.logging import init_logger
from lmcache.v1.platform.base_ipc_wrapper import DeviceIPCWrapper
from lmcache.v1.platform.rdma.rdma_transport import (
    MrInfo,
    RegisteredBuffer,
    StubRdmaTransport,
    get_rdma_transport,
)

logger = init_logger(__name__)


_REGISTERED_MRS: dict[int, tuple[weakref.ref, MrInfo]] = {}
_MR_LOCK = threading.Lock()

# SHM migration state for stub backend cross-process support.
_SHM_COUNTER = itertools.count()
_SHM_PREFIX = "/lmcache_rdma_"
# Keyed by id(tensor) → (weakref to tensor, shm_name).
_SHM_MIGRATED: dict[int, tuple[weakref.ref, str]] = {}
_SHM_MIGRATED_LOCK = threading.Lock()


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


class RdmaWrapper(DeviceIPCWrapper):
    """IPC wrapper for RDMA-registered DRAM.

    Used in the symmetric RDMA deployment where both initiator and target
    have a RoCEv2 NIC.  The initiator registers its KV buffer as an RDMA MR
    (via ``wrap()``), and the target pulls data via RDMA Read (via
    ``to_tensor()``).

    The wrapper is serialized across the ZMQ control channel (via pickle,
    inherited from DeviceIPCWrapper).  Only the descriptor travels over
    ZMQ; actual KV data moves via RDMA on the data plane.
    """

    device_type: ClassVar[str] = "rdma"
    _is_default_wrapper: ClassVar[bool] = True

    @classmethod
    def wrap(cls, tensor: torch.Tensor) -> "RdmaWrapper":
        """Register the tensor's backing memory as an RDMA MR and wrap.

        When the active transport is :class:`StubRdmaTransport`, the
        tensor's storage is first migrated to a POSIX SHM segment so that
        a remote process can map the same physical pages.  The SHM segment
        name is stored in the wrapper and travels across ZMQ so the server
        can call :meth:`StubRdmaTransport.map_remote_mr`.

        Args:
            tensor: A contiguous tensor in RDMA-registered host DRAM.

        Returns:
            A new RdmaWrapper carrying the RDMA descriptor.
        """
        if not tensor.is_contiguous():
            raise ValueError("RdmaWrapper requires a contiguous tensor")

        transport = get_rdma_transport()
        shm_name: str = ""

        if isinstance(transport, StubRdmaTransport):
            tensor, shm_name = cls._migrate_to_shm(tensor)

        data_ptr = tensor.data_ptr()
        nbytes = tensor.numel() * tensor.element_size()

        mr_info = cls._get_or_register_mr(tensor, data_ptr, nbytes)
        wrapper = cls(tensor, mr_info)
        wrapper.shm_name = shm_name
        return wrapper

    @classmethod
    def _migrate_to_shm(cls, tensor: torch.Tensor) -> tuple[torch.Tensor, str]:
        """Migrate tensor storage to a named POSIX SHM segment.

        Idempotent: if the same tensor object has already been migrated,
        returns the existing SHM name without re-creating the segment.

        Returns the (now SHM-backed) tensor and the SHM segment name.
        """
        from lmcache.v1.multiprocess.posix_shm import (
            shm_create_readwrite,
            shm_munmap,
            shm_unlink,
        )

        nbytes = tensor.numel() * tensor.element_size()
        if nbytes == 0:
            return tensor, ""

        tid = id(tensor)
        with _SHM_MIGRATED_LOCK:
            cached = _SHM_MIGRATED.get(tid)
            if cached is not None:
                ref, cached_name = cached
                if ref() is tensor:
                    return tensor, cached_name
                _SHM_MIGRATED.pop(tid, None)

        shm_name = "%s%d_%d" % (_SHM_PREFIX, os.getpid(), next(_SHM_COUNTER))
        addr = shm_create_readwrite(shm_name, nbytes)
        try:
            buf_type = ctypes.c_uint8 * nbytes
            buf = buf_type.from_address(addr)
            # Copy existing tensor data into the SHM segment before
            # re-pointing the storage, so the migration is non-destructive.
            ctypes.memmove(addr, tensor.data_ptr(), nbytes)
            shm_storage = torch.frombuffer(buf, dtype=torch.uint8).untyped_storage()
            tensor.set_(
                shm_storage,
                tensor.storage_offset(),
                tensor.shape,
                tensor.stride(),
            )
        except Exception:
            shm_munmap(addr, nbytes)
            shm_unlink(shm_name)
            raise

        with _SHM_MIGRATED_LOCK:
            _SHM_MIGRATED[tid] = (weakref.ref(tensor), shm_name)

        weakref.finalize(
            tensor,
            cls._cleanup_shm,
            tid,
            shm_name,
            addr,
            nbytes,
        )
        logger.debug(
            "RdmaWrapper: migrated tensor (nbytes=%d) to SHM %s",
            nbytes, shm_name,
        )
        return tensor, shm_name

    @staticmethod
    def _cleanup_shm(tid: int, shm_name: str, addr: int, nbytes: int) -> None:
        """Release SHM segment when the migrated tensor is collected."""
        from lmcache.v1.multiprocess.posix_shm import shm_munmap, shm_unlink

        with _SHM_MIGRATED_LOCK:
            cached = _SHM_MIGRATED.get(tid)
            if cached is not None and cached[1] == shm_name:
                _SHM_MIGRATED.pop(tid, None)
        shm_munmap(addr, nbytes)
        shm_unlink(shm_name)

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
        self.device_uuid = "rdma"
        # SHM segment name for stub cross-process support; empty string
        # when using real verbs backend.
        self.shm_name: str = ""

    def to_tensor(self) -> torch.Tensor:
        """Pull data from the remote MR via RDMA Read.

        Allocates a local registered buffer, posts an RDMA Read to copy
        data from the remote address (initiator's RDMA-registered DRAM)
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


# Backward-compat alias for deserialization of pickled objects from prior versions.
IPURdmaWrapper = RdmaWrapper
