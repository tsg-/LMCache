# SPDX-License-Identifier: Apache-2.0
"""End-to-end integration test for the RDMA path.

Spins up a real ``MPCacheServer`` subprocess with
``supported_transfer_mode="rdma"`` (routing STORE/RETRIEVE to
:class:`RdmaTransferModule`) and drives it from a real
:class:`~lmcache.v1.multiprocess.mq.MessageQueueClient`, exactly as a
standalone initiator thin client would: plain CPU host-DRAM tensors,
wrapped via :class:`RdmaWrapper`, with descriptors sent as
``rdma_descriptor_bytes`` over the wire.  No CUDA, no vLLM, no
``LMCacheDrivenTransferContext`` involvement anywhere in this path.

Uses ``StubRdmaTransport`` (memcpy) since CI has no RDMA hardware — the
transport backend is swapped by setting ``LMCACHE_RDMA_TRANSPORT=verbs``
outside of this test, with no code changes required on either side.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import time
from typing import Generator

import pytest
import torch
import zmq

from lmcache.v1.distributed.config import (
    EvictionConfig,
    L1ManagerConfig,
    L1MemoryManagerConfig,
    StorageManagerConfig,
)
from lmcache.v1.mp_observability.config import DEFAULT_OBSERVABILITY_CONFIG
from lmcache.v1.multiprocess.config import MPServerConfig
from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey
from lmcache.v1.multiprocess.mq import MessageQueueClient
from lmcache.v1.multiprocess.protocols.base import RequestType
from lmcache.v1.multiprocess.server import run_cache_server
from lmcache.v1.platform.base.ipc_wrapper import DeviceIPCWrapper
from lmcache.v1.platform.rdma.rdma_wrapper import RdmaWrapper

SERVER_HOST = "localhost"
SERVER_PORT = 5601
SERVER_URL = f"tcp://{SERVER_HOST}:{SERVER_PORT}"
CHUNK_SIZE = 4
DEFAULT_TIMEOUT = 20.0


def _server_process_runner(host: str, port: int, chunk_size: int) -> None:
    """Entry point for the RDMA-mode server subprocess."""
    os.environ["LMCACHE_RDMA_TRANSPORT"] = "stub"
    mp_config = MPServerConfig(
        host=host,
        port=port,
        chunk_size=chunk_size,
        supported_transfer_mode="rdma",
    )
    storage_manager_config = StorageManagerConfig(
        l1_manager_config=L1ManagerConfig(
            memory_config=L1MemoryManagerConfig(
                size_in_bytes=64 * 1024 * 1024,
                use_lazy=False,
            ),
        ),
        eviction_config=EvictionConfig(eviction_policy="LRU"),
    )
    run_cache_server(
        mp_config=mp_config,
        storage_manager_config=storage_manager_config,
        obs_config=DEFAULT_OBSERVABILITY_CONFIG,
        start_prometheus_http_server=False,
    )


@pytest.fixture(scope="module")
def rdma_server_process() -> Generator[mp.Process, None, None]:
    """Start a real MPCacheServer subprocess in RDMA transfer mode."""
    mp.set_start_method("spawn", force=True)
    process = mp.Process(
        target=_server_process_runner,
        args=(SERVER_HOST, SERVER_PORT, CHUNK_SIZE),
        daemon=True,
    )
    process.start()
    time.sleep(2)
    yield process
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join()


@pytest.fixture(scope="module")
def zmq_context() -> Generator[zmq.Context, None, None]:
    context = zmq.Context.instance()
    yield context


@pytest.fixture
def client(
    rdma_server_process: mp.Process, zmq_context: zmq.Context
) -> Generator[MessageQueueClient, None, None]:
    os.environ.setdefault("LMCACHE_RDMA_TRANSPORT", "stub")
    client = MessageQueueClient(server_url=SERVER_URL, context=zmq_context)
    yield client
    client.close()


def _make_key(request_id: str, num_tokens: int) -> IPCCacheServerKey:
    """Build a single-chunk IPCCacheServerKey (CHUNK_SIZE tokens)."""
    return IPCCacheServerKey(
        model_name="rdma-thin-client-test",
        world_size=1,
        worker_id=0,
        token_ids=tuple(range(num_tokens)),
        start=0,
        end=num_tokens,
        request_id=request_id,
    )


class TestRdmaThinClientStore:
    """Drives STORE against a real server via a real ZMQ client."""

    def test_store_pulls_bytes_from_initiator_buffer(
        self, client: MessageQueueClient
    ) -> None:
        """Server should RDMA-Read the initiator's source buffer and
        confirm success only after the pull completes."""
        numel = CHUNK_SIZE
        src = torch.arange(numel, dtype=torch.float32)

        wrapper = RdmaWrapper.wrap(src)
        descriptor = DeviceIPCWrapper.Serialize(wrapper)

        key = _make_key("store-req-0", numel)
        future = client.submit_request(
            RequestType.STORE,
            [key, os.getpid(), [], descriptor],
        )
        response_bytes, ok = future.result(timeout=DEFAULT_TIMEOUT)

        assert ok is True
        assert response_bytes == b""


class TestRdmaThinClientRetrieve:
    """Drives STORE then RETRIEVE, verifying the round-tripped bytes."""

    def test_retrieve_pushes_bytes_into_initiator_buffer(
        self, client: MessageQueueClient
    ) -> None:
        numel = CHUNK_SIZE
        src = torch.arange(numel, dtype=torch.float32) + 100.0

        store_wrapper = RdmaWrapper.wrap(src)
        store_descriptor = DeviceIPCWrapper.Serialize(store_wrapper)

        # Use token_ids that don't collide with earlier tests.
        key = IPCCacheServerKey(
            model_name="rdma-thin-client-test",
            world_size=1,
            worker_id=0,
            token_ids=tuple(range(100, 100 + numel)),
            start=0,
            end=numel,
            request_id="retrieve-req-0",
        )
        store_future = client.submit_request(
            RequestType.STORE,
            [key, os.getpid(), [], store_descriptor],
        )
        _, store_ok = store_future.result(timeout=DEFAULT_TIMEOUT)
        assert store_ok is True

        # Allocate a fresh destination buffer and wrap it as the RDMA
        # descriptor the server should RDMA-Write into.
        dst = torch.zeros(numel, dtype=torch.float32)
        retrieve_wrapper = RdmaWrapper.wrap(dst)
        retrieve_descriptor = DeviceIPCWrapper.Serialize(retrieve_wrapper)

        retrieve_future = client.submit_request(
            RequestType.RETRIEVE,
            [key, os.getpid(), [], retrieve_descriptor, 0],
        )
        _, retrieve_ok = retrieve_future.result(timeout=DEFAULT_TIMEOUT)

        assert retrieve_ok is True
        assert torch.equal(dst, src), (
            "RETRIEVE must write back the exact bytes STORE pulled in"
        )

    def test_retrieve_miss_returns_false(self, client: MessageQueueClient) -> None:
        """A key that was never stored must report a clean miss."""
        numel = CHUNK_SIZE
        dst = torch.zeros(numel, dtype=torch.float32)
        wrapper = RdmaWrapper.wrap(dst)
        descriptor = DeviceIPCWrapper.Serialize(wrapper)

        key = IPCCacheServerKey(
            model_name="rdma-thin-client-test",
            world_size=1,
            worker_id=0,
            token_ids=tuple(range(9000, 9000 + numel)),
            start=0,
            end=numel,
            request_id="never-stored-req",
        )
        future = client.submit_request(
            RequestType.RETRIEVE,
            [key, os.getpid(), [], descriptor, 0],
        )
        _, ok = future.result(timeout=DEFAULT_TIMEOUT)

        assert ok is False
