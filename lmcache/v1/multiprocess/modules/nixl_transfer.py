# SPDX-License-Identifier: Apache-2.0
"""NIXL transfer module for LMCache multiprocess server.

Implements :class:`NixlTransferModule`, an :class:`EngineModule` that handles
STORE and RETRIEVE requests by moving KV tensors between host DRAM buffers
via NIXL (UCX or other configured backend).

The server maintains a single :class:`nixl_agent` (``_agent``).  Each
STORE/RETRIEVE call:

  1. Deserializes the :class:`NixlWrapper` from the client.
  2. Calls ``add_remote_agent(wrapper.agent_metadata)`` (idempotent after
     first call — this registers the worker's UCX endpoint).
  3. Builds per-chunk local and remote xfer dlists from raw addresses.
  4. Calls ``initialize_xfer(operation, local_dlist, remote_dlist, agent_name)``
     — the combined one-shot API that avoids UCX endpoint state issues that
     the ``prep_xfer_dlist`` + ``make_prepped_xfer`` path can encounter on
     the second request when the remote UCX endpoint state has changed.
  5. Calls ``transfer()`` + polls.
  7. Releases handles and commits / finishes the write/read.

No CUDA events, no GPU block IDs, and no GPU synchronisation occur here.
"""

from __future__ import annotations

import ctypes
import importlib
import mmap
import threading
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
_XFER_TIMEOUT_S: float = 15.0
_POOL_PAGE_SIZE: int = 256 * 1024          # 256 KB — matches verbs_transport pool page
_POOL_DEFAULT_SIZE: int = 256 * 1024 * 1024  # 256 MB arena default


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


class _NixlBufferPool:
    """Pre-registered DRAM arena for zero-registration NIXL local buffers.

    Allocates one large mmap arena, registers it once with the NIXL agent,
    then services ``acquire()`` by carving 256 KB aligned pages from a
    freelist.  ``release()`` returns pages to the freelist without any NIXL
    call.  Requests larger than the page size bypass the pool entirely.

    Thread-safe: a single lock guards the freelist.

    Args:
        agent: The nixl_agent to register the arena against.
        arena_size: Total arena size in bytes (multiple of page_size).
        page_size: Granularity of pool pages in bytes.
    """

    def __init__(
        self,
        agent: object,
        arena_size: int = _POOL_DEFAULT_SIZE,
        page_size: int = _POOL_PAGE_SIZE,
    ) -> None:
        if arena_size % page_size != 0:
            raise ValueError(
                f"_NixlBufferPool: arena_size={arena_size} must be a "
                f"multiple of page_size={page_size}"
            )
        self._page_size = page_size
        self._lock = threading.Lock()
        self._agent = agent

        self._backing = mmap.mmap(-1, arena_size)
        self._base_addr: int = ctypes.addressof(
            ctypes.c_char.from_buffer(self._backing)
        )

        # Register the whole arena once with NIXL.
        self._reg_dlist = agent.register_memory(
            [(self._base_addr, arena_size, 0, "")], "DRAM"
        )

        num_pages = arena_size // page_size
        self._freelist: list[int] = [
            self._base_addr + i * page_size for i in range(num_pages)
        ]
        logger.info(
            "NixlBufferPool: arena=%d MB, page=%d KB, pages=%d",
            arena_size // (1024 * 1024),
            page_size // 1024,
            num_pages,
        )

    def acquire(self, length: int) -> Optional[int]:
        """Return a free page address if ``length <= page_size``; else None."""
        if length > self._page_size:
            return None
        with self._lock:
            if not self._freelist:
                return None
            return self._freelist.pop()

    def release(self, addr: int) -> None:
        """Return a page address to the freelist."""
        with self._lock:
            self._freelist.append(addr)

    def close(self) -> None:
        """Deregister the arena and unmap backing memory."""
        try:
            self._agent.deregister_memory(self._reg_dlist)
        except Exception as exc:
            logger.warning("NixlBufferPool: deregister_memory failed: %s", exc)
        try:
            self._backing.close()
        except Exception as exc:
            logger.warning("NixlBufferPool: mmap close failed: %s", exc)


