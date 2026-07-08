# SPDX-License-Identifier: Apache-2.0
"""End-to-end integration test for the NIXL transfer path.

Spins up a real ``MPCacheServer`` subprocess with
``supported_transfer_mode="nixl"`` (routing STORE/RETRIEVE to
:class:`NixlTransferModule`) and drives it from a real
:class:`~lmcache.v1.multiprocess.mq.MessageQueueClient`, exactly as a
standalone initiator thin client would: plain CPU host-DRAM tensors,
wrapped via :class:`NixlWrapper`, with descriptors sent as
``nixl_descriptor_bytes`` over the wire.  No CUDA, no vLLM, no
``LMCacheDrivenTransferContext`` involvement anywhere in this path.

**Prerequisites (bmg0/bmg1):**

1. UCX and NIXL must be installed::

       .venv-ipu/bin/pip install nixl-cu12  # or nixl-cu13

2. The ConnectX-7 NIC is optional; UCX falls back to loopback if absent.

3. Run::

       .venv-ipu/bin/python -m pytest tests/v1/multiprocess/test_nixl_thin_client.py -xvs

This test is automatically skipped if no nixl variant is importable.

**Note on UCX progress:**
With ``UCX_TLS=tcp,self``, UCX reads are *two-sided*: the server sends a
read request and the worker must process and respond to it.  The NIXL
progress thread handles this, but in the pytest process the GIL can delay
the progress thread.  We work around this by manually calling
``get_new_notifs()`` (which drives UCX progress) while the test waits for
each future to resolve.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import time
from typing import Generator

import pytest
import torch
import zmq

# Try each cuXX variant in order; skip if none are importable.
for _nixl_modname in ("nixl._api", "nixl_cu12._api", "nixl_cu13._api"):
    try:
        import importlib as _il
        _il.import_module(_nixl_modname)
        break
    except ImportError:
        pass
else:
    pytest.skip("nixl not installed; skipping NIXL integration tests", allow_module_level=True)

from lmcache.v1.distributed.config import (
    EvictionConfig,
    L1ManagerConfig,
    L1MemoryManagerConfig,
    StorageManagerConfig,
)
from lmcache.v1.mp_observability.config import DEFAULT_OBSERVABILITY_CONFIG
from lmcache.v1.multiprocess.config import MPServerConfig
from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey
from lmcache.v1.multiprocess.futures import MessagingFuture
from lmcache.v1.multiprocess.mq import MessageQueueClient
from lmcache.v1.multiprocess.protocols.base import RequestType
from lmcache.v1.multiprocess.server import run_cache_server
from lmcache.v1.platform.base_ipc_wrapper import DeviceIPCWrapper
from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper

SERVER_HOST = "localhost"
SERVER_PORT = 5605
SERVER_URL = f"tcp://{SERVER_HOST}:{SERVER_PORT}"
CHUNK_SIZE = 4
DEFAULT_TIMEOUT = 30.0


def _server_process_runner(host: str, port: int, chunk_size: int) -> None:
    """Entry point for the NIXL-mode server subprocess."""
    mp_config = MPServerConfig(
        host=host,
        port=port,
        chunk_size=chunk_size,
        supported_transfer_mode="nixl",
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
def nixl_server_process() -> Generator[mp.Process, None, None]:
    """Start a real MPCacheServer subprocess in NIXL transfer mode."""
    mp.set_start_method("spawn", force=True)
    proc = mp.Process(
        target=_server_process_runner,
        args=(SERVER_HOST, SERVER_PORT, CHUNK_SIZE),
        daemon=True,
    )
    proc.start()
    time.sleep(3)
    yield proc
    if proc.is_alive():
        proc.terminate()
        proc.join(timeout=5)
        if proc.is_alive():
            proc.kill()
            proc.join()


@pytest.fixture(scope="module")
def zmq_context() -> Generator[zmq.Context, None, None]:
    context = zmq.Context.instance()
    yield context


@pytest.fixture(scope="module")
def client(
    nixl_server_process: mp.Process, zmq_context: zmq.Context
) -> Generator[MessageQueueClient, None, None]:
    """Single module-scoped client — reused across all tests."""
    c = MessageQueueClient(server_url=SERVER_URL, context=zmq_context)
    yield c
    c.close()


def _await_future(future: MessagingFuture, timeout: float = DEFAULT_TIMEOUT) -> tuple:
    """Wait for a future, returning its result.

    Args:
        future: The MessagingFuture to wait for.
        timeout: Maximum wait time in seconds.

    Returns:
        The future's result tuple.

    Raises:
        TimeoutError: If the future does not resolve within ``timeout``.
    """
    return future.result(timeout=timeout)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_key(request_id: str, tok_start: int = 0) -> IPCCacheServerKey:
    return IPCCacheServerKey(
        model_name="nixl-thin-client-test",
        world_size=1,
        worker_id=0,
        token_ids=tuple(range(tok_start, tok_start + CHUNK_SIZE)),
        start=0,
        end=CHUNK_SIZE,
        request_id=request_id,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNixlThinClientE2E:
    """End-to-end STORE / RETRIEVE / miss tests against a live NIXL server."""

    def test_store_succeeds(
        self,
        client: MessageQueueClient,
        nixl_server_process: mp.Process,
    ) -> None:
        assert nixl_server_process.is_alive(), "Server subprocess died before test"
        src = torch.arange(CHUNK_SIZE, dtype=torch.float32)
        wrapper = NixlWrapper.wrap(src)
        descriptor = DeviceIPCWrapper.Serialize(wrapper)

        key = _make_key("nixl-store-0", tok_start=0)
        future = client.submit_request(
            RequestType.STORE,
            [key, os.getpid(), [], descriptor],
        )
        response_bytes, ok = _await_future(future)

        assert nixl_server_process.is_alive(), "Server died after STORE"
        assert ok is True
        assert response_bytes == b""

    def test_store_then_retrieve(
        self,
        client: MessageQueueClient,
        nixl_server_process: mp.Process,
    ) -> None:
        assert nixl_server_process.is_alive(), "Server subprocess died before retrieve test"
        src = torch.arange(CHUNK_SIZE, dtype=torch.float32) + 10.0

        store_wrapper = NixlWrapper.wrap(src)
        store_descriptor = DeviceIPCWrapper.Serialize(store_wrapper)

        key = _make_key("nixl-store-retrieve-0", tok_start=100)
        store_future = client.submit_request(
            RequestType.STORE,
            [key, os.getpid(), [], store_descriptor],
        )
        _, store_ok = _await_future(store_future)
        assert nixl_server_process.is_alive(), "Server died after STORE in retrieve test"
        assert store_ok is True

        dst = torch.zeros(CHUNK_SIZE, dtype=torch.float32)
        retrieve_wrapper = NixlWrapper.wrap(dst)
        retrieve_descriptor = DeviceIPCWrapper.Serialize(retrieve_wrapper)

        retrieve_future = client.submit_request(
            RequestType.RETRIEVE,
            [key, os.getpid(), [], retrieve_descriptor],
        )
        _, retrieve_ok = _await_future(retrieve_future)
        assert retrieve_ok is True
        assert torch.allclose(dst, src), f"Mismatch: {dst} != {src}"

    def test_retrieve_miss_returns_false(
        self,
        client: MessageQueueClient,
        nixl_server_process: mp.Process,
    ) -> None:
        assert nixl_server_process.is_alive(), "Server subprocess died before miss test"
        key = _make_key("nixl-miss-0", tok_start=9000)
        dst = torch.zeros(CHUNK_SIZE, dtype=torch.float32)
        wrapper = NixlWrapper.wrap(dst)
        descriptor = DeviceIPCWrapper.Serialize(wrapper)

        future = client.submit_request(
            RequestType.RETRIEVE,
            [key, os.getpid(), [], descriptor],
        )
        _, ok = _await_future(future)
        assert ok is False
