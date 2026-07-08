# SPDX-License-Identifier: Apache-2.0
"""RDMA transfer module for LMCache multiprocess server.

Implements :class:`RdmaTransferModule`, an :class:`EngineModule` that handles
STORE and RETRIEVE requests by moving KV tensors between host DRAM buffers
over RoCEv2 RDMA (ibverbs post_read / post_write).

The NIC is a dumb RoCEv2 device.  All KV processing happens on the CPU.
KV tensors are plain CPU tensors in host DRAM.  The NIC handles only RDMA
DMA operations.  There are no CUDA events, no GPU block IDs, and no GPU
synchronisation anywhere in this module.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import torch

from lmcache.logging import init_logger
from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey
from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey
from lmcache.v1.multiprocess.engine_module import EngineModule, HandlerSpec, ThreadPoolType
from lmcache.v1.platform.base_ipc_wrapper import DeviceIPCWrapper
from lmcache.v1.platform.rdma.rdma_transport import (
    RegisteredBuffer,
    StubRdmaTransport,
    get_rdma_transport,
)
from lmcache.v1.platform.rdma.rdma_wrapper import RdmaWrapper
from lmcache.v1.multiprocess.protocols.base import RequestType

if TYPE_CHECKING:
    from lmcache.v1.multiprocess.engine_context import MPCacheServerContext

logger = init_logger(__name__)


def _per_chunk_shape(full_shape: tuple[int, ...], num_chunks: int) -> torch.Size:
    """Divide a wrapper's leading dimension by ``num_chunks``.

    :class:`RdmaWrapper` describes the *entire* registered buffer, which
    may span multiple LMCache chunks. ``reserve_write`` must be given the
    shape of a single chunk, not the whole range, or the storage manager
    allocates an oversized :class:`MemoryObj` per chunk.

    Args:
        full_shape: The wrapper's full tensor shape (dimension 0 is the
            flat byte/element count spanning all chunks).
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