class NixlTransferModule:
    """Handles STORE and RETRIEVE KV cache transfers over NIXL.

    Maintains a single server-side :class:`nixl_agent` (UCX backend by
    default).  Client agents are registered lazily on first request
    (``add_remote_agent`` is idempotent in NIXL).

    Per-transfer, the server uses the serialized xfer dlist from the
    :class:`NixlWrapper` directly — it does not re-examine the remote
    agent's full registered-memory list.

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
        import os
        _backends = backends if backends is not None else ["UCX"]

        nixl_agent_cls, nixl_agent_config_cls = _load_nixl()

        # Use STRICT sync mode so concurrent AFFINITY-pool threads don't race
        # on shared nixl_agent internal state.
        nixl_thread_sync_t = None
        for _mod in ("nixl._api", "nixl_cu12._api", "nixl_cu13._api"):
            try:
                _m = importlib.import_module(_mod)
                nixl_thread_sync_t = getattr(_m, "nixl_thread_sync_t", None)
                if nixl_thread_sync_t is not None:
                    break
            except ImportError:
                pass

        agent_kwargs: dict = {"backends": _backends}
        if nixl_thread_sync_t is not None:
            agent_kwargs["sync_mode"] = nixl_thread_sync_t.NIXL_THREAD_SYNC_STRICT

        self._agent = nixl_agent_cls(
            f"lmcache_server_{os.getpid()}_{id(self)}",
            nixl_agent_config_cls(**agent_kwargs),
        )

        pool_mb = int(os.environ.get("LMCACHE_NIXL_POOL_SIZE_MB", "256"))
        if pool_mb > 0:
            self._pool: Optional[_NixlBufferPool] = _NixlBufferPool(
                self._agent, arena_size=pool_mb * 1024 * 1024
            )
        else:
            self._pool = None

        logger.info(
            "NixlTransferModule initialised (backends=%s, pool=%s MB)",
            _backends,
            pool_mb if pool_mb > 0 else "disabled",
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
        """Release module resources including the pre-registered buffer pool."""
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run_xfer(
        self,
        operation: str,
        local_ptr: int,
        chunk_length: int,
        mem_type: str,
        remote_ptr: int,
        device_id: int,
        agent_name: str,
        chunk_idx: int,
        num_chunks: int,
        instance_id: int,
        key: IPCCacheServerKey,
    ) -> bool:
        """Register local memory, build both dlists, post and poll a transfer.

        If a pre-registered pool is available and ``mem_type == "DRAM"`` and
        ``chunk_length <= pool page``, acquires a pool page instead of calling
        ``register_memory`` (zero NIXL calls on the hot path).  A
        ``ctypes.memmove`` bridges the pool page and ``local_ptr`` when needed.

        Uses ``initialize_xfer`` (combined one-shot API) rather than
        ``prep_xfer_dlist`` + ``make_prepped_xfer`` to avoid stale UCX
        endpoint state across sequential cross-process requests.

        Args:
            operation: ``"READ"`` (STORE path) or ``"WRITE"`` (RETRIEVE path).
            local_ptr: Data pointer for the local (server) buffer.
            chunk_length: Byte count of this chunk.
            mem_type: NIXL memory type string (``"DRAM"`` or ``"VRAM"``).
            remote_ptr: Data pointer for the remote (worker) chunk buffer.
            device_id: NIXL device ID for the remote buffer.
            agent_name: Name of the remote nixl_agent.
            chunk_idx: Chunk index (for logging).
            num_chunks: Total chunks (for logging).
            instance_id: Worker process ID (for logging).
            key: Cache key (for logging).

        Returns:
            True on success, False on timeout, error, or exception.
        """
        local_reg = None
        pool_addr: Optional[int] = None
        xfer_handle = None
        try:
            # Try pool first (O(1), no NIXL call) for DRAM local buffers.
            # Pool pages are pre-registered; only fall back to per-call
            # register_memory for VRAM or chunks larger than the pool page.
            if mem_type == "DRAM" and self._pool is not None:
                pool_addr = self._pool.acquire(chunk_length)

            if pool_addr is not None:
                # Pool hit: arena already registered — no kernel call needed.
                # For WRITE (RETRIEVE): copy storage data into pool page first.
                if operation == "WRITE":
                    ctypes.memmove(pool_addr, local_ptr, chunk_length)
                local_dlist = self._agent.get_xfer_descs(
                    [(pool_addr, chunk_length, 0)], "DRAM"
                )
            else:
                # Pool miss or VRAM: register local memory per-chunk.
                local_reg = self._agent.register_memory(
                    self._agent.get_reg_descs(
                        [(local_ptr, chunk_length, 0, "")], mem_type
                    )
                )
                local_dlist = self._agent.get_xfer_descs(
                    [(local_ptr, chunk_length, 0)], mem_type
                )
            remote_dlist = self._agent.get_xfer_descs(
                [(remote_ptr, chunk_length, device_id)], mem_type
            )

            # Use initialize_xfer (combined one-shot API) rather than
            # prep_xfer_dlist + make_prepped_xfer.  The prep-based path
            # caches a prepared dlist handle and can stall on the second
            # cross-process request when UCX endpoint state has changed
            # between requests.
            xfer_handle = self._agent.initialize_xfer(
                operation,
                local_dlist,
                remote_dlist,
                agent_name,
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
                ok = True
            else:
                ok = _poll_until_done(self._agent, xfer_handle, _XFER_TIMEOUT_S)
            if not ok:
                logger.warning(
                    "_run_xfer: NIXL %s timed out for chunk %d/%d "
                    "(instance_id=%d key=%s)",
                    operation, chunk_idx, num_chunks, instance_id, key,
                )
                return False
            # For READ (STORE): data landed in pool page; copy to storage buffer.
            if pool_addr is not None and operation == "READ":
                ctypes.memmove(local_ptr, pool_addr, chunk_length)
            return True
        except Exception:
            logger.exception(
                "_run_xfer: error for chunk %d/%d (instance_id=%d key=%s)",
                chunk_idx, num_chunks, instance_id, key,
            )
            return False
        finally:
            if xfer_handle is not None:
                try:
                    self._agent.release_xfer_handle(xfer_handle)
                except Exception:
                    pass
            if pool_addr is not None and self._pool is not None:
                self._pool.release(pool_addr)
            elif local_reg is not None:
                try:
                    self._agent.deregister_memory(local_reg)
                except Exception:
                    pass

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
        channel.  This handler loads the client agent, allocates local storage
        for each chunk, builds per-chunk remote xfer dlists from the wrapper's
        raw buffer address, and performs a NIXL READ per chunk via
        ``initialize_xfer``.

        Args:
            key: The IPC cache key identifying the token range to store.
            instance_id: Initiator process ID (used for logging only).
            block_ids: GPU block IDs — always ``[]`` for NIXL; ignored.
            nixl_descriptor_bytes: Pickled :class:`NixlWrapper`.

        Returns:
            A ``(b"", all_succeeded)`` tuple.

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

        # Register the client agent (idempotent after first call).
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

                ok = self._run_xfer(
                    "READ",
                    local_ptr=mem_obj.data_ptr,
                    chunk_length=chunk_length,
                    mem_type=wrapper.mem_type,
                    remote_ptr=wrapper.base_addr + i * chunk_length,
                    device_id=wrapper.device_id,
                    agent_name=wrapper.agent_name,
                    chunk_idx=i,
                    num_chunks=num_chunks,
                    instance_id=instance_id,
                    key=key,
                )
                if not ok:
                    all_succeeded = False
                else:
                    success = True

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

        The initiator allocated and registered a destination buffer, built a
        per-tensor xfer dlist with :meth:`NixlWrapper.wrap`, and serialized
        it into ``nixl_descriptor_bytes``.  This handler reads MemoryObjs
        from the storage manager and performs a NIXL WRITE per chunk.

        The response is not sent until all WRITEs complete.

        Args:
            key: The IPC cache key identifying the token range to retrieve.
            instance_id: Initiator process ID (used for logging only).
            block_ids: GPU block IDs — always ``[]`` for NIXL; ignored.
            nixl_descriptor_bytes: Pickled :class:`NixlWrapper`.
            skip_first_n_tokens: Not yet implemented for NIXL path.

        Returns:
            A ``(b"", success)`` tuple.

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

        # Register the client agent (idempotent after first call).
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
                ok = self._run_xfer(
                    "WRITE",
                    local_ptr=mem_obj.data_ptr,
                    chunk_length=chunk_length,
                    mem_type=wrapper.mem_type,
                    remote_ptr=wrapper.base_addr + i * chunk_length,
                    device_id=wrapper.device_id,
                    agent_name=wrapper.agent_name,
                    chunk_idx=i,
                    num_chunks=num_chunks,
                    instance_id=instance_id,
                    key=key,
                )
                if not ok:
                    all_succeeded = False

        if all_succeeded:
            # Release read locks only after all WRITEs have completed.
            self._ctx.storage_manager.finish_read_prefetched(obj_keys)

        return (b"", all_succeeded)
