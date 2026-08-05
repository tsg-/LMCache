# SPDX-License-Identifier: Apache-2.0
"""NIXL-backed IPC wrapper for LMCache multiprocess transfers.

Each :meth:`NixlWrapper.wrap` call registers the tensor with the
process-level :class:`nixl_agent` singleton and sends the raw buffer
descriptor (``base_addr``, ``length``, ``device_id``, ``mem_type``) and
``agent_metadata`` to the server.  The server calls ``add_remote_agent``
then uses ``initialize_xfer`` with per-chunk xfer dlists built from the
raw addresses.

This mirrors the pattern in
:mod:`lmcache.v1.distributed.transfer_channel.impl.nixl_impl`: the
initiator always sends a fresh serialized dlist per transfer, so the
server never needs to re-examine remote-agent metadata between requests.

Self-registers a ``"nixl"`` factory with :mod:`lmcache.v1.platform._registry`
at import time via the ``device_type`` ClassVar.
"""

from __future__ import annotations

import importlib
import threading
import weakref
from typing import ClassVar, Optional

import torch

from lmcache.logging import init_logger
from lmcache.v1.platform.base.ipc_wrapper import DeviceIPCWrapper

logger = init_logger(__name__)

# ---------------------------------------------------------------------------
# Process-level nixl agent singleton
# ---------------------------------------------------------------------------

_AGENT_LOCK = threading.Lock()
_AGENT: Optional[object] = None  # nixl_agent instance
_REG_PTRS: dict[int, tuple[weakref.ref, object]] = {}  # data_ptr → (weak tensor, reg_desc)
_REG_LOCK = threading.Lock()


def _load_nixl():
    """Import nixl Python bindings, tolerating the cuXX-suffixed packages."""
    last_err: Optional[Exception] = None
    for modname in ("nixl._api", "nixl_cu12._api", "nixl_cu13._api"):
        try:
            mod = importlib.import_module(modname)
            return mod.nixl_agent, mod.nixl_agent_config
        except ImportError as err:
            last_err = err
    raise RuntimeError(
        "NIXL is not available (tried nixl._api, nixl_cu12._api, nixl_cu13._api)"
    ) from last_err


def get_nixl_agent():
    """Return (creating on first call) the process-level nixl_agent.

    Returns:
        The shared :class:`nixl_agent` instance for this process.
    """
    global _AGENT
    if _AGENT is not None:
        return _AGENT
    with _AGENT_LOCK:
        if _AGENT is None:
            import os
            nixl_agent_cls, nixl_agent_config_cls = _load_nixl()
            _AGENT = nixl_agent_cls(
                f"lmcache_worker_{os.getpid()}",
                nixl_agent_config_cls(backends=["UCX"]),
            )
            logger.info(
                "NixlWrapper: created process-level nixl_agent %s",
                _AGENT.name,
            )
    return _AGENT


def _deregister_on_gc(data_ptr: int, reg_desc: object) -> None:
    """Finalizer: deregister the MR and remove the stale cache entry.

    The GC finalizer may run while a NIXL transfer is in flight only if
    the tensor was collected before :meth:`NixlWrapper.wrap` retains a
    reference to it.  Since :meth:`wrap` is called in the same scope as
    the tensor and the future is not resolved until the server completes
    the NIXL READ, the tensor must remain alive until the future resolves.
    Callers are responsible for keeping the tensor alive long enough.

    We deregister here so that if the allocator reuses the same address
    for a new tensor, the new registration does not conflict with the stale
    one in the worker's UCX context.
    """
    with _REG_LOCK:
        _REG_PTRS.pop(data_ptr, None)
    agent = _AGENT
    if agent is not None:
        try:
            agent.deregister_memory(reg_desc)
        except Exception:
            pass


def _ensure_registered(tensor: torch.Tensor) -> object:
    """Register ``tensor``'s backing memory with the process-level agent.

    Idempotent: re-uses the existing registration if the tensor's data_ptr
    has already been registered and the tensor is still alive.

    Args:
        tensor: A contiguous tensor whose memory should be registered.

    Returns:
        The nixlRegDList for the registered region (new or cached).
    """
    if not tensor.is_contiguous():
        raise ValueError("NixlWrapper requires a contiguous tensor")

    data_ptr = tensor.data_ptr()
    nbytes = tensor.numel() * tensor.element_size()
    agent = get_nixl_agent()

    stale_reg_desc = None

    with _REG_LOCK:
        entry = _REG_PTRS.get(data_ptr)
        if entry is not None:
            ref, old_reg_desc = entry
            if ref() is tensor:
                return old_reg_desc
            # Stale entry: save the old reg_desc so we can deregister it
            # synchronously BEFORE registering the new tensor at the same
            # address.  Without this, calling register_memory twice on the
            # same address creates a double-registration that can be
            # invalidated when the GC finalizer deregisters the old one,
            # corrupting the new registration mid-transfer.
            _REG_PTRS.pop(data_ptr, None)
            stale_reg_desc = old_reg_desc

    # Deregister the stale MR outside the lock so UCX can complete the
    # operation without holding _REG_LOCK.
    if stale_reg_desc is not None:
        try:
            agent.deregister_memory(stale_reg_desc)
        except Exception:
            pass

    with _REG_LOCK:
        # Re-check after dropping the lock: another thread might have
        # registered the same address.
        entry = _REG_PTRS.get(data_ptr)
        if entry is not None:
            ref, reg_desc = entry
            if ref() is tensor:
                return reg_desc
            # Still stale — this shouldn't happen in single-threaded usage.
            _REG_PTRS.pop(data_ptr, None)

        device_id = max(tensor.get_device(), 0)
        mem_type = "DRAM" if tensor.get_device() == -1 else "VRAM"
        reg_descs = agent.get_reg_descs(
            [(data_ptr, nbytes, device_id, "")], mem_type
        )
        agent.register_memory(reg_descs)
        _REG_PTRS[data_ptr] = (weakref.ref(tensor), reg_descs)
        weakref.finalize(tensor, _deregister_on_gc, data_ptr, reg_descs)

        logger.debug(
            "NixlWrapper: registered %s tensor: addr=0x%x nbytes=%d",
            mem_type, data_ptr, nbytes,
        )
        return reg_descs


