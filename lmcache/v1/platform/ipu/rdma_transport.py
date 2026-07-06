# SPDX-License-Identifier: Apache-2.0
"""RDMA transport abstraction for IPU-driven KV cache transfers.

Defines the :class:`RdmaTransport` protocol and a stub implementation
that uses local memory copies for testing without RDMA hardware.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable
import ctypes
import threading

import torch

from lmcache.logging import init_logger

logger = init_logger(__name__)


class Closeable(Protocol):
    """Protocol for objects that support close()."""

    def close(self) -> None: ...


@dataclass(frozen=True)
class MrInfo:
    """Memory region descriptor returned by :meth:`RdmaTransport.register_mr`."""

    rkey: int
    addr: int
    length: int
    handle: object
    deregister: Callable[[], None] | None = None


@dataclass(frozen=True)
class RegisteredBuffer:
    """A buffer allocated and registered for RDMA access."""

    addr: int
    length: int
    mr: MrInfo
    backing: object = None


class RdmaFuture:
    """Opaque completion token for an in-flight RDMA operation."""

    def __init__(self) -> None:
        self._done = threading.Event()
        self._success: bool = False

    def set_complete(self, success: bool = True) -> None:
        self._success = success
        self._done.set()

    def wait(self, timeout_ms: int = 5000) -> bool:
        return self._done.wait(timeout=timeout_ms / 1000.0) and self._success


@runtime_checkable
class RdmaTransport(Protocol):
    """Protocol for RDMA verb operations.

    Concrete implementations wrap libibverbs (or the IPU SDK equivalent).
    The stub implementation uses memcpy for local testing.
    """

    def register_mr(self, buffer_ptr: int, length: int) -> MrInfo:
        """Register a memory region for RDMA access."""
        ...

    def deregister_mr(self, mr: MrInfo) -> None:
        """Deregister a previously registered memory region."""
        ...

    def post_read(
        self,
        local_buf: RegisteredBuffer,
        remote_addr: int,
        rkey: int,
        length: int,
    ) -> RdmaFuture:
        """Post an RDMA Read work request."""
        ...

    def poll_completion(self, future: RdmaFuture, timeout_ms: int = 5000) -> bool:
        """Wait for an RDMA operation to complete."""
        ...

    def allocate_buffer(self, length: int) -> RegisteredBuffer:
        """Allocate a buffer pre-registered for RDMA access."""
        ...

    def free_buffer(self, buf: RegisteredBuffer) -> None:
        """Free a previously allocated registered buffer."""
        ...

    def release_buffer_tracking(self, buf: RegisteredBuffer) -> None:
        """Transfer buffer ownership from transport to caller."""
        ...

    def drain_on_timeout(self) -> bool:
        """Drain after a timeout. Returns True if all WRs confirmed flushed."""
        ...


class _CtypesBackingWrapper:
    """Wrapper giving ctypes arrays a close() method for RegisteredBuffer."""

    def __init__(self, buf: ctypes.Array) -> None:
        self._buf = buf

    def close(self) -> None:
        self._buf = None


class StubRdmaTransport:
    """Local-memory stub for testing without RDMA hardware.

    Simulates RDMA Read by doing a direct memcpy from the source address
    to the destination address within the same process (or across processes
    sharing the same address space via mmap).
    """

    def __init__(self) -> None:
        self._registered: dict[int, MrInfo] = {}
        self._next_rkey = 1
        self._lock = threading.Lock()

    def register_mr(self, buffer_ptr: int, length: int) -> MrInfo:
        with self._lock:
            rkey = self._next_rkey
            self._next_rkey += 1
        mr = MrInfo(
            rkey=rkey, addr=buffer_ptr, length=length, handle=None,
            deregister=lambda: self._registered.pop(rkey, None),
        )
        self._registered[rkey] = mr
        logger.debug("StubRDMA: registered MR rkey=%d addr=0x%x len=%d", rkey, buffer_ptr, length)
        return mr

    def deregister_mr(self, mr: MrInfo) -> None:
        if mr.deregister is not None:
            mr.deregister()
        else:
            self._registered.pop(mr.rkey, None)

    def post_read(
        self,
        local_buf: RegisteredBuffer,
        remote_addr: int,
        rkey: int,
        length: int,
    ) -> RdmaFuture:
        future = RdmaFuture()
        ctypes.memmove(local_buf.addr, remote_addr, length)
        future.set_complete(success=True)
        logger.debug(
            "StubRDMA: read %d bytes from 0x%x (rkey=%d) to 0x%x",
            length, remote_addr, rkey, local_buf.addr,
        )
        return future

    def poll_completion(self, future: RdmaFuture, timeout_ms: int = 5000) -> bool:
        return future.wait(timeout_ms=timeout_ms)

    def allocate_buffer(self, length: int) -> RegisteredBuffer:
        buf = (ctypes.c_uint8 * length)()
        addr = ctypes.addressof(buf)
        mr = self.register_mr(addr, length)
        mr = MrInfo(
            rkey=mr.rkey, addr=addr, length=length, handle=buf,
            deregister=lambda: self._registered.pop(mr.rkey, None),
        )
        return RegisteredBuffer(
            addr=addr, length=length, mr=mr,
            backing=_CtypesBackingWrapper(buf),
        )

    def free_buffer(self, buf: RegisteredBuffer) -> None:
        self.deregister_mr(buf.mr)
        if buf.backing is not None:
            buf.backing.close()

    def release_buffer_tracking(self, buf: RegisteredBuffer) -> None:
        pass

    def drain_on_timeout(self) -> bool:
        return True


_global_transport: RdmaTransport | None = None
_transport_lock = threading.Lock()


def get_rdma_transport() -> RdmaTransport:
    """Return the process-global RDMA transport instance.

    On first call, checks for a real RDMA backend (via LMCACHE_RDMA_TRANSPORT
    env var). Falls back to StubRdmaTransport if no hardware backend is
    configured.
    """
    global _global_transport
    if _global_transport is not None:
        return _global_transport

    with _transport_lock:
        if _global_transport is not None:
            return _global_transport

        import os

        backend = os.environ.get("LMCACHE_RDMA_TRANSPORT", "stub")
        if backend == "stub":
            logger.info("Using StubRdmaTransport (no RDMA hardware)")
            _global_transport = StubRdmaTransport()
        elif backend == "verbs":
            from lmcache.v1.platform.ipu.verbs_transport import (
                VerbsRdmaTransport,
            )
            _global_transport = VerbsRdmaTransport.from_env()
        else:
            raise ValueError(
                f"Unknown RDMA transport backend: {backend!r}. "
                "Available: 'stub', 'verbs'."
            )

    return _global_transport


def set_rdma_transport(transport: RdmaTransport) -> None:
    """Override the global RDMA transport (for testing or hardware injection)."""
    global _global_transport
    with _transport_lock:
        _global_transport = transport
