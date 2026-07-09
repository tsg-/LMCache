# SPDX-License-Identifier: Apache-2.0
"""Synthetic KV-cache bench for the NIXL transfer path (no GPU required).

Validates the full NixlTransferModule STORE/RETRIEVE lifecycle using
CPU DRAM tensors shaped like real KV cache pages (layers × heads × dim),
exercising the same code path that bmg0/bmg1 hardware validation (LMCache-3br)
targets.  This test runs on any machine that has nixl installed.

GPU VRAM substitution: CPU tensors with mem_type='DRAM' exercise identical
NixlWrapper descriptor construction, per-call register_memory + initialize_xfer
+ poll + deregister, and ZMQ protocol as VRAM tensors.  The only difference is
mem_type; the NIXL VRAM path (LMCache-3br) validates the VRAM=True branch on
hardware.

Test design:
- Multi-chunk KV pages: 32 layers × 32 heads × 128 dim, dtype=bfloat16
  (~512 KB per request at 2 chunks/request = 256 KB/chunk, within pool page)
- Sequential STORE then RETRIEVE with data integrity check (allclose)
- Repeated transfers (5 requests) to catch MR registration leaks
- Pool-enabled server: LMCACHE_NIXL_POOL_SIZE_MB=1 to exercise pre-registered
  arena path

This test is skipped automatically if nixl is not installed (same skip guard
as test_nixl_thin_client.py).
"""

from __future__ import annotations

import importlib
import multiprocessing as mp
import os
import time
from typing import Generator

import pytest
import torch
import zmq

# Skip if nixl not available.
for _nixl_modname in ("nixl._api", "nixl_cu12._api", "nixl_cu13._api"):
    try:
        importlib.import_module(_nixl_modname)
        break
    except ImportError:
        pass
else:
    pytest.skip(
        "nixl not installed; skipping NIXL synthetic bench tests",
        allow_module_level=True,
    )

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
from lmcache.v1.platform.rdma.thin_client import RdmaThinClient

SERVER_HOST = "localhost"
SERVER_PORT = 5607
SERVER_URL = f"tcp://{SERVER_HOST}:{SERVER_PORT}"

# KV shape parameters — matches a small Llama-style config
_NUM_LAYERS = 4
_NUM_HEADS = 32
_HEAD_DIM = 128
_DTYPE = torch.bfloat16
_BYTES_PER_ELEMENT = 2  # bfloat16

# 2 chunks per request: chunk_size=2 tokens, total 4 tokens/request.
_CHUNK_SIZE = 2
_TOKENS_PER_REQUEST = 4

# Pool enabled so we also exercise the pre-registered arena path.
_POOL_MB = 1

DEFAULT_TIMEOUT = 30.0
_NUM_REQUESTS = 5


def _kv_tensor(seed: int = 0) -> torch.Tensor:
    """Return a single KV page as a flat CPU tensor seeded for determinism.

    Shape: [num_layers * num_heads * head_dim] bfloat16, filled with a
    recognisable pattern so allclose catches any byte-swap corruption.
    """
    numel = _NUM_LAYERS * _NUM_HEADS * _HEAD_DIM
    t = torch.arange(numel, dtype=torch.float32) * 0.001 + float(seed)
    return t.to(_DTYPE)


