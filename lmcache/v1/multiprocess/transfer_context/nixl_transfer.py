# SPDX-License-Identifier: Apache-2.0
"""Worker-side NIXL transfer context for LMCache multiprocess adapters.

Used when ``LMCACHE_MP_TRANSFER_MODE=nixl``.  The worker registers its KV
buffer with the process-level :class:`nixl_agent` (created by
:func:`lmcache.v1.platform.rdma.nixl_wrapper.get_nixl_agent`) and sends a
:class:`NixlWrapper` descriptor to the server over the ZMQ control channel.
The server performs the actual data movement via NIXL READ (STORE) or NIXL
WRITE (RETRIEVE).

``event.ipc_handle()`` must return the pickled :class:`NixlWrapper` bytes
produced by :func:`NixlWrapper.wrap`.  ``block_ids`` is unused — the NIXL
descriptor carries all addressing information.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import torch

from lmcache.utils import init_logger
from lmcache.v1.multiprocess.futures import MessagingFuture
from lmcache.v1.multiprocess.group_view import EngineGroupInfo
from lmcache.v1.multiprocess.mq import MessageQueueClient
from lmcache.v1.multiprocess.protocol import RequestType
from lmcache.v1.multiprocess.transfer_context.worker_transfer import (
    IPCEvent,
    SendRequest,
    TransferContext,
)
from lmcache.v1.gpu_connector.utils import LayoutHints

logger = init_logger(__name__)


class NixlTransferContext(TransferContext):
    """NIXL-based transfer context for workers with UCX-capable NICs.

    Used when ``LMCACHE_MP_TRANSFER_MODE=nixl``.  The worker registers its
    process-level :class:`nixl_agent` once at construction (via
    :func:`get_nixl_agent`) and wraps KV tensors into :class:`NixlWrapper`
    descriptors on each store/retrieve call.

    The server performs NIXL READ (for STORE) or NIXL WRITE (for RETRIEVE)
    using the descriptor.  The worker's buffer must remain live until the
    future resolves.

    ``event.ipc_handle()`` must return pickled :class:`NixlWrapper` bytes.
    ``block_ids`` and ``kv_caches`` are unused — the NIXL descriptor carries
    all addressing information.
    """

    def __init__(self) -> None:
        self._mq_client: MessageQueueClient | None = None
        self._send_request: SendRequest | None = None

    def register(
        self,
        instance_id: int,
        kv_caches: dict[str, torch.Tensor],
        model_name: str,
        world_size: int,
        blocks_in_chunk: int,
        mq_client: MessageQueueClient,
        mq_timeout: float,
        send_request: SendRequest,
        layout_hints: LayoutHints | None = None,
        engine_group_infos: Sequence[EngineGroupInfo] = (),
    ) -> None:
        """Store MQ client and sender; skip server KV-cache registration.

        No ``REGISTER_KV_CACHE`` message is sent because the NIXL agent is
        established at :func:`get_nixl_agent` call time (first wrap in the
        process), not at registration time.

        Args:
            instance_id: Worker process instance identifier (unused).
            kv_caches: Worker KV cache tensors keyed by layer name (unused).
            model_name: Model name (unused).
            world_size: KV world size (unused).
            blocks_in_chunk: Number of vLLM blocks per LMCache chunk (unused).
            mq_client: Message queue client used to communicate with server.
            mq_timeout: Timeout in seconds for synchronous request wait
                (unused; no synchronous registration round-trip here).
            send_request: Request sender callable used to issue MQ requests.
            layout_hints: Optional layout hints (unused).
            engine_group_infos: Engine KV cache group metadata (unused).
        """
        self._mq_client = mq_client
        self._send_request = send_request

    def submit_store(
        self,
        _request_id: str,
        key: Any,
        instance_id: int,
        _kv_caches: dict[str, torch.Tensor],
        _block_ids: list[list[int]],
        event: IPCEvent,
        _blocks_in_chunk: int,
    ) -> MessagingFuture:
        """Submit a NIXL store request and return the MQ future.

        ``event.ipc_handle()`` must return the pickled :class:`NixlWrapper`
        bytes carrying the source buffer descriptor.  The server performs a
        NIXL READ into its own storage and responds when complete.  The
        worker's source buffer must remain live until the future resolves.

        Do not call ``.to_cuda_future()`` on the returned future — the
        response payload is not a CUDA event handle.

        Args:
            _request_id: External request identifier (unused).
            key: LMCache key object for the store range.
            instance_id: Worker process instance identifier.
            _kv_caches: Worker KV cache tensors (unused; NIXL path uses
                the wrapper descriptor, not GPU block IDs).
            _block_ids: vLLM block IDs (unused on the NIXL path).
            event: IPC event whose ``ipc_handle()`` returns the pickled
                NIXL source descriptor bytes.
            _blocks_in_chunk: Number of vLLM blocks per chunk (unused).

        Returns:
            A :class:`MessagingFuture` backed by the MQ STORE request. It
            resolves only after the server confirms the NIXL READ completed.

        Raises:
            RuntimeError: If :meth:`register` was not called first.
        """
        if self._mq_client is None or self._send_request is None:
            raise RuntimeError(
                "NIXL transfer context is not registered. "
                "Call register() before submit_store()."
            )
        nixl_descriptor_bytes = event.ipc_handle()
        return self._send_request(
            self._mq_client,
            RequestType.STORE,
            [key, instance_id, [], nixl_descriptor_bytes],
        )

    def submit_retrieve(
        self,
        _request_id: str,
        key: Any,
        instance_id: int,
        _kv_caches: dict[str, torch.Tensor],
        _block_ids: list[list[int]],
        event: IPCEvent,
        _blocks_in_chunk: int,
        skip_first_n_tokens: int = 0,
    ) -> MessagingFuture:
        """Submit a NIXL retrieve request and return the MQ future.

        ``event.ipc_handle()`` must return the pickled :class:`NixlWrapper`
        bytes carrying the destination buffer descriptor.  The server performs
        a NIXL WRITE into the worker's buffer and responds when complete.

        Do not call ``.to_cuda_future()`` on the returned future — the
        response payload is not a CUDA event handle.

        Args:
            _request_id: External request identifier (unused).
            key: LMCache key object for the retrieve range.
            instance_id: Worker process instance identifier.
            _kv_caches: Worker KV cache tensors (unused).
            _block_ids: vLLM block IDs (unused on the NIXL path).
            event: IPC event whose ``ipc_handle()`` returns the pickled
                NIXL destination descriptor bytes.
            _blocks_in_chunk: Number of vLLM blocks per chunk (unused).
            skip_first_n_tokens: Number of initial tokens to skip when writing.

        Returns:
            A :class:`MessagingFuture` backed by the MQ RETRIEVE request.

        Raises:
            RuntimeError: If :meth:`register` was not called first.
        """
        if self._mq_client is None or self._send_request is None:
            raise RuntimeError(
                "NIXL transfer context is not registered. "
                "Call register() before submit_retrieve()."
            )
        nixl_descriptor_bytes = event.ipc_handle()
        return self._send_request(
            self._mq_client,
            RequestType.RETRIEVE,
            [key, instance_id, [], nixl_descriptor_bytes, skip_first_n_tokens],
        )

    def close(self) -> None:
        """Release MQ client and sender references."""
        self._mq_client = None
        self._send_request = None