# ---------------------------------------------------------------------------
# Wrapper class
# ---------------------------------------------------------------------------

class NixlWrapper(DeviceIPCWrapper):
    """IPC wrapper for NIXL-based KV-cache transfers.

    The initiator registers its KV buffer with the process-level
    :class:`nixl_agent`, builds a transfer descriptor list (xfer dlist)
    covering the tensor, and serializes both the agent metadata and the
    xfer dlist into this wrapper.

    The server:
    1. Calls ``add_remote_agent(agent_metadata)`` to learn how to reach the
       worker's UCX endpoint (idempotent after first call).
    2. Builds a per-chunk remote xfer dlist from ``base_addr + chunk_offset``.
    3. Calls ``initialize_xfer(operation, local_dlist, remote_dlist, agent_name)``
       — the combined one-shot API that avoids UCX endpoint state issues from
       the prep-based path across sequential cross-process requests.
    4. Posts a NIXL READ (STORE) or NIXL WRITE (RETRIEVE) per chunk.

    Attributes:
        agent_name: Name of the initiator's nixl_agent.
        agent_metadata: Full metadata bytes from
            ``agent.get_agent_metadata()``.
        base_addr: Data pointer of the initiator's KV buffer.
        length: Total byte count of the described buffer.
        device_id: NIXL device ID (0 for DRAM, GPU id for VRAM).
        mem_type: NIXL memory type string (``"DRAM"`` or ``"VRAM"``).
        shape: Tensor shape for layout reconstruction on the server.
        dtype: Tensor dtype.
        stride: Tensor strides.
        storage_offset: Tensor storage offset.
    """

    device_type: ClassVar[str] = "nixl"

    @classmethod
    def wrap(cls, tensor: torch.Tensor) -> "NixlWrapper":
        """Register the tensor's backing memory with NIXL and create a wrapper.

        The server uses ``base_addr`` and ``device_id`` to build per-chunk
        remote xfer dlists and calls ``initialize_xfer`` to post transfers.

        Args:
            tensor: A contiguous tensor in CPU DRAM or GPU VRAM.

        Returns:
            A new :class:`NixlWrapper` carrying the NIXL descriptor.
        """
        if not tensor.is_contiguous():
            raise ValueError("NixlWrapper requires a contiguous tensor")

        _ensure_registered(tensor)
        agent = get_nixl_agent()
        # Drive UCX progress to flush any pending operations (e.g., MR
        # deregistrations from prior GC finalizers) before the next transfer.
        try:
            agent.get_new_notifs()
        except Exception:
            pass

        data_ptr = tensor.data_ptr()
        nbytes = tensor.numel() * tensor.element_size()
        device_id = max(tensor.get_device(), 0)
        mem_type = "DRAM" if tensor.get_device() == -1 else "VRAM"

        return cls(
            agent_name=agent.name,
            agent_metadata=agent.get_agent_metadata(),
            base_addr=data_ptr,
            length=nbytes,
            device_id=device_id,
            mem_type=mem_type,
            shape=tuple(tensor.shape),
            dtype=tensor.dtype,
            stride=tuple(tensor.stride()),
            storage_offset=int(tensor.storage_offset()),
        )

    def __init__(
        self,
        agent_name: str,
        agent_metadata: bytes,
        base_addr: int,
        length: int,
        device_id: int,
        mem_type: str,
        shape: tuple[int, ...],
        dtype: torch.dtype,
        stride: tuple[int, ...],
        storage_offset: int,
    ) -> None:
        self.agent_name = agent_name
        self.agent_metadata = agent_metadata
        self.base_addr = base_addr
        self.length = length
        self.device_id = device_id
        self.mem_type = mem_type
        self.shape = shape
        self.dtype = dtype
        self.stride = stride
        self.storage_offset = storage_offset

        # DeviceIPCWrapper expects a .handle attribute.
        self.handle = (agent_name, base_addr, length)

    def to_tensor(self) -> torch.Tensor:
        """Not supported for NixlWrapper.

        NIXL transfers are always server-initiated.  The initiator never
        needs to materialize a tensor from its own descriptor.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError(
            "NixlWrapper.to_tensor() is not supported; "
            "NIXL transfers are initiated by the server, not the worker."
        )
