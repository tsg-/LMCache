# SPDX-License-Identifier: Apache-2.0
"""NIXL transfer module for LMCache multiprocess server.

Implements :class:`NixlTransferModule`, an :class:`EngineModule` that handles
STORE and RETRIEVE requests by moving KV tensors between host DRAM buffers
via NIXL (UCX or other configured backend).

The server maintains a single :class:`nixl_agent` (``_agent``).  Each
STORE/RETRIEVE call:

  1. Deserializes the :class:`NixlWrapper` from the client.
  2. Loads the client's agent metadata into ``_agent`` (idempotent).
  3. Resolves the token range to storage chunks.
  4. For each chunk, builds per-chunk xfer descriptor lists (local from the
     reserved / read MemoryObj, remote from the wrapper's base_addr + offset).
  5. Calls ``make_prepped_xfer(READ/WRITE)`` + ``transfer()`` + polls until
     ``DONE``.
  6. Releases handles and commits / finishes the write/read.

No CUDA events, no GPU block IDs, and no GPU synchronisation occur here.
"""

from __future__ import annotations

import ctypes
import importlib
import time
from typing import TYPE_CHECKING, Optional

import torch

from lmcache.logging import init_logger
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey
from lmcache.v1.multiprocess.engine_module import EngineModule, HandlerSpec, ThreadPoolType
from lmcache.v1.platform.base_ipc_wrapper import DeviceIPCWrapper
from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper
from lmcache.v1.multiprocess.protocols.base import RequestType

if TYPE_CHECKING:
    from lmcache.v1.multiprocess.engine_context import MPCacheServerContext

logger = init_logger(__name__)

_POLL_INTERVAL_S: float = 0.0001  # 100 µs between check_xfer_state polls
_XFER_TIMEOUT_S: float = 5.0


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


def _per_chunk_shape(full_shape: tuple[int, ...], num_chunks: int) -> torch.Size:
    """Divide a wrapper's leading dimension by ``num_chunks``.

    Args:
        full_shape: The wrapper's full tensor shape.
        num_chunks: Number of equal-sized chunks the range is split into.

    Returns:
        A :class:`torch.Size` with dimension 0 divided by ``num_chunks``.

    Raises:
        ValueError: If dimension 0 is not evenly divisible by ``num_chunks``.
    """
    total = full_shape[0]
    per_chunk, remainder = divmod(total, num_chunks)
    if remainder != 0:
        raise ValueError(
            f"_per_chunk_shape: leading dim {total} is not evenly "
            f"divisible by num_chunks={num_chunks}"
        )
    return torch.Size((per_chunk,) + tuple(full_shape[1:]))


def _compact_strides(shape: torch.Size) -> tuple[int, ...]:
    """Return row-major (C-contiguous) strides for ``shape``.

    Args:
        shape: Tensor shape.

    Returns:
        A tuple of strides where each stride equals the product of all
        dimensions to the right.
    """
    strides: list[int] = []
    s = 1
    for dim in reversed(shape):
        strides.append(s)
        s *= dim
    return tuple(reversed(strides))


