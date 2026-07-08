# SPDX-License-Identifier: Apache-2.0
"""NIXL-backed IPC wrapper for LMCache multiprocess transfers.

Carries the initiator's NIXL agent metadata and raw buffer descriptor
(base_addr, length, device_id, mem_type) from the worker to the server.
The server uses these to:
  1. Register the client agent via ``add_remote_agent(agent_metadata)``.
  2. Build per-chunk remote xfer dlists from ``(base_addr + offset, chunk_length, device_id)``.
  3. Pull (STORE) or push (RETRIEVE) each chunk via ``make_prepped_xfer`` + ``transfer``.

No serialized nixlXferDList crosses the wire — only the raw memory descriptor,
which the server turns into per-chunk dlists locally.  This mirrors how
:class:`RdmaWrapper` carries ``(rkey, remote_addr, length)`` without serializing
the MR structure.

A process-level :class:`nixl_agent` singleton is created on first
``NixlWrapper.wrap()`` call and reused across all wraps in the same process.

Self-registers a ``"nixl"`` factory with :mod:`lmcache.v1.platform._registry`
at import time via the ``device_type`` ClassVar.
"""

from __future__ import annotations

import ctypes
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
_AGENT_REG_DESCS: Optional[object] = None  # nixlRegDList for _REG_PTRS regions
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
        The nixlRegDList for the registered region.
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

    The initiator registers its KV buffer with the process-level nixl_agent
    and sends the raw buffer descriptor (base_addr, length, device_id,
    mem_type) alongside its agent metadata.  The server builds per-chunk
    remote xfer dlists from these fields and initiates a NIXL READ (STORE) or
    NIXL WRITE (RETRIEVE) for each chunk.

    The wrapper is serialized across the ZMQ control channel (via pickle,
    inherited from DeviceIPCWrapper).  Actual KV data moves via NIXL on the
    data plane.

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

        Sends partial agent metadata (only this tensor's registration entry +
        connection info) so the server can locate the new tensor even after
        the agent already exists in its registry from a prior wrap() call.

        Args:
            tensor: A contiguous tensor in CPU DRAM or GPU VRAM.

        Returns:
            A new :class:`NixlWrapper` carrying the NIXL descriptor.
        """
        if not tensor.is_contiguous():
            raise ValueError("NixlWrapper requires a contiguous tensor")

        reg_descs = _ensure_registered(tensor)
        agent = get_nixl_agent()

        agent_name = agent.name
        # Use partial metadata scoped to this tensor so the server can add or
        # update the remote descriptor even when the agent is already known.
        agent_metadata = agent.get_partial_agent_metadata(
            reg_descs, inc_conn_info=True
        )
        base_addr = tensor.data_ptr()
        nbytes = tensor.numel() * tensor.element_size()
        device_id = max(tensor.get_device(), 0)
        mem_type = "DRAM" if tensor.get_device() == -1 else "VRAM"

        return cls(
            agent_name=agent_name,
            agent_metadata=agent_metadata,
            base_addr=base_addr,
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
