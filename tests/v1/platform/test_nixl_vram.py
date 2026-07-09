# SPDX-License-Identifier: Apache-2.0
"""Tests for the NIXL VRAM (GPUDirect-equivalent) path in NixlWrapper.

Two test classes:

- ``TestNixlWrapperVram``: unit tests with a mocked CUDA tensor.  No GPU or
  NIXL bindings required — verifies that ``NixlWrapper.wrap()`` sets
  ``mem_type='VRAM'`` and ``device_id=gpu_id`` for a CUDA tensor.

- ``TestNixlVramRoundtrip``: hardware integration tests that require a real
  CUDA GPU **and** NIXL bindings (nixl_cu12 or nixl_cu13).  Marked
  ``@pytest.mark.cuda_required`` and skipped automatically when either
  dependency is absent.  These tests drive a real STORE/RETRIEVE cycle with
  VRAM tensors over the NixlTransferModule path, validating LMCache-3br.

Skip guard for the hardware class:
  - ``torch.cuda.is_available()`` must be True
  - At least one of nixl._api / nixl_cu12._api / nixl_cu13._api must import
"""

from __future__ import annotations

import importlib
import multiprocessing as mp
import os
import sys
import time
from types import ModuleType
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
import torch

# ---------------------------------------------------------------------------
# Module-level skip: nixl bindings needed even for unit tests that mock the
# import path, because nixl_wrapper does a top-level import of nixl._api.
# We only skip the hardware roundtrip class, not the unit class.
# ---------------------------------------------------------------------------


def _nixl_available() -> bool:
    for modname in ("nixl._api", "nixl_cu12._api", "nixl_cu13._api"):
        try:
            importlib.import_module(modname)
            return True
        except ImportError:
            pass
    return False


# ---------------------------------------------------------------------------
# Helpers shared with test_nixl_wrapper.py
# ---------------------------------------------------------------------------


def _make_nixl_mock() -> ModuleType:
    mod = ModuleType("nixl._api")
    agent_instance = MagicMock()
    agent_instance.name = "test_agent"
    agent_instance.get_agent_metadata.return_value = b"agent_meta"
    agent_instance.register_memory.return_value = MagicMock()
    agent_instance.deregister_memory.return_value = None
    agent_instance.get_reg_descs.return_value = MagicMock()
    agent_cls = MagicMock(return_value=agent_instance)
    config_cls = MagicMock(return_value=MagicMock())
    mod.nixl_agent = agent_cls
    mod.nixl_agent_config = config_cls
    mod._agent_instance = agent_instance  # type: ignore[attr-defined]
    return mod


@pytest.fixture()
def patched_nixl(monkeypatch):
    """Inject a fake nixl._api and return the module mock."""
    fake_mod = _make_nixl_mock()
    monkeypatch.setitem(sys.modules, "nixl._api", fake_mod)
    monkeypatch.setitem(sys.modules, "nixl", MagicMock(_api=fake_mod))
    for key in list(sys.modules):
        if "nixl_wrapper" in key:
            monkeypatch.delitem(sys.modules, key, raising=False)
    yield fake_mod
    try:
        import importlib as _il
        nw = _il.import_module("lmcache.v1.platform.rdma.nixl_wrapper")
        nw._AGENT = None  # type: ignore[attr-defined]
        nw._REG_PTRS.clear()  # type: ignore[attr-defined]
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Unit tests — no GPU, no nixl
# ---------------------------------------------------------------------------


