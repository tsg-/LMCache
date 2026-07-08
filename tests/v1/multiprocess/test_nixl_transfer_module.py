# SPDX-License-Identifier: Apache-2.0
"""Unit tests for NixlTransferModule (STORE and RETRIEVE handlers).

All nixl imports are mocked — no NIXL hardware or Python bindings required.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from types import ModuleType
from typing import Iterator
from unittest.mock import MagicMock, call, patch

import pytest
import torch

from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey, PrefetchHandle
from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey
from lmcache.v1.multiprocess.protocols.base import RequestType
from lmcache.v1.platform.base_ipc_wrapper import DeviceIPCWrapper


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


def _make_nixl_agent_mock(name: str = "server_agent") -> MagicMock:
    """Return a minimal nixl_agent mock with the key transfer API."""
    agent = MagicMock()
    agent.name = name
    agent.backends = {"UCX": MagicMock()}

    # Return "DONE" from transfer() by default.
    agent.transfer.return_value = "DONE"
    agent.check_xfer_state.return_value = "DONE"

    # Return opaque handles.
    agent.initialize_xfer.return_value = MagicMock()
    agent.get_xfer_descs.return_value = MagicMock()
    agent.get_reg_descs.return_value = MagicMock()
    agent.register_memory.return_value = MagicMock()
    agent.add_remote_agent.return_value = "remote_agent"
    return agent


def _make_nixl_module_mock():
    """Return a fake nixl._api module."""
    mod = ModuleType("nixl._api")
    agent_mock = _make_nixl_agent_mock()
    mod.nixl_agent = MagicMock(return_value=agent_mock)
    mod.nixl_agent_config = MagicMock(return_value=MagicMock())
    mod._agent_instance = agent_mock
    return mod


def _make_ipc_key() -> IPCCacheServerKey:
    return IPCCacheServerKey(
        model_name="test-model",
        world_size=1,
        worker_id=0,
        token_ids=tuple([1, 2, 3]),
        start=0,
        end=3,
        request_id="req-0",
    )


def _make_obj_key(idx: int = 0) -> ObjectKey:
    return ObjectKey(
        chunk_hash=idx.to_bytes(4, "big"),
        model_name="test-model",
        kv_rank=0,
    )


def _make_mem_obj(ptr: int, size: int = 64) -> MagicMock:
    obj = MagicMock()
    obj.data_ptr = ptr
    obj.get_size.return_value = size
    return obj


def _make_nixl_wrapper_bytes(length: int = 64) -> bytes:
    """Return serialized NixlWrapper bytes with a dummy agent descriptor."""
    from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper

    w = NixlWrapper(
        agent_name="worker_agent",
        agent_metadata=b"agent_meta",
        base_addr=0x1000,
        length=length,
        device_id=0,
        mem_type="DRAM",
        shape=(length // 4,),
        dtype=torch.float32,
        stride=(1,),
        storage_offset=0,
    )
    return DeviceIPCWrapper.Serialize(w)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_nixl(monkeypatch):
    """Inject a fake nixl._api so NixlTransferModule can be imported."""
    fake_mod = _make_nixl_module_mock()
    monkeypatch.setitem(sys.modules, "nixl._api", fake_mod)
    monkeypatch.setitem(sys.modules, "nixl", MagicMock(_api=fake_mod))

    # Clear module cache so the patched import takes effect.
    for key in list(sys.modules):
        if "nixl_transfer" in key:
            monkeypatch.delitem(sys.modules, key, raising=False)

    yield fake_mod


@pytest.fixture()
def mock_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.storage_manager = MagicMock()
    return ctx


@pytest.fixture()
def module(mock_ctx: MagicMock) -> "NixlTransferModule":
    from lmcache.v1.multiprocess.modules.nixl_transfer import NixlTransferModule

    return NixlTransferModule(mock_ctx)


# ---------------------------------------------------------------------------
# TestHandlers
# ---------------------------------------------------------------------------


class TestNixlTransferModuleHandlers:
    """get_handlers() and report_status() contract."""

    def test_get_handlers_returns_store_and_retrieve(self, module) -> None:
        specs = module.get_handlers()
        types = {s.request_type for s in specs}
        assert RequestType.STORE in types
        assert RequestType.RETRIEVE in types

    def test_get_handlers_returns_exactly_two(self, module) -> None:
        assert len(module.get_handlers()) == 2

    def test_no_register_kv_cache_handler(self, module) -> None:
        types = {s.request_type for s in module.get_handlers()}
        assert RequestType.REGISTER_KV_CACHE not in types

    def test_report_status_contains_nixl_transfer_key(self, module) -> None:
        status = module.report_status()
        assert "nixl_transfer" in status
        assert "agent" in status["nixl_transfer"]


# ---------------------------------------------------------------------------
# TestStore
# ---------------------------------------------------------------------------


class TestNixlTransferModuleStore:
    """store() correctness."""

    def _setup_single_chunk(
        self, mock_ctx: MagicMock, tensor: torch.Tensor
    ) -> tuple[ObjectKey, MagicMock]:
        obj_key = _make_obj_key(0)
        mem_obj = _make_mem_obj(tensor.data_ptr(), tensor.numel() * tensor.element_size())
        mock_ctx.resolve_obj_keys.return_value = [[obj_key]]
        mock_ctx.storage_manager.reserve_write.return_value = {obj_key: mem_obj}
        return obj_key, mem_obj

    def test_store_single_chunk_succeeds(self, module, mock_ctx: MagicMock) -> None:
        nbytes = 64
        src = torch.zeros(nbytes // 4, dtype=torch.float32)
        obj_key, _ = self._setup_single_chunk(mock_ctx, src)

        descriptor = _make_nixl_wrapper_bytes(nbytes)
        _, succeeded = module.store(_make_ipc_key(), 1, [], descriptor)

        assert succeeded is True
        mock_ctx.storage_manager.finish_write.assert_called_once_with([obj_key])

    def test_store_zero_chunks_returns_true(self, module, mock_ctx: MagicMock) -> None:
        mock_ctx.resolve_obj_keys.return_value = [[]]
        descriptor = _make_nixl_wrapper_bytes(64)
        _, succeeded = module.store(_make_ipc_key(), 1, [], descriptor)
        assert succeeded is True

    def test_store_wrong_wrapper_type_raises(self, module, mock_ctx: MagicMock) -> None:
        # Use the RdmaWrapper (which is picklable) to simulate a non-NixlWrapper.
        from lmcache.v1.platform.rdma.rdma_transport import (
            MrInfo,
            StubRdmaTransport,
            set_rdma_transport,
        )
        from lmcache.v1.platform.rdma.rdma_wrapper import RdmaWrapper, _REGISTERED_MRS

        transport = StubRdmaTransport()
        set_rdma_transport(transport)
        try:
            t = torch.zeros(8, dtype=torch.float32)
            wrapper = RdmaWrapper.wrap(t)
            descriptor = DeviceIPCWrapper.Serialize(wrapper)
        finally:
            set_rdma_transport(None)  # type: ignore[arg-type]
            _REGISTERED_MRS.clear()

        with pytest.raises(ValueError, match="NixlWrapper"):
            module.store(_make_ipc_key(), 1, [], descriptor)

    def test_store_length_not_divisible_raises(
        self, module, mock_ctx: MagicMock
    ) -> None:
        """Three-chunk request with length not divisible by 3 raises ValueError."""
        keys = [_make_obj_key(i) for i in range(3)]
        mock_ctx.resolve_obj_keys.return_value = [keys]

        from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper

        w = NixlWrapper(
            agent_name="x",
            agent_metadata=b"",
            base_addr=0x1000,
            length=65,  # 65 / 3 is not exact
            device_id=0,
            mem_type="DRAM",
            shape=(65,),
            dtype=torch.uint8,
            stride=(1,),
            storage_offset=0,
        )
        descriptor = DeviceIPCWrapper.Serialize(w)
        with pytest.raises(ValueError, match="evenly divisible"):
            module.store(_make_ipc_key(), 1, [], descriptor)

    def test_store_adds_remote_agent(self, module, mock_ctx: MagicMock, _patch_nixl) -> None:
        """store() calls add_remote_agent with the wrapper's agent_metadata."""
        nbytes = 64
        src = torch.zeros(nbytes // 4, dtype=torch.float32)
        self._setup_single_chunk(mock_ctx, src)

        descriptor = _make_nixl_wrapper_bytes(nbytes)
        module.store(_make_ipc_key(), 1, [], descriptor)

        module._agent.add_remote_agent.assert_called_once_with(b"agent_meta")

    def test_store_xfer_timeout_returns_false(
        self, module, mock_ctx: MagicMock, _patch_nixl
    ) -> None:
        """When transfer() returns PROC and check_xfer_state never DONE, all_succeeded=False."""
        nbytes = 64
        src = torch.zeros(nbytes // 4, dtype=torch.float32)
        obj_key, _ = self._setup_single_chunk(mock_ctx, src)

        module._agent.transfer.return_value = "PROC"
        module._agent.check_xfer_state.return_value = "ERR"

        descriptor = _make_nixl_wrapper_bytes(nbytes)

        with patch(
            "lmcache.v1.multiprocess.modules.nixl_transfer._XFER_TIMEOUT_S",
            0.01,
        ):
            _, succeeded = module.store(_make_ipc_key(), 1, [], descriptor)

        assert succeeded is False
        # finish_write is still called (with empty commit list on failure).
        mock_ctx.storage_manager.finish_write.assert_called_once_with([])

    def test_store_multi_chunk_calls_finish_write_per_chunk(
        self, module, mock_ctx: MagicMock
    ) -> None:
        nbytes = 128
        keys = [_make_obj_key(i) for i in range(2)]
        t = torch.zeros(nbytes // 4, dtype=torch.float32)
        mem_objs = {k: _make_mem_obj(t.data_ptr(), nbytes // 2) for k in keys}
        mock_ctx.resolve_obj_keys.return_value = [keys]
        mock_ctx.storage_manager.reserve_write.side_effect = lambda ks, *a, **kw: {
            ks[0]: mem_objs[ks[0]]
        }

        from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper

        w = NixlWrapper(
            agent_name="w",
            agent_metadata=b"m",
            base_addr=0x2000,
            length=nbytes,
            device_id=0,
            mem_type="DRAM",
            shape=(nbytes // 4,),
            dtype=torch.float32,
            stride=(1,),
            storage_offset=0,
        )
        descriptor = DeviceIPCWrapper.Serialize(w)
        _, succeeded = module.store(_make_ipc_key(), 1, [], descriptor)

        assert succeeded is True
        assert mock_ctx.storage_manager.finish_write.call_count == 2


# ---------------------------------------------------------------------------
# TestRetrieve
# ---------------------------------------------------------------------------


class TestNixlTransferModuleRetrieve:
    """retrieve() correctness."""

    def _setup_prefetch_hit(
        self,
        mock_ctx: MagicMock,
        num_chunks: int,
        chunk_length: int,
    ) -> tuple[list[ObjectKey], list[MagicMock]]:
        keys = [_make_obj_key(i) for i in range(num_chunks)]
        mock_ctx.resolve_obj_keys.return_value = [keys]

        prefetch = MagicMock()
        prefetch.l1_found_indices = list(range(num_chunks))
        mock_ctx.storage_manager.submit_prefetch_task.return_value = prefetch

        mem_objs = [
            _make_mem_obj(0x3000 + i * chunk_length, chunk_length)
            for i in range(num_chunks)
        ]
        mock_ctx.storage_manager.read_prefetched_results.return_value = (
            _ctx_manager(mem_objs)
        )
        return keys, mem_objs

    def test_retrieve_single_chunk_succeeds(
        self, module, mock_ctx: MagicMock
    ) -> None:
        chunk_len = 64
        keys, _ = self._setup_prefetch_hit(mock_ctx, 1, chunk_len)

        descriptor = _make_nixl_wrapper_bytes(chunk_len)
        _, succeeded = module.retrieve(_make_ipc_key(), 1, [], descriptor)

        assert succeeded is True
        mock_ctx.storage_manager.finish_read_prefetched.assert_called_once_with(keys)

    def test_retrieve_cache_miss_returns_false(
        self, module, mock_ctx: MagicMock
    ) -> None:
        keys = [_make_obj_key(0)]
        mock_ctx.resolve_obj_keys.return_value = [keys]

        prefetch = MagicMock()
        prefetch.l1_found_indices = []  # miss
        mock_ctx.storage_manager.submit_prefetch_task.return_value = prefetch

        descriptor = _make_nixl_wrapper_bytes(64)
        _, succeeded = module.retrieve(_make_ipc_key(), 1, [], descriptor)

        assert succeeded is False
        mock_ctx.storage_manager.finish_read_prefetched.assert_not_called()

    def test_retrieve_zero_chunks_returns_false(
        self, module, mock_ctx: MagicMock
    ) -> None:
        mock_ctx.resolve_obj_keys.return_value = [[]]
        descriptor = _make_nixl_wrapper_bytes(64)
        _, succeeded = module.retrieve(_make_ipc_key(), 1, [], descriptor)
        assert succeeded is False

    def test_retrieve_wrong_wrapper_type_raises(
        self, module, mock_ctx: MagicMock
    ) -> None:
        from lmcache.v1.platform.rdma.rdma_transport import (
            StubRdmaTransport,
            set_rdma_transport,
        )
        from lmcache.v1.platform.rdma.rdma_wrapper import RdmaWrapper, _REGISTERED_MRS

        transport = StubRdmaTransport()
        set_rdma_transport(transport)
        try:
            t = torch.zeros(8, dtype=torch.float32)
            wrapper = RdmaWrapper.wrap(t)
            descriptor = DeviceIPCWrapper.Serialize(wrapper)
        finally:
            set_rdma_transport(None)  # type: ignore[arg-type]
            _REGISTERED_MRS.clear()

        with pytest.raises(ValueError, match="NixlWrapper"):
            module.retrieve(_make_ipc_key(), 1, [], descriptor)

    def test_retrieve_adds_remote_agent(
        self, module, mock_ctx: MagicMock
    ) -> None:
        chunk_len = 64
        self._setup_prefetch_hit(mock_ctx, 1, chunk_len)

        descriptor = _make_nixl_wrapper_bytes(chunk_len)
        module.retrieve(_make_ipc_key(), 1, [], descriptor)

        module._agent.add_remote_agent.assert_called_once_with(b"agent_meta")

    def test_retrieve_none_mem_objs_returns_false(
        self, module, mock_ctx: MagicMock
    ) -> None:
        keys = [_make_obj_key(0)]
        mock_ctx.resolve_obj_keys.return_value = [keys]

        prefetch = MagicMock()
        prefetch.l1_found_indices = [0]
        mock_ctx.storage_manager.submit_prefetch_task.return_value = prefetch
        mock_ctx.storage_manager.read_prefetched_results.return_value = (
            _ctx_manager(None)
        )

        descriptor = _make_nixl_wrapper_bytes(64)
        _, succeeded = module.retrieve(_make_ipc_key(), 1, [], descriptor)

        assert succeeded is False
        mock_ctx.storage_manager.finish_read_prefetched.assert_not_called()


# ---------------------------------------------------------------------------
# TestNixlTransferContext
# ---------------------------------------------------------------------------


class TestNixlTransferContext:
    """Worker-side NixlTransferContext submit_store / submit_retrieve."""

    @pytest.fixture()
    def ctx(self):
        from lmcache.v1.multiprocess.transfer_context.nixl_transfer import (
            NixlTransferContext,
        )

        return NixlTransferContext()

    @pytest.fixture()
    def mq_client(self):
        return MagicMock()

    @pytest.fixture()
    def send_request(self):
        future = MagicMock()
        future.result.return_value = True
        return MagicMock(return_value=future)

    @pytest.fixture()
    def registered_ctx(self, ctx, mq_client, send_request):
        ctx.register(
            instance_id=1,
            kv_caches={},
            model_name="m",
            world_size=1,
            blocks_in_chunk=1,
            mq_client=mq_client,
            mq_timeout=5.0,
            send_request=send_request,
        )
        return ctx

    def test_register_stores_client_and_sender(
        self, registered_ctx, mq_client, send_request
    ) -> None:
        assert registered_ctx._mq_client is mq_client
        assert registered_ctx._send_request is send_request

    def test_submit_store_before_register_raises(self, ctx) -> None:
        event = MagicMock()
        event.ipc_handle.return_value = b"data"
        with pytest.raises(RuntimeError, match="not registered"):
            ctx.submit_store("req", MagicMock(), 1, {}, [[]], event, 1)

    def test_submit_retrieve_before_register_raises(self, ctx) -> None:
        event = MagicMock()
        event.ipc_handle.return_value = b"data"
        with pytest.raises(RuntimeError, match="not registered"):
            ctx.submit_retrieve("req", MagicMock(), 1, {}, [[]], event, 1)

    def test_submit_store_sends_store_request(
        self, registered_ctx, mq_client, send_request
    ) -> None:
        event = MagicMock()
        event.ipc_handle.return_value = b"nixl_bytes"
        key = MagicMock()

        registered_ctx.submit_store("req-1", key, 42, {}, [[0]], event, 1)

        send_request.assert_called_once()
        args = send_request.call_args[0]
        assert args[1] == RequestType.STORE
        assert args[2][1] == 42      # instance_id
        assert args[2][2] == []      # block_ids empty
        assert args[2][3] == b"nixl_bytes"

    def test_submit_retrieve_sends_retrieve_request(
        self, registered_ctx, mq_client, send_request
    ) -> None:
        event = MagicMock()
        event.ipc_handle.return_value = b"nixl_bytes"
        key = MagicMock()

        registered_ctx.submit_retrieve("req-2", key, 7, {}, [[0]], event, 1)

        send_request.assert_called_once()
        args = send_request.call_args[0]
        assert args[1] == RequestType.RETRIEVE
        assert args[2][3] == b"nixl_bytes"

    def test_close_clears_state(self, registered_ctx) -> None:
        registered_ctx.close()
        assert registered_ctx._mq_client is None
        assert registered_ctx._send_request is None


# ---------------------------------------------------------------------------
# TestMPTransferModeNIXL
# ---------------------------------------------------------------------------


class TestMPTransferModeNIXL:
    """create_transfer_context routes NIXL mode correctly."""

    def test_nixl_mode_returns_nixl_context(self) -> None:
        from lmcache.v1.multiprocess.transfer_context.nixl_transfer import (
            NixlTransferContext,
        )
        from lmcache.v1.multiprocess.transfer_context.worker_transfer import (
            MPTransferMode,
            create_transfer_context,
        )

        t = torch.zeros(4, dtype=torch.float32)
        ctx = create_transfer_context({"layer0": t}, mode=MPTransferMode.NIXL)
        assert isinstance(ctx, NixlTransferContext)

    def test_nixl_enum_value_is_nixl_string(self) -> None:
        from lmcache.v1.multiprocess.transfer_context.worker_transfer import (
            MPTransferMode,
        )

        assert MPTransferMode.NIXL.value == "nixl"

    def test_nixl_mode_via_string(self) -> None:
        from lmcache.v1.multiprocess.transfer_context.nixl_transfer import (
            NixlTransferContext,
        )
        from lmcache.v1.multiprocess.transfer_context.worker_transfer import (
            create_transfer_context,
        )

        t = torch.zeros(4, dtype=torch.float32)
        ctx = create_transfer_context({"layer0": t}, mode="nixl")
        assert isinstance(ctx, NixlTransferContext)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


from contextlib import contextmanager


@contextmanager
def _ctx_manager(value):
    """Minimal context manager returning ``value`` from __enter__."""
    yield value