def _server_process_runner(host: str, port: int) -> None:
    """Entry point for the synthetic-bench NIXL server subprocess."""
    os.environ["LMCACHE_NIXL_POOL_SIZE_MB"] = str(_POOL_MB)
    mp_config = MPServerConfig(
        host=host,
        port=port,
        chunk_size=_CHUNK_SIZE,
        supported_transfer_mode="nixl",
    )
    # 64 MB arena — enough for _NUM_REQUESTS × 2 chunks in flight.
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
def bench_server() -> Generator[mp.Process, None, None]:
    mp.set_start_method("spawn", force=True)
    proc = mp.Process(
        target=_server_process_runner,
        args=(SERVER_HOST, SERVER_PORT),
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
def zmq_ctx() -> Generator[zmq.Context, None, None]:
    ctx = zmq.Context.instance()
    yield ctx


@pytest.fixture(scope="module")
def bench_client(
    bench_server: mp.Process, zmq_ctx: zmq.Context
) -> Generator[RdmaThinClient, None, None]:
    c = RdmaThinClient(
        server_url=SERVER_URL,
        model_name="nixl-synth-bench",
        timeout=DEFAULT_TIMEOUT,
        zmq_context=zmq_ctx,
        wrapper_cls=NixlWrapper,
    )
    yield c
    c.close()


def _make_key(seq: int) -> IPCCacheServerKey:
    tok_start = seq * _TOKENS_PER_REQUEST
    return IPCCacheServerKey(
        model_name="nixl-synth-bench",
        world_size=1,
        worker_id=0,
        token_ids=tuple(range(tok_start, tok_start + _TOKENS_PER_REQUEST)),
        start=0,
        end=_TOKENS_PER_REQUEST,
        request_id=f"synth-{seq}",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.multiprocess
class TestNixlSyntheticBench:
    """Synthetic KV bench — STORE/RETRIEVE integrity over NIXL with CPU tensors."""

    def test_single_store_retrieve_roundtrip(
        self, bench_client: RdmaThinClient, bench_server: mp.Process
    ) -> None:
        """STORE then RETRIEVE of a single KV page preserves all bytes."""
        assert bench_server.is_alive(), "Bench server died before test"
        src = _kv_tensor(seed=42)

        ok = bench_client.store(
            request_id="synth-roundtrip-0",
            token_ids=list(range(1000, 1000 + _TOKENS_PER_REQUEST)),
            data=src,
        )
        assert ok is True, "STORE failed"

        result = bench_client.retrieve(
            request_id="synth-roundtrip-0",
            token_ids=list(range(1000, 1000 + _TOKENS_PER_REQUEST)),
            numel=src.numel(),
            dtype=_DTYPE,
        )
        assert result is not None, "RETRIEVE returned None (cache miss)"
        assert result.dtype == _DTYPE
        assert torch.equal(result, src), (
            f"Data mismatch after roundtrip: max delta "
            f"{(result.float() - src.float()).abs().max():.6f}"
        )

    def test_sequential_stores_no_mr_leak(
        self, bench_client: RdmaThinClient, bench_server: mp.Process
    ) -> None:
        """N sequential STOREs complete without hanging — catches MR registration leaks."""
        assert bench_server.is_alive(), "Bench server died before test"
        start_seq = 2000
        for i in range(_NUM_REQUESTS):
            src = _kv_tensor(seed=i)
            tok_start = start_seq + i * _TOKENS_PER_REQUEST
            ok = bench_client.store(
                request_id=f"synth-leak-{i}",
                token_ids=list(range(tok_start, tok_start + _TOKENS_PER_REQUEST)),
                data=src,
            )
            assert ok is True, f"STORE {i} failed"

        assert bench_server.is_alive(), "Server died during sequential STOREs"

    def test_all_requests_retrieve_correct_data(
        self, bench_client: RdmaThinClient, bench_server: mp.Process
    ) -> None:
        """5 distinct keys: STORE all, then RETRIEVE all, verify each."""
        assert bench_server.is_alive(), "Bench server died before test"
        start_seq = 3000
        srcs = {i: _kv_tensor(seed=100 + i) for i in range(_NUM_REQUESTS)}

        for i, src in srcs.items():
            tok_start = start_seq + i * _TOKENS_PER_REQUEST
            ok = bench_client.store(
                request_id=f"synth-all-{i}",
                token_ids=list(range(tok_start, tok_start + _TOKENS_PER_REQUEST)),
                data=src,
            )
            assert ok is True, f"STORE {i} failed"

        mismatches = []
        for i, src in srcs.items():
            tok_start = start_seq + i * _TOKENS_PER_REQUEST
            result = bench_client.retrieve(
                request_id=f"synth-all-{i}",
                token_ids=list(range(tok_start, tok_start + _TOKENS_PER_REQUEST)),
                numel=src.numel(),
                dtype=_DTYPE,
            )
            if result is None:
                mismatches.append(f"seq={i}: RETRIEVE returned None")
            elif not torch.equal(result, src):
                delta = (result.float() - src.float()).abs().max()
                mismatches.append(f"seq={i}: max delta={delta:.6f}")

        assert not mismatches, "Data mismatches:\n" + "\n".join(mismatches)

    def test_retrieve_miss_returns_none(
        self, bench_client: RdmaThinClient, bench_server: mp.Process
    ) -> None:
        """RETRIEVE of a key that was never stored returns None."""
        assert bench_server.is_alive(), "Bench server died before test"
        result = bench_client.retrieve(
            request_id="synth-miss-0",
            token_ids=list(range(99000, 99000 + _TOKENS_PER_REQUEST)),
            numel=_kv_tensor().numel(),
            dtype=_DTYPE,
        )
        assert result is None, f"Expected None for miss, got {result}"

    def test_pool_path_store_retrieve(
        self, bench_client: RdmaThinClient, bench_server: mp.Process
    ) -> None:
        """Explicitly small chunks (< 256 KB) exercise the pre-registered pool path."""
        assert bench_server.is_alive(), "Bench server died before pool test"
        # Small tensor: 256 elements, well within pool page (256 KB).
        numel = 256
        src = torch.arange(numel, dtype=_DTYPE)
        tok_start = 5000

        ok = bench_client.store(
            request_id="synth-pool-0",
            token_ids=list(range(tok_start, tok_start + _TOKENS_PER_REQUEST)),
            data=src,
        )
        assert ok is True, "Pool-path STORE failed"

        result = bench_client.retrieve(
            request_id="synth-pool-0",
            token_ids=list(range(tok_start, tok_start + _TOKENS_PER_REQUEST)),
            numel=numel,
            dtype=_DTYPE,
        )
        assert result is not None, "Pool-path RETRIEVE returned None"
        assert torch.equal(result, src), "Pool-path data mismatch"