class RdmaTransferModule:
    """Handles STORE and RETRIEVE KV cache transfers over RDMA.

    Binds directly to the process-global :class:`RdmaTransport` obtained at
    construction time (fails fast if the transport is unavailable).  There is
    no per-instance GPU context — the RDMA QP is established once at transport
    initialisation, not per vLLM worker registration.

    Args:
        ctx: The shared engine context providing storage_manager and
             resolve_obj_keys.
    """

    def __init__(self, ctx: "MPCacheServerContext") -> None:
        self._ctx = ctx
        # Fail fast: if the transport is misconfigured this raises immediately
        # rather than surfacing errors on the first STORE/RETRIEVE call.
        self._transport = get_rdma_transport()
        logger.info(
            "RdmaTransferModule initialised with transport %s",
            type(self._transport).__name__,
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
            A two-element list mapping STORE and RETRIEVE to their
            handlers, both running in the AFFINITY pool to allow
            concurrent RDMA operations.
        """
        return [
            HandlerSpec(RequestType.STORE, self.store, ThreadPoolType.AFFINITY),
            HandlerSpec(RequestType.RETRIEVE, self.retrieve, ThreadPoolType.AFFINITY),
        ]

    def report_status(self) -> dict:
        """Return module status including the active transport type.

        Returns:
            A dict with key ``"rdma_transfer"`` containing the transport
            class name.
        """
        return {
            "rdma_transfer": {
                "rdma_transport": type(self._transport).__name__,
            }
        }

    def close(self) -> None:
        """Release module resources.

        The transport lifecycle is managed by :func:`get_rdma_transport`;
        this module does not own it and takes no action on close.
        """
        # Transport is a process-global singleton; do not close it here.
        pass

    # ------------------------------------------------------------------
    # SHM mapping helpers (stub backend only)
    # ------------------------------------------------------------------

    def _map_remote_if_stub(self, wrapper: RdmaWrapper) -> None:
        """Map the remote wrapper's SHM segment if using stub transport."""
        if (
            isinstance(self._transport, StubRdmaTransport)
            and wrapper.shm_name
        ):
            self._transport.map_remote_mr(
                wrapper.rkey,
                wrapper.shm_name,
                wrapper.remote_addr,
                wrapper.length,
            )

    def _unmap_remote_if_stub(self, wrapper: RdmaWrapper) -> None:
        """Unmap a previously mapped remote SHM segment."""
        if (
            isinstance(self._transport, StubRdmaTransport)
            and wrapper.shm_name
        ):
            self._transport.unmap_remote_mr(wrapper.rkey)

    # ------------------------------------------------------------------
    # Request handlers
    # ------------------------------------------------------------------

    def store(
        self,
        key: IPCCacheServerKey,
        instance_id: int,
        block_ids: list[list[int]],
        rdma_descriptor_bytes: bytes,
    ) -> tuple[bytes, bool]:
        """Pull KV data from the initiator via RDMA Read and store it locally.

        The initiator has already registered its KV buffer as an RDMA MR and
        sent the descriptor (rkey, remote_addr, length, shape, dtype) as a
        pickled :class:`RdmaWrapper`.  This handler iterates over the
        resolved object keys, reserves write slots in the storage manager,
        registers each MemoryObj's backing memory as a local RDMA MR, and
        posts an RDMA Read to pull the data directly from the initiator's
        host DRAM with zero intermediate copies.

        Args:
            key: The IPC cache key identifying the token range to store.
            instance_id: Initiator process ID (used for logging only).
            block_ids: GPU block IDs — always ``[]`` for RDMA; ignored.
            rdma_descriptor_bytes: Pickled :class:`RdmaWrapper` carrying
                the source RDMA descriptor (rkey, remote_addr, length,
                shape, dtype).

        Returns:
            A ``(b"", all_succeeded)`` tuple.  ``all_succeeded`` is ``True``
            only when every chunk completed without error or timeout.

        Raises:
            ValueError: If the deserialized descriptor is not a
                :class:`RdmaWrapper`, or if the total length is not
                evenly divisible by the number of chunks.
        """
        wrapper = DeviceIPCWrapper.Deserialize(rdma_descriptor_bytes)
        if not isinstance(wrapper, RdmaWrapper):
            raise ValueError(
                f"store: expected RdmaWrapper, got {type(wrapper).__name__}"
            )

        # Map the remote SHM segment locally so post_read can access it.
        self._map_remote_if_stub(wrapper)

        obj_key_groups: list[list[ObjectKey]] = self._ctx.resolve_obj_keys(key, [0])
        obj_keys: list[ObjectKey] = obj_key_groups[0]
        num_chunks = len(obj_keys)

        if num_chunks == 0:
            logger.debug(
                "store: instance_id=%d key=%s resolved to 0 chunks, nothing to do",
                instance_id,
                key,
            )
            self._unmap_remote_if_stub(wrapper)
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
            remote_offset = i * chunk_length
            reserved: dict[ObjectKey, object] = {}
            mr = None
            success = False
            try:
                reserved = self._ctx.storage_manager.reserve_write(
                    [obj_key], layout_desc, "new"
                )
                mem_obj = reserved[obj_key]

                local_ptr = mem_obj.data_ptr
                mr = self._transport.register_mr(local_ptr, chunk_length)
                local_buf = RegisteredBuffer(
                    addr=mr.addr,
                    length=chunk_length,
                    mr=mr,
                )

                future = self._transport.post_read(
                    local_buf=local_buf,
                    remote_addr=wrapper.remote_addr + remote_offset,
                    rkey=wrapper.rkey,
                    length=chunk_length,
                )
                ok = self._transport.poll_completion(future, timeout_ms=5000)
                if not ok:
                    logger.warning(
                        "store: RDMA Read timed out for chunk %d/%d "
                        "(instance_id=%d key=%s)",
                        i,
                        num_chunks,
                        instance_id,
                        key,
                    )
                    all_succeeded = False
                else:
                    success = True
            except Exception:
                logger.exception(
                    "store: error on chunk %d/%d (instance_id=%d key=%s)",
                    i,
                    num_chunks,
                    instance_id,
                    key,
                )
                all_succeeded = False
            finally:
                if mr is not None:
                    try:
                        self._transport.deregister_mr(mr)
                    except Exception:
                        logger.exception(
                            "store: failed to deregister MR for chunk %d", i
                        )
                # Always release the write lock. Pass the key on success so the
                # storage manager commits the write; pass an empty list on
                # failure so it aborts without leaking the lock.
                commit_keys = [obj_key] if success else []
                try:
                    self._ctx.storage_manager.finish_write(commit_keys)
                except Exception:
                    logger.exception(
                        "store: finish_write failed for chunk %d (success=%s)",
                        i,
                        success,
                    )

        self._unmap_remote_if_stub(wrapper)
        return (b"", all_succeeded)

    def retrieve(
        self,
        key: IPCCacheServerKey,
        instance_id: int,
        block_ids: list[list[int]],
        rdma_descriptor_bytes: bytes,
        skip_first_n_tokens: int = 0,
    ) -> tuple[bytes, bool]:
        """Push locally stored KV data to the initiator via RDMA Write.

        The initiator has already allocated and registered a destination buffer
        in its host DRAM and sent its RDMA descriptor as a pickled
        :class:`RdmaWrapper`.  This handler reads the matching MemoryObjs
        from the storage manager, registers each as a local RDMA source MR,
        and posts an RDMA Write to push the data directly into the initiator's
        buffer.

        The response is not returned until all RDMA Writes have completed so
        that the initiator knows its destination MR is safe to use as soon as
        it receives the response.

        Args:
            key: The IPC cache key identifying the token range to retrieve.
            instance_id: Initiator process ID (used for logging only).
            block_ids: GPU block IDs — always ``[]`` for RDMA; ignored.
            rdma_descriptor_bytes: Pickled :class:`RdmaWrapper` carrying
                the destination RDMA descriptor (rkey, remote_addr) on the
                initiator side.
            skip_first_n_tokens: Number of tokens to skip at the start of
                the retrieve range (passed through; not yet implemented for
                RDMA path).

        Returns:
            A ``(b"", success)`` tuple.  ``success`` is ``True`` only when
            all chunks were found in L1 and all RDMA Writes completed
            without timeout or error.

        Raises:
            ValueError: If the deserialized descriptor is not a
                :class:`RdmaWrapper`, or if the total length is not
                evenly divisible by the number of resolved chunks.
        """
        wrapper = DeviceIPCWrapper.Deserialize(rdma_descriptor_bytes)
        if not isinstance(wrapper, RdmaWrapper):
            raise ValueError(
                f"retrieve: expected RdmaWrapper, got {type(wrapper).__name__}"
            )

        # Map the remote SHM segment locally so post_write can write into it.
        self._map_remote_if_stub(wrapper)

        obj_key_groups: list[list[ObjectKey]] = self._ctx.resolve_obj_keys(key, [0])
        obj_keys: list[ObjectKey] = obj_key_groups[0]
        num_chunks = len(obj_keys)

        if num_chunks == 0:
            logger.debug(
                "retrieve: instance_id=%d key=%s resolved to 0 chunks",
                instance_id,
                key,
            )
            self._unmap_remote_if_stub(wrapper)
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

        # Acquire read locks before read_prefetched_results. The vLLM engine
        # adapter normally does this via submit_prefetch_task in the request
        # pipeline, but thin clients bypass that layer (LMCache-gyk).
        prefetch_handle = self._ctx.storage_manager.submit_prefetch_task(
            obj_keys, layout_desc, skip_l2=True
        )
        if len(prefetch_handle.l1_found_indices) < num_chunks:
            logger.debug(
                "retrieve: cache miss for key=%s (instance_id=%d, "
                "found=%d/%d chunks)",
                key,
                instance_id,
                len(prefetch_handle.l1_found_indices),
                num_chunks,
            )
            # Release any partial read locks acquired by submit_prefetch_task.
            if prefetch_handle.l1_found_indices:
                found_keys = [obj_keys[i] for i in prefetch_handle.l1_found_indices]
                self._ctx.storage_manager.finish_read_prefetched(found_keys)
            self._unmap_remote_if_stub(wrapper)
            return (b"", False)

        all_succeeded = True

        with self._ctx.storage_manager.read_prefetched_results(obj_keys) as mem_objs:
            if mem_objs is None:
                logger.debug(
                    "retrieve: read_prefetched_results returned None for "
                    "key=%s (instance_id=%d)",
                    key,
                    instance_id,
                )
                self._unmap_remote_if_stub(wrapper)
                return (b"", False)

            for i, mem_obj in enumerate(mem_objs):
                remote_offset = i * chunk_length
                mr = None
                try:
                    local_ptr = mem_obj.data_ptr
                    mr = self._transport.register_mr(local_ptr, chunk_length)
                    local_buf = RegisteredBuffer(
                        addr=mr.addr,
                        length=chunk_length,
                        mr=mr,
                    )

                    future = self._transport.post_write(
                        local_buf=local_buf,
                        remote_addr=wrapper.remote_addr + remote_offset,
                        rkey=wrapper.rkey,
                        length=chunk_length,
                    )
                    ok = self._transport.poll_completion(future, timeout_ms=5000)
                    if not ok:
                        logger.warning(
                            "retrieve: RDMA Write timed out for chunk %d/%d "
                            "(instance_id=%d key=%s)",
                            i,
                            num_chunks,
                            instance_id,
                            key,
                        )
                        all_succeeded = False
                except Exception:
                    logger.exception(
                        "retrieve: error on chunk %d/%d (instance_id=%d key=%s)",
                        i,
                        num_chunks,
                        instance_id,
                        key,
                    )
                    all_succeeded = False
                finally:
                    if mr is not None:
                        try:
                            self._transport.deregister_mr(mr)
                        except Exception:
                            logger.exception(
                                "retrieve: failed to deregister MR for chunk %d", i
                            )

        if all_succeeded:
            # Release read locks only after all RDMA Writes have completed.
            # The initiator must not use the destination buffer before this
            # response is sent, so poll_completion() must return True first.
            self._ctx.storage_manager.finish_read_prefetched(obj_keys)

        self._unmap_remote_if_stub(wrapper)
        return (b"", all_succeeded)


# Backward-compat alias.
IPUTransferModule = RdmaTransferModule