def _poll_until_done(agent, xfer_handle, timeout_s: float) -> bool:
    """Poll a NIXL transfer handle until DONE or timeout.

    Args:
        agent: The nixl_agent owning the handle.
        xfer_handle: Handle returned by ``agent.transfer()``.
        timeout_s: Maximum polling time in seconds.

    Returns:
        True if the transfer completed (DONE), False on timeout or error.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = agent.check_xfer_state(xfer_handle)
        if status == "DONE":
            return True
        if status == "ERR":
            return False
        time.sleep(_POLL_INTERVAL_S)
    return False


class NixlTransferModule:
    """Handles STORE and RETRIEVE KV cache transfers over NIXL.

    Maintains a single server-side :class:`nixl_agent` (UCX backend by
    default).  Client agents are registered lazily on first request
    (``add_remote_agent`` is idempotent in NIXL).

    Args:
        ctx: The shared engine context providing storage_manager and
             resolve_obj_keys.
        backends: NIXL backend list to pass to the server agent.
                  Defaults to ``["UCX"]``.
    """

    def __init__(
        self,
        ctx: "MPCacheServerContext",
        backends: Optional[list[str]] = None,
    ) -> None:
        self._ctx = ctx
        nixl_agent_cls, nixl_agent_config_cls = _load_nixl()
        _backends = backends if backends is not None else ["UCX"]
        import os
        self._agent = nixl_agent_cls(
            f"lmcache_server_{os.getpid()}_{id(self)}",
            nixl_agent_config_cls(backends=_backends),
        )
        logger.info(
            "NixlTransferModule initialised (backends=%s)", _backends
        )

    @property
    def context(self) -> "MPCacheServerContext":
        """Return the shared engine context. Exposed for testing only."""
        return self._ctx

    # ------------------------------------------------------------------
    # EngineModule interface
    # ------------------------------------------------------------------

    def get_handlers(self) -> list[HandlerSpec]:
        """Return handler specs for STORE and RETRIEVE request types.

        Returns:
            A two-element list mapping STORE and RETRIEVE to their handlers,
            both in the AFFINITY pool.
        """
        return [
            HandlerSpec(RequestType.STORE, self.store, ThreadPoolType.AFFINITY),
            HandlerSpec(RequestType.RETRIEVE, self.retrieve, ThreadPoolType.AFFINITY),
        ]

    def report_status(self) -> dict:
        """Return module status.

        Returns:
            A dict with key ``"nixl_transfer"`` containing the agent name
            and active backend names.
        """
        return {
            "nixl_transfer": {
                "agent": self._agent.name,
                "backends": list(self._agent.backends.keys()),
            }
        }

    def close(self) -> None:
        """Release module resources.

        The agent is module-owned; currently no explicit teardown is
        needed beyond garbage collection.
        """
        pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_local_xfer_dlist(self, data_ptr: int, chunk_length: int, mem_type: str):
        """Build a single-slot local transfer descriptor list.

        Args:
            data_ptr: Pointer to the start of the local buffer.
            chunk_length: Byte count of the chunk.
            mem_type: NIXL memory type string (``"DRAM"`` or ``"VRAM"``).

        Returns:
            nixlXferDList with one descriptor.
        """
        return self._agent.get_xfer_descs(
            [(data_ptr, chunk_length, 0)], mem_type
        )

    def _build_remote_xfer_dlist(
        self,
        base_addr: int,
        offset: int,
        chunk_length: int,
        device_id: int,
        mem_type: str,
    ):
        """Build a single-slot remote transfer descriptor list.

        Args:
            base_addr: Remote buffer's base data pointer.
            offset: Byte offset of this chunk within the remote buffer.
            chunk_length: Byte count of the chunk.
            device_id: Remote NIXL device ID.
            mem_type: NIXL memory type string.

        Returns:
            nixlXferDList with one descriptor.
        """
        return self._agent.get_xfer_descs(
            [(base_addr + offset, chunk_length, device_id)], mem_type
        )

    def _run_xfer(
        self,
        operation: str,
        local_dlist,
        remote_dlist,
        agent_name: str,
        chunk_idx: int,
        num_chunks: int,
        instance_id: int,
        key: IPCCacheServerKey,
    ) -> bool:
        """Prep, post, and poll a single NIXL transfer.

        Args:
            operation: ``"READ"`` or ``"WRITE"``.
            local_dlist: nixlXferDList for the local side.
            remote_dlist: nixlXferDList for the remote side.
            agent_name: Name of the remote nixl_agent.
            chunk_idx: Chunk index (for logging).
            num_chunks: Total chunks (for logging).
            instance_id: Worker process ID (for logging).
            key: Cache key (for logging).

        Returns:
            True on success, False on timeout or error.
        """
        local_handle = None
        remote_handle = None
        xfer_handle = None
        try:
            local_handle = self._agent.prep_xfer_dlist("", local_dlist)
            remote_handle = self._agent.prep_xfer_dlist(agent_name, remote_dlist)
            xfer_handle = self._agent.make_prepped_xfer(
                operation,
                local_handle, [0],
                remote_handle, [0],
            )
            status = self._agent.transfer(xfer_handle)
            if status == "ERR":
                logger.warning(
                    "_run_xfer: NIXL transfer() returned ERR immediately for "
                    "chunk %d/%d (instance_id=%d key=%s)",
                    chunk_idx, num_chunks, instance_id, key,
                )
                return False
            if status == "DONE":
                return True
            ok = _poll_until_done(self._agent, xfer_handle, _XFER_TIMEOUT_S)
            if not ok:
                logger.warning(
                    "_run_xfer: NIXL %s timed out for chunk %d/%d "
                    "(instance_id=%d key=%s)",
                    operation, chunk_idx, num_chunks, instance_id, key,
                )
            return ok
        finally:
            if xfer_handle is not None:
                try:
                    self._agent.release_xfer_handle(xfer_handle)
                except Exception:
                    logger.exception(
                        "_run_xfer: failed to release xfer handle for chunk %d",
                        chunk_idx,
                    )
            if local_handle is not None:
                try:
                    self._agent.release_dlist_handle(local_handle)
                except Exception:
                    logger.exception(
                        "_run_xfer: failed to release local dlist handle for chunk %d",
                        chunk_idx,
                    )
            if remote_handle is not None:
                try:
                    self._agent.release_dlist_handle(remote_handle)
                except Exception:
                    logger.exception(
                        "_run_xfer: failed to release remote dlist handle for chunk %d",
                        chunk_idx,
                    )

    # ------------------------------------------------------------------
    # Request handlers
    # ------------------------------------------------------------------

    def store(
        self,
        key: IPCCacheServerKey,
        instance_id: int,
        block_ids: list[list[int]],
        nixl_descriptor_bytes: bytes,
    ) -> tuple[bytes, bool]:
        """Pull KV data from the initiator via NIXL READ and store it locally.

        The initiator has registered its KV buffer with its local nixl_agent
        and sent the :class:`NixlWrapper` descriptor over the ZMQ control
        channel.  This handler loads the client agent metadata, allocates
        local storage for each chunk, builds remote xfer dlists from the
        wrapper's raw buffer address, and performs a NIXL READ per chunk.

        Args:
            key: The IPC cache key identifying the token range to store.
            instance_id: Initiator process ID (used for logging only).
            block_ids: GPU block IDs — always ``[]`` for NIXL; ignored.
            nixl_descriptor_bytes: Pickled :class:`NixlWrapper` carrying the
                initiator's NIXL memory descriptor.

        Returns:
            A ``(b"", all_succeeded)`` tuple.  ``all_succeeded`` is ``True``
            only when every chunk completed without error or timeout.

        Raises:
            ValueError: If the deserialized descriptor is not a
                :class:`NixlWrapper`, or if the total length is not
                evenly divisible by the number of chunks.
        """
        wrapper = DeviceIPCWrapper.Deserialize(nixl_descriptor_bytes)
        if not isinstance(wrapper, NixlWrapper):
            raise ValueError(
                f"store: expected NixlWrapper, got {type(wrapper).__name__}"
            )

        # Register the client agent (idempotent — NIXL ignores duplicates).
        self._agent.add_remote_agent(wrapper.agent_metadata)

        obj_key_groups: list[list[ObjectKey]] = self._ctx.resolve_obj_keys(key, [0])
        obj_keys: list[ObjectKey] = obj_key_groups[0]
        num_chunks = len(obj_keys)

        if num_chunks == 0:
            logger.debug(
                "store: instance_id=%d key=%s resolved to 0 chunks, nothing to do",
                instance_id, key,
            )
            return (b"", True)

        chunk_length = wrapper.length // num_chunks
        if wrapper.length != chunk_length * num_chunks:
            raise ValueError(
                f"store: wrapper.length={wrapper.length} is not evenly "
                f"divisible by num_chunks={num_chunks}"
            )

        layout_desc = MemoryLayoutDesc(
            shapes=[_per_chunk_shape(wrapper.shape, num_chunks)],
            dtypes=[wrapper.dtype],
        )

        all_succeeded = True

        for i, obj_key in enumerate(obj_keys):
            reserved: dict[ObjectKey, object] = {}
            success = False

            try:
                reserved = self._ctx.storage_manager.reserve_write(
                    [obj_key], layout_desc, "new"
                )
                mem_obj = reserved[obj_key]
                local_ptr = mem_obj.data_ptr

                # Register local memory with the server agent.
                local_reg = self._agent.register_memory(
                    self._agent.get_reg_descs(
                        [(local_ptr, chunk_length, 0, "")], wrapper.mem_type
                    )
                )

                local_dlist = self._build_local_xfer_dlist(
                    local_ptr, chunk_length, wrapper.mem_type
                )
                remote_dlist = self._build_remote_xfer_dlist(
                    wrapper.base_addr,
                    i * chunk_length,
                    chunk_length,
                    wrapper.device_id,
                    wrapper.mem_type,
                )

                ok = self._run_xfer(
                    "READ",
                    local_dlist, remote_dlist,
                    wrapper.agent_name,
                    i, num_chunks, instance_id, key,
                )
                if not ok:
                    all_succeeded = False
                else:
                    success = True

                try:
                    self._agent.deregister_memory(local_reg)
                except Exception:
                    logger.exception(
                        "store: failed to deregister local memory for chunk %d", i
                    )

            except Exception:
                logger.exception(
                    "store: error on chunk %d/%d (instance_id=%d key=%s)",
                    i, num_chunks, instance_id, key,
                )
                all_succeeded = False
            finally:
                commit_keys = [obj_key] if success else []
                try:
                    self._ctx.storage_manager.finish_write(commit_keys)
                except Exception:
                    logger.exception(
                        "store: finish_write failed for chunk %d (success=%s)",
                        i, success,
                    )

        return (b"", all_succeeded)

    def retrieve(
        self,
        key: IPCCacheServerKey,
        instance_id: int,
        block_ids: list[list[int]],
        nixl_descriptor_bytes: bytes,
        skip_first_n_tokens: int = 0,
    ) -> tuple[bytes, bool]:
        """Push locally stored KV data to the initiator via NIXL WRITE.

        The initiator has allocated and registered a destination buffer with
        its nixl_agent and sent the :class:`NixlWrapper` descriptor over the
        ZMQ control channel.  This handler reads MemoryObjs from the storage
        manager and performs a NIXL WRITE per chunk to push data into the
        initiator's buffer.

        The response is not sent until all NIXL WRITEs complete, so the
        initiator knows its destination buffer is ready upon response receipt.

        Args:
            key: The IPC cache key identifying the token range to retrieve.
            instance_id: Initiator process ID (used for logging only).
            block_ids: GPU block IDs — always ``[]`` for NIXL; ignored.
            nixl_descriptor_bytes: Pickled :class:`NixlWrapper` carrying the
                initiator's destination NIXL descriptor.
            skip_first_n_tokens: Number of initial tokens to skip (passed
                through; not yet implemented for NIXL path).

        Returns:
            A ``(b"", success)`` tuple.  ``success`` is ``True`` only when all
            chunks were found in L1 and all NIXL WRITEs completed without
            timeout or error.

        Raises:
            ValueError: If the deserialized descriptor is not a
                :class:`NixlWrapper`, or if the total length is not evenly
                divisible by the number of resolved chunks.
        """
        wrapper = DeviceIPCWrapper.Deserialize(nixl_descriptor_bytes)
        if not isinstance(wrapper, NixlWrapper):
            raise ValueError(
                f"retrieve: expected NixlWrapper, got {type(wrapper).__name__}"
            )

        # Register the client agent (idempotent).
        self._agent.add_remote_agent(wrapper.agent_metadata)

        obj_key_groups: list[list[ObjectKey]] = self._ctx.resolve_obj_keys(key, [0])
        obj_keys: list[ObjectKey] = obj_key_groups[0]
        num_chunks = len(obj_keys)

        if num_chunks == 0:
            logger.debug(
                "retrieve: instance_id=%d key=%s resolved to 0 chunks",
                instance_id, key,
            )
            return (b"", False)

        chunk_length = wrapper.length // num_chunks
        if wrapper.length != chunk_length * num_chunks:
            raise ValueError(
                f"retrieve: wrapper.length={wrapper.length} is not evenly "
                f"divisible by num_chunks={num_chunks}"
            )

        layout_desc = MemoryLayoutDesc(
            shapes=[_per_chunk_shape(wrapper.shape, num_chunks)],
            dtypes=[wrapper.dtype],
        )

        prefetch_handle = self._ctx.storage_manager.submit_prefetch_task(
            obj_keys, layout_desc, skip_l2=True
        )
        if len(prefetch_handle.l1_found_indices) < num_chunks:
            logger.debug(
                "retrieve: cache miss for key=%s (instance_id=%d, "
                "found=%d/%d chunks)",
                key, instance_id,
                len(prefetch_handle.l1_found_indices), num_chunks,
            )
            if prefetch_handle.l1_found_indices:
                found_keys = [obj_keys[idx] for idx in prefetch_handle.l1_found_indices]
                self._ctx.storage_manager.finish_read_prefetched(found_keys)
            return (b"", False)

        all_succeeded = True

        with self._ctx.storage_manager.read_prefetched_results(obj_keys) as mem_objs:
            if mem_objs is None:
                logger.debug(
                    "retrieve: read_prefetched_results returned None for "
                    "key=%s (instance_id=%d)",
                    key, instance_id,
                )
                return (b"", False)

            for i, mem_obj in enumerate(mem_objs):
                local_ptr = mem_obj.data_ptr

                try:
                    # Register local memory with the server agent.
                    local_reg = self._agent.register_memory(
                        self._agent.get_reg_descs(
                            [(local_ptr, chunk_length, 0, "")], wrapper.mem_type
                        )
                    )

                    local_dlist = self._build_local_xfer_dlist(
                        local_ptr, chunk_length, wrapper.mem_type
                    )
                    remote_dlist = self._build_remote_xfer_dlist(
                        wrapper.base_addr,
                        i * chunk_length,
                        chunk_length,
                        wrapper.device_id,
                        wrapper.mem_type,
                    )

                    ok = self._run_xfer(
                        "WRITE",
                        local_dlist, remote_dlist,
                        wrapper.agent_name,
                        i, num_chunks, instance_id, key,
                    )
                    if not ok:
                        all_succeeded = False

                    try:
                        self._agent.deregister_memory(local_reg)
                    except Exception:
                        logger.exception(
                            "retrieve: failed to deregister local memory for chunk %d", i
                        )

                except Exception:
                    logger.exception(
                        "retrieve: error on chunk %d/%d (instance_id=%d key=%s)",
                        i, num_chunks, instance_id, key,
                    )
                    all_succeeded = False

        if all_succeeded:
            # Release read locks only after all WRITEs have completed so the
            # initiator's destination buffer is populated before this response
            # is sent.
            self._ctx.storage_manager.finish_read_prefetched(obj_keys)

        return (b"", all_succeeded)
