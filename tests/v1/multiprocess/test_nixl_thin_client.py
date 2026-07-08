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

       uv pip install nixl-cu12  # or nixl-cu13

2. The ConnectX-7 NIC must be present (UCX falls back to loopback if not).

3. Run with LMCACHE_NIXL_BACKENDS=UCX (the default)::

       uv run pytest tests/v1/multiprocess/test_nixl_thin_client.py -xvs

This test is automatically skipped if ``nixl`` is not importable.
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
from lmcache.v1.multiprocess.mq import MessageQueueClient
from lmcache.v1.multiprocess.protocols.base import RequestType
from lmcache.v1.multiprocess.server import run_cache_server
from lmcache.v1.platform.base_ipc_wrapper import DeviceIPCWrapper
from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper

SERVER_HOST = "localhost"
SERVER_PORT = 5605  # Different port from RDMA test (5601) to avoid conflicts.
SERVER_URL = f"tcp://{SERVER_HOST}:{SERVER_PORT}"
CHUNK_SIZE = 4
DEFAULT_TIMEOUT = 20.0


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

    # Wait until the server's ZMQ socket is ready.
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.RCVTIMEO, 1000)
    sock.connect(SERVER_URL)
    deadline = time.monotonic() + DEFAULT_TIMEOUT
    while time.monotonic() < deadline:
        try:
            sock.send(b"ping")
            sock.recv()
            break
        except zmq.Again:
            time.sleep(0.2)
    sock.close(linger=0)

    yield proc

    proc.terminate()
    proc.join(timeout=5)


@pytest.fixture(scope="module")
def mq_client(nixl_server_process: mp.Process) -> Generator[MessageQueueClient, None, None]:
    """Return an MQ client connected to the NIXL server."""
    client = MessageQueueClient(SERVER_URL, timeout=DEFAULT_TIMEOUT)
    yield client
    client.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_key(token_ids: list[int] | None = None) -> IPCCacheServerKey:
    return IPCCacheServerKey(
        model_name="test-model",
        world_size=1,
        worker_id=0,
        token_ids=tuple(token_ids or [10, 20, 30, 40]),
        start=0,
        end=4,
        request_id="req-nixl-0",
    )


def _send_store(mq_client: MessageQueueClient, key: IPCCacheServerKey, tensor: torch.Tensor) -> bool:
    """Wrap ``tensor`` via NIXL and send a STORE request."""
    wrapper = NixlWrapper.wrap(tensor)
    descriptor = DeviceIPCWrapper.Serialize(wrapper)
    future = mq_client.send_request(RequestType.STORE, [key, 0, [], descriptor])
    _result, succeeded = future.result(timeout=DEFAULT_TIMEOUT)
    return succeeded


def _send_retrieve(
    mq_client: MessageQueueClient,
    key: IPCCacheServerKey,
    dst: torch.Tensor,
) -> bool:
    """Wrap ``dst`` via NIXL and send a RETRIEVE request."""
    wrapper = NixlWrapper.wrap(dst)
    descriptor = DeviceIPCWrapper.Serialize(wrapper)
    future = mq_client.send_request(RequestType.RETRIEVE, [key, 0, [], descriptor])
    _result, succeeded = future.result(timeout=DEFAULT_TIMEOUT)
    return succeeded


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestNixlThinClientE2E:
    """End-to-end STORE → RETRIEVE round-trip via NIXL."""

    def test_store_succeeds(self, mq_client: MessageQueueClient) -> None:
        key = _make_key([1, 2, 3, 4])
        src = torch.arange(16, dtype=torch.float32)
        succeeded = _send_store(mq_client, key, src)
        assert succeeded

    def test_store_then_retrieve_returns_same_data(
        self, mq_client: MessageQueueClient
    ) -> None:
        key = _make_key([5, 6, 7, 8])
        src = torch.arange(16, dtype=torch.float32)
        assert _send_store(mq_client, key, src)

        dst = torch.zeros(16, dtype=torch.float32)
        succeeded = _send_retrieve(mq_client, key, dst)
        assert succeeded
        assert torch.allclose(dst, src), f"Mismatch: max abs diff={abs(dst - src).max()}"

    def test_retrieve_miss_returns_false(self, mq_client: MessageQueueClient) -> None:
        """A key that was never stored returns success=False."""
        key = _make_key([999, 1000, 1001, 1002])
        dst = torch.zeros(16, dtype=torch.float32)
        succeeded = _send_retrieve(mq_client, key, dst)
        assert not succeeded

    def test_repeated_stores_are_idempotent(self, mq_client: MessageQueueClient) -> None:
        """Storing the same key twice does not corrupt the retrieved data."""
        key = _make_key([100, 101, 102, 103])
        src = torch.ones(16, dtype=torch.float32) * 3.14
        _send_store(mq_client, key, src)
        _send_store(mq_client, key, src)

        dst = torch.zeros(16, dtype=torch.float32)
        succeeded = _send_retrieve(mq_client, key, dst)
        assert succeeded
        assert torch.allclose(dst, src)