class TestNixlWrapperVram:
    """NixlWrapper.wrap() sets VRAM fields for a CUDA tensor.

    Uses a mocked nixl._api and a MagicMock tensor whose ``get_device()``
    returns a non-negative GPU id.  No real CUDA device needed.
    """

    def test_wrap_cpu_tensor_sets_dram(self, patched_nixl) -> None:
        """CPU tensor → mem_type='DRAM', device_id=0."""
        from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper

        tensor = torch.zeros(32, dtype=torch.float32)
        wrapper = NixlWrapper.wrap(tensor)

        assert wrapper.mem_type == "DRAM"
        assert wrapper.device_id == 0

    def test_wrap_cuda_tensor_sets_vram(self, patched_nixl, monkeypatch) -> None:
        """CUDA tensor (mocked) → mem_type='VRAM', device_id=gpu_id."""
        from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper

        fake_tensor = MagicMock(spec=torch.Tensor)
        fake_tensor.is_contiguous.return_value = True
        fake_tensor.get_device.return_value = 1  # GPU 1
        fake_tensor.numel.return_value = 64
        fake_tensor.element_size.return_value = 2  # bfloat16
        fake_tensor.data_ptr.return_value = 0xABCD0000
        fake_tensor.dtype = torch.bfloat16
        fake_tensor.shape = torch.Size([64])
        fake_tensor.stride.return_value = (1,)
        fake_tensor.storage_offset.return_value = 0

        wrapper = NixlWrapper.wrap(fake_tensor)

        assert wrapper.mem_type == "VRAM"
        assert wrapper.device_id == 1

    def test_wrap_cuda_device0_sets_device_id_0(self, patched_nixl, monkeypatch) -> None:
        """GPU 0 tensor → device_id=0 (not clamped to negative)."""
        from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper

        fake_tensor = MagicMock(spec=torch.Tensor)
        fake_tensor.is_contiguous.return_value = True
        fake_tensor.get_device.return_value = 0
        fake_tensor.numel.return_value = 16
        fake_tensor.element_size.return_value = 4
        fake_tensor.data_ptr.return_value = 0xBEEF0000
        fake_tensor.dtype = torch.float32
        fake_tensor.shape = torch.Size([16])
        fake_tensor.stride.return_value = (1,)
        fake_tensor.storage_offset.return_value = 0

        wrapper = NixlWrapper.wrap(fake_tensor)

        assert wrapper.mem_type == "VRAM"
        assert wrapper.device_id == 0

    def test_wrap_cuda_tensor_length_correct(self, patched_nixl) -> None:
        """Wrapper length = numel × element_size for a mocked CUDA tensor."""
        from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper

        fake_tensor = MagicMock(spec=torch.Tensor)
        fake_tensor.is_contiguous.return_value = True
        fake_tensor.get_device.return_value = 0
        fake_tensor.numel.return_value = 128
        fake_tensor.element_size.return_value = 2
        fake_tensor.data_ptr.return_value = 0xC0DE0000
        fake_tensor.dtype = torch.bfloat16
        fake_tensor.shape = torch.Size([128])
        fake_tensor.stride.return_value = (1,)
        fake_tensor.storage_offset.return_value = 0

        wrapper = NixlWrapper.wrap(fake_tensor)

        assert wrapper.length == 128 * 2

    def test_wrap_cuda_tensor_calls_get_reg_descs_with_vram(
        self, patched_nixl, monkeypatch
    ) -> None:
        """get_reg_descs is called with mem_type='VRAM' for a CUDA tensor."""
        from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper

        fake_tensor = MagicMock(spec=torch.Tensor)
        fake_tensor.is_contiguous.return_value = True
        fake_tensor.get_device.return_value = 2
        fake_tensor.numel.return_value = 32
        fake_tensor.element_size.return_value = 2
        fake_tensor.data_ptr.return_value = 0xDEAD0000
        fake_tensor.dtype = torch.bfloat16
        fake_tensor.shape = torch.Size([32])
        fake_tensor.stride.return_value = (1,)
        fake_tensor.storage_offset.return_value = 0

        NixlWrapper.wrap(fake_tensor)

        agent = patched_nixl._agent_instance
        calls = agent.get_reg_descs.call_args_list
        assert calls, "get_reg_descs was never called"
        _, kwargs = calls[-1]
        # Positional: (dlist, mem_type) — mem_type is second positional arg
        positional = calls[-1][0]
        assert positional[1] == "VRAM", (
            f"Expected mem_type='VRAM' in get_reg_descs call, got: {positional[1]!r}"
        )


# ---------------------------------------------------------------------------
# Hardware roundtrip — requires real CUDA GPU + nixl_cu12/cu13
# ---------------------------------------------------------------------------

_CUDA_AND_NIXL = torch.cuda.is_available() and _nixl_available()

# Lazily import so the file parses cleanly on CPU-only hosts.
if _CUDA_AND_NIXL:
    from lmcache.v1.distributed.config import (
        EvictionConfig,
        L1ManagerConfig,
        L1MemoryManagerConfig,
        StorageManagerConfig,
    )
    from lmcache.v1.mp_observability.config import DEFAULT_OBSERVABILITY_CONFIG
    from lmcache.v1.multiprocess.config import MPServerConfig
    from lmcache.v1.multiprocess.server import run_cache_server
    from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper
    from lmcache.v1.platform.rdma.thin_client import RdmaThinClient

_VRAM_SERVER_PORT = 5608
_VRAM_POOL_MB = 1


def _vram_server_runner(host: str, port: int) -> None:
    os.environ["LMCACHE_NIXL_POOL_SIZE_MB"] = str(_VRAM_POOL_MB)
    mp_config = MPServerConfig(
        host=host,
        port=port,
        chunk_size=2,
        supported_transfer_mode="nixl",
    )
    storage_cfg = StorageManagerConfig(
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
        storage_manager_config=storage_cfg,
        obs_config=DEFAULT_OBSERVABILITY_CONFIG,
        start_prometheus_http_server=False,
    )


