# SPDX-License-Identifier: Apache-2.0
"""NIXL-backed IPC wrapper for LMCache multiprocess transfers.

Each :meth:`NixlWrapper.wrap` call registers the tensor with the
process-level :class:`nixl_agent` singleton, builds a transfer descriptor
list (xfer dlist) covering the tensor, and serializes it.  The server
receives ``agent_metadata`` (for the first ``add_remote_agent`` call),
``serialized_xfer_descs`` (the per-tensor dlist, freshly built on every
wrap), and ``agent_name`` (to route ``prep_xfer_dlist`` calls).

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
from lmcache.v1.platform.base_ipc_wrapper import DeviceIPCWrapper

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
            nixl_agent_cls, nixl_agent_config_cls = _load_nixl()
            import os
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
    """Finalizer: deregister memory region when tensor is collected."""
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

    with _REG_LOCK:
        entry = _REG_PTRS.get(data_ptr)
        if entry is not None:
            ref, reg_desc = entry
            if ref() is tensor:
                return reg_desc
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
       worker's UCX endpoint (idempotent after the first call).
    2. Calls ``deserialize_descs(serialized_xfer_descs)`` to get the
       per-tensor xfer dlist.
    3. Calls ``prep_xfer_dlist(agent_name, xfer_dlist)`` — this succeeds
       even on subsequent calls because the dlist carries the exact buffer
       addresses; NIXL does not re-examine the remote agent's registered
       memory list.
    4. Posts a NIXL READ (STORE) or NIXL WRITE (RETRIEVE) per chunk.

    Attributes:
        agent_name: Name of the initiator's nixl_agent.
        agent_metadata: Full metadata bytes from
            ``agent.get_agent_metadata()``.
        serialized_xfer_descs: Pickled nixlXferDList covering the tensor.
        shape: Tensor shape for layout reconstruction on the server.
        dtype: Tensor dtype.
        length: Total byte count of the described buffer.
        stride: Tensor strides.
        storage_offset: Tensor storage offset.
    """

    device_type: ClassVar[str] = "nixl"

    @classmethod
    def wrap(cls, tensor: torch.Tensor) -> "NixlWrapper":
        """Register the tensor with NIXL and create a per-tensor wrapper.

        Builds a fresh xfer dlist on every call so the server can call
        ``prep_xfer_dlist(agent_name, dlist)`` without needing updated
        remote-agent metadata.

        Args:
            tensor: A contiguous tensor in CPU DRAM or GPU VRAM.

        Returns:
            A new :class:`NixlWrapper` carrying the NIXL descriptor.
        """
        if not tensor.is_contiguous():
            raise ValueError("NixlWrapper requires a contiguous tensor")

        _ensure_registered(tensor)
        agent = get_nixl_agent()

        data_ptr = tensor.data_ptr()
        nbytes = tensor.numel() * tensor.element_size()
        device_id = max(tensor.get_device(), 0)
        mem_type = "DRAM" if tensor.get_device() == -1 else "VRAM"

        # Build a fresh xfer dlist for this tensor — one descriptor covering
        # the whole buffer.  The server slices it by index per chunk.
        xfer_dlist = agent.get_xfer_descs(
            [(data_ptr, nbytes, device_id)], mem_type
        )
        serialized_xfer_descs = agent.get_serialized_descs(xfer_dlist)

        return cls(
            agent_name=agent.name,
            agent_metadata=agent.get_agent_metadata(),
            serialized_xfer_descs=serialized_xfer_descs,
            shape=tuple(tensor.shape),
            dtype=tensor.dtype,
            length=nbytes,
            stride=tuple(tensor.stride()),
            storage_offset=int(tensor.storage_offset()),
        )

    def __init__(
        self,
        agent_name: str,
        agent_metadata: bytes,
        serialized_xfer_descs: bytes,
        shape: tuple[int, ...],
        dtype: torch.dtype,
        length: int,
        stride: tuple[int, ...],
        storage_offset: int,
    ) -> None:
        self.agent_name = agent_name
        self.agent_metadata = agent_metadata
        self.serialized_xfer_descs = serialized_xfer_descs
        self.shape = shape
        self.dtype = dtype
        self.length = length
        self.stride = stride
        self.storage_offset = storage_offset

        # DeviceIPCWrapper expects a .handle attribute.
        self.handle = (agent_name, len(serialized_xfer_descs), length)

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
