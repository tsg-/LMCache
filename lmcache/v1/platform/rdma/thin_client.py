# SPDX-License-Identifier: Apache-2.0
"""Standalone RDMA thin client.

Connects to an LMCache server over ZMQ and exercises the RDMA store/retrieve
paths using plain CPU tensors — no vLLM, no CUDA, no engine adapter.

Usage::

    from lmcache.v1.platform.rdma.thin_client import RdmaThinClient

    # Default: pyverbs RDMA path
    client = RdmaThinClient("tcp://server:5601", model_name="deepseek-v3")

    # NIXL UCX path
    from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper
    client = RdmaThinClient("tcp://server:5605", model_name="deepseek-v3",
                            wrapper_cls=NixlWrapper)

    client.store("req-0", token_ids=[1, 2, 3, 4], data=my_tensor)
    result = client.retrieve("req-1", token_ids=[1, 2, 3, 4], numel=4, dtype=torch.float32)
    client.close()
"""

from __future__ import annotations

import os
from typing import Optional, Type

import torch
import zmq

from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey
from lmcache.v1.multiprocess.mq import MessageQueueClient
from lmcache.v1.multiprocess.protocols.base import RequestType
from lmcache.v1.platform.base.ipc_wrapper import DeviceIPCWrapper
from lmcache.v1.platform.rdma.rdma_wrapper import RdmaWrapper


class RdmaThinClient:
    """Standalone initiator that drives RDMA store/retrieve over ZMQ.

    Allocates host-DRAM buffers (CPU tensors), registers them via
    ``wrapper_cls``, and communicates with the LMCache server using
    the same protocol as the vLLM engine adapter — but without any
    CUDA or vLLM dependencies.

    The default wrapper is :class:`RdmaWrapper` (pyverbs RDMA path).
    Pass ``wrapper_cls=NixlWrapper`` to use the NIXL/UCX path against a
    server started with ``supported_transfer_mode="nixl"``.

    Args:
        server_url: ZMQ endpoint of the LMCache server (e.g.
            ``tcp://10.0.0.1:5601``).
        model_name: Model name used in cache key construction.
        world_size: KV world size (default 1).
        worker_id: Worker identifier (default 0).
        timeout: Default timeout in seconds for blocking operations.
        zmq_context: Optional shared ZMQ context; a new one is created
            if not provided.
        wrapper_cls: IPC wrapper class used to register tensors and
            produce descriptors.  Defaults to :class:`RdmaWrapper`.
            Pass :class:`~lmcache.v1.platform.rdma.nixl_wrapper.NixlWrapper`
            for the NIXL transfer path.
    """

    def __init__(
        self,
        server_url: str,
        model_name: str = "thin-client",
        world_size: int = 1,
        worker_id: int = 0,
        timeout: float = 30.0,
        zmq_context: Optional[zmq.Context] = None,
        wrapper_cls: Type[DeviceIPCWrapper] = RdmaWrapper,
    ) -> None:
        os.environ.setdefault("LMCACHE_RDMA_TRANSPORT", "stub")
        self._model_name = model_name
        self._world_size = world_size
        self._worker_id = worker_id
        self._timeout = timeout
        self._wrapper_cls = wrapper_cls
        self._ctx = zmq_context or zmq.Context.instance()
        self._client = MessageQueueClient(
            server_url=server_url, context=self._ctx
        )

    def store(
        self,
        request_id: str,
        token_ids: list[int],
        data: torch.Tensor,
        start: int = 0,
        end: Optional[int] = None,
    ) -> bool:
        """Store a KV tensor on the server via RDMA Read (server pulls).

        The tensor must be a contiguous CPU tensor. It is wrapped as an
        RDMA memory region and the server pulls the data directly from
        the caller's host DRAM.

        Args:
            request_id: Unique request identifier for tracing.
            token_ids: Token ID sequence that identifies this cache entry.
            data: Contiguous CPU tensor to store.
            start: Start offset within the token range.
            end: End offset (defaults to ``len(token_ids)``).

        Returns:
            ``True`` if the server confirmed successful storage.

        Raises:
            ValueError: If *data* is not a contiguous CPU tensor.
            TimeoutError: If the server does not respond within the
                configured timeout.
        """
        if not data.is_contiguous():
            raise ValueError("store: data tensor must be contiguous")
        if data.device.type != "cpu":
            raise ValueError("store: data tensor must be on CPU")

        if end is None:
            end = len(token_ids)

        key = IPCCacheServerKey(
            model_name=self._model_name,
            world_size=self._world_size,
            worker_id=self._worker_id,
            token_ids=tuple(token_ids),
            start=start,
            end=end,
            request_id=request_id,
        )

        wrapper = self._wrapper_cls.wrap(data)
        descriptor = DeviceIPCWrapper.Serialize(wrapper)

        future = self._client.submit_request(
            RequestType.STORE,
            [key, os.getpid(), [], descriptor],
        )
        _, ok = future.result(timeout=self._timeout)
        return ok

    def retrieve(
        self,
        request_id: str,
        token_ids: list[int],
        numel: int,
        dtype: torch.dtype = torch.float32,
        start: int = 0,
        end: Optional[int] = None,
    ) -> Optional[torch.Tensor]:
        """Retrieve a KV tensor from the server via RDMA Write (server pushes).

        Allocates a destination buffer in host DRAM and sends its RDMA
        descriptor to the server. The server writes the cached data
        directly into the buffer. On cache miss, returns ``None``.

        Args:
            request_id: Unique request identifier for tracing.
            token_ids: Token ID sequence identifying the cache entry.
            numel: Number of elements to allocate in the destination buffer.
            dtype: Element type of the destination buffer.
            start: Start offset within the token range.
            end: End offset (defaults to ``len(token_ids)``).

        Returns:
            The filled destination tensor on success, or ``None`` on
            cache miss.

        Raises:
            TimeoutError: If the server does not respond within the
                configured timeout.
        """
        if end is None:
            end = len(token_ids)

        key = IPCCacheServerKey(
            model_name=self._model_name,
            world_size=self._world_size,
            worker_id=self._worker_id,
            token_ids=tuple(token_ids),
            start=start,
            end=end,
            request_id=request_id,
        )

        dst = torch.zeros(numel, dtype=dtype)
        wrapper = self._wrapper_cls.wrap(dst)
        descriptor = DeviceIPCWrapper.Serialize(wrapper)

        future = self._client.submit_request(
            RequestType.RETRIEVE,
            [key, os.getpid(), [], descriptor, 0],
        )
        _, ok = future.result(timeout=self._timeout)
        if not ok:
            return None
        return dst

    def close(self) -> None:
        """Close the ZMQ connection and release resources."""
        self._client.close()

    def __enter__(self) -> "RdmaThinClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# Backward-compat alias.
IPURdmaThinClient = RdmaThinClient