@pytest.fixture(scope="module")
def vram_server() -> Generator[mp.Process, None, None]:
    import zmq

    mp.set_start_method("spawn", force=True)
    proc = mp.Process(
        target=_vram_server_runner,
        args=("localhost", _VRAM_SERVER_PORT),
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
def vram_client(vram_server: mp.Process) -> Generator["RdmaThinClient", None, None]:
    import zmq

    ctx = zmq.Context.instance()
    c = RdmaThinClient(
        server_url=f"tcp://localhost:{_VRAM_SERVER_PORT}",
        model_name="nixl-vram-bench",
        timeout=30.0,
        zmq_context=ctx,
        wrapper_cls=NixlWrapper,
    )
    yield c
    c.close()


def _gpu_kv_tensor(seed: int = 0, device: int = 0) -> torch.Tensor:
    """Return a small KV-shaped CUDA tensor for VRAM path testing."""
    # 4 layers × 32 heads × 128 dim = 16384 bfloat16 elements (32 KB)
    numel = 4 * 32 * 128
    t = torch.arange(numel, dtype=torch.float32) * 0.001 + float(seed)
    return t.to(torch.bfloat16).cuda(device)


@pytest.mark.cuda_required
@pytest.mark.skipif(not _CUDA_AND_NIXL, reason="CUDA GPU + nixl bindings required")
class TestNixlVramRoundtrip:
    """Hardware: STORE/RETRIEVE of a VRAM tensor over NIXL (LMCache-3br).

    Requires: CUDA-capable GPU + nixl_cu12 or nixl_cu13 installed.
    Run on bmg0/bmg1 with UCX_NET_DEVICES=mlx5_1:1 (bmg0) or mlx5_0:1 (bmg1).
    """

    def test_vram_wrap_fields(self) -> None:
        """CUDA tensor → wrap() sets mem_type=VRAM and device_id=gpu_id."""
        gpu_id = 0
        tensor = _gpu_kv_tensor(seed=1, device=gpu_id)
        wrapper = NixlWrapper.wrap(tensor)

        assert wrapper.mem_type == "VRAM", (
            f"Expected VRAM, got {wrapper.mem_type!r}"
        )
        assert wrapper.device_id == gpu_id
        assert wrapper.length == tensor.numel() * tensor.element_size()
        assert wrapper.base_addr == tensor.data_ptr()

    def test_vram_store_retrieve_roundtrip(
        self, vram_client: "RdmaThinClient", vram_server: mp.Process
    ) -> None:
        """STORE then RETRIEVE of a VRAM tensor preserves all bytes."""
        assert vram_server.is_alive(), "VRAM server died before test"
        src = _gpu_kv_tensor(seed=42)

        ok = vram_client.store(
            request_id="vram-roundtrip-0",
            token_ids=list(range(9000, 9004)),
            data=src,
        )
        assert ok is True, "VRAM STORE failed"

        result = vram_client.retrieve(
            request_id="vram-roundtrip-0",
            token_ids=list(range(9000, 9004)),
            numel=src.numel(),
            dtype=src.dtype,
        )
        assert result is not None, "VRAM RETRIEVE returned None (cache miss)"

        # Move result to same device for comparison
        result_gpu = result.cuda(src.get_device()) if not result.is_cuda else result
        assert torch.equal(result_gpu, src), (
            f"VRAM data mismatch: max delta "
            f"{(result_gpu.float() - src.float()).abs().max():.6f}"
        )

    def test_vram_sequential_stores_no_mr_leak(
        self, vram_client: "RdmaThinClient", vram_server: mp.Process
    ) -> None:
        """5 sequential VRAM STOREs complete without hanging (MR leak check)."""
        assert vram_server.is_alive(), "VRAM server died before test"
        for i in range(5):
            src = _gpu_kv_tensor(seed=i)
            tok_start = 8000 + i * 4
            ok = vram_client.store(
                request_id=f"vram-leak-{i}",
                token_ids=list(range(tok_start, tok_start + 4)),
                data=src,
            )
            assert ok is True, f"VRAM STORE {i} failed"
        assert vram_server.is_alive(), "Server died during sequential VRAM STOREs"

    def test_vram_retrieve_miss_returns_none(
        self, vram_client: "RdmaThinClient", vram_server: mp.Process
    ) -> None:
        """RETRIEVE of a key that was never stored returns None."""
        assert vram_server.is_alive(), "VRAM server died before test"
        result = vram_client.retrieve(
            request_id="vram-miss-0",
            token_ids=list(range(99900, 99904)),
            numel=4 * 32 * 128,
            dtype=torch.bfloat16,
        )
        assert result is None, f"Expected None for VRAM miss, got {result}"
