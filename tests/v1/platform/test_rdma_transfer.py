# SPDX-License-Identifier: Apache-2.0
"""Unit tests for RdmaTransferModule (STORE and RETRIEVE handlers).

Uses StubRdmaTransport (memcpy, same process) and mocked
MPCacheServerContext — no RDMA hardware required.
"""

from __future__ import annotations

import pickle
from contextlib import contextmanager
from typing import Iterator
from unittest.mock import MagicMock, call, patch

import pytest
import torch

from lmcache.v1.distributed.api import MemoryLayoutDesc, ObjectKey, PrefetchHandle
from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey
from lmcache.v1.multiprocess.modules.rdma_transfer import RdmaTransferModule
from lmcache.v1.multiprocess.protocols.base import RequestType
from lmcache.v1.platform.base.ipc_wrapper import DeviceIPCWrapper
from lmcache.v1.platform.rdma.rdma_transport import (
    MrInfo,
    RegisteredBuffer,
    StubRdmaTransport,
    set_rdma_transport,
)
from lmcache.v1.platform.rdma.rdma_wrapper import RdmaWrapper, _REGISTERED_MRS


# ---------------------------------------------------------------------------
# Module-level fake wrapper (pickle requires top-level class)
# ---------------------------------------------------------------------------


class _FakeWrapper(DeviceIPCWrapper):
    """Non-RdmaWrapper subclass used to test descriptor validation."""

    def __init__(self) -> None:
        self.handle = None
        self.dtype = torch.float32
        self.shape = (1,)
        self.stride = (1,)
        self.storage_offset = 0
        self.device_uuid = "cpu"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ipc_key(token_ids: list[int] | None = None) -> IPCCacheServerKey:
    """Return a minimal IPCCacheServerKey for use in tests."""
    return IPCCacheServerKey(
        model_name="test-model",
        world_size=1,
        worker_id=0,
        token_ids=tuple(token_ids or [1, 2, 3]),
        start=0,
        end=3,
        request_id="req-0",
    )


def _make_obj_key(idx: int = 0) -> ObjectKey:
    """Return a deterministic ObjectKey."""
    return ObjectKey(
        chunk_hash=idx.to_bytes(4, "big"),
        model_name="test-model",
        kv_rank=0,
    )


def _make_mem_obj(tensor: torch.Tensor) -> MagicMock:
    """Return a MemoryObj mock whose data_ptr matches ``tensor``."""
    mem_obj = MagicMock()
    mem_obj.data_ptr = tensor.data_ptr()
    mem_obj.get_size.return_value = tensor.numel() * tensor.element_size()
    return mem_obj


def _make_wrapper(tensor: torch.Tensor) -> RdmaWrapper:
    """Return a real RdmaWrapper for ``tensor`` (uses the global transport)."""
    return RdmaWrapper.wrap(tensor)


def _serialized_wrapper(tensor: torch.Tensor) -> bytes:
    """Serialize an RdmaWrapper for ``tensor`` into wire bytes."""
    return DeviceIPCWrapper.Serialize(_make_wrapper(tensor))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_transport():
    """Install a fresh StubRdmaTransport and clear global MR state."""
    transport = StubRdmaTransport()
    set_rdma_transport(transport)
    yield transport
    set_rdma_transport(None)  # type: ignore[arg-type]
    _REGISTERED_MRS.clear()


@pytest.fixture()
def transport(_isolate_transport: StubRdmaTransport) -> StubRdmaTransport:
    """Expose the current stub transport to individual tests."""
    return _isolate_transport


@pytest.fixture()
def mock_ctx() -> MagicMock:
    """Minimal MPCacheServerContext mock."""
    ctx = MagicMock()
    # Ensure storage_manager is a plain MagicMock (no spec), so attribute
    # access never raises.
    ctx.storage_manager = MagicMock()
    return ctx


@pytest.fixture()
def module(mock_ctx: MagicMock) -> RdmaTransferModule:
    """RdmaTransferModule wired to mock_ctx and StubRdmaTransport."""
    return RdmaTransferModule(mock_ctx)


# ---------------------------------------------------------------------------
# TestRdmaTransferModuleHandlers
# ---------------------------------------------------------------------------


class TestRdmaTransferModuleHandlers:
    """get_handlers() contract."""

    def test_get_handlers_returns_store_and_retrieve(
        self, module: RdmaTransferModule
    ) -> None:
        specs = module.get_handlers()
        request_types = {s.request_type for s in specs}
        assert RequestType.STORE in request_types
        assert RequestType.RETRIEVE in request_types

    def test_no_register_kv_cache_handler(
        self, module: RdmaTransferModule
    ) -> None:
        specs = module.get_handlers()
        request_types = {s.request_type for s in specs}
        assert RequestType.REGISTER_KV_CACHE not in request_types

    def test_get_handlers_returns_exactly_two(
        self, module: RdmaTransferModule
    ) -> None:
        """Only STORE and RETRIEVE — no extra handler registered."""
        assert len(module.get_handlers()) == 2


# ---------------------------------------------------------------------------
# TestRdmaTransferModuleStore
# ---------------------------------------------------------------------------


class TestRdmaTransferModuleStore:
    """store() correctness against StubRdmaTransport."""

    # ------------------------------------------------------------------
    # helpers to build a single-chunk store scenario
    # ------------------------------------------------------------------

    @staticmethod
    def _setup_single_chunk(
        mock_ctx: MagicMock,
        src_tensor: torch.Tensor,
    ) -> tuple[ObjectKey, MagicMock]:
        """Wire ctx so that resolve_obj_keys returns one chunk."""
        obj_key = _make_obj_key(0)
        mem_obj = _make_mem_obj(src_tensor)
        mock_ctx.resolve_obj_keys.return_value = [[obj_key]]
        mock_ctx.storage_manager.reserve_write.return_value = {obj_key: mem_obj}
        return obj_key, mem_obj

    def test_store_posts_rdma_read_into_memory_obj_backing(
        self,
        module: RdmaTransferModule,
        mock_ctx: MagicMock,
        transport: StubRdmaTransport,
    ) -> None:
        """post_read() is called with the MemoryObj's data_ptr and wrapper
        descriptor fields; finish_write([obj_key]) is called on success."""
        nbytes = 64
        src = torch.ones(nbytes // 4, dtype=torch.float32)
        dst = torch.zeros(nbytes // 4, dtype=torch.float32)

        obj_key, _ = self._setup_single_chunk(mock_ctx, dst)

        # Spy on post_read so we can capture the call arguments.
        real_post_read = transport.post_read
        post_read_calls: list[dict] = []

        def spy_post_read(
            local_buf: RegisteredBuffer,
            remote_addr: int,
            rkey: int,
            length: int,
        ):
            post_read_calls.append(
                {
                    "local_buf": local_buf,
                    "remote_addr": remote_addr,
                    "rkey": rkey,
                    "length": length,
                }
            )
            return real_post_read(
                local_buf=local_buf,
                remote_addr=remote_addr,
                rkey=rkey,
                length=length,
            )

        transport.post_read = spy_post_read  # type: ignore[method-assign]

        wrapper = _make_wrapper(src)
        descriptor = DeviceIPCWrapper.Serialize(wrapper)

        result = module.store(
            key=_make_ipc_key(),
            instance_id=1,
            block_ids=[],
            rdma_descriptor_bytes=descriptor,
        )

        assert result == (b"", True)
        assert len(post_read_calls) == 1

        call_kwargs = post_read_calls[0]
        # local_buf.addr must point into dst's backing memory
        assert call_kwargs["local_buf"].addr == dst.data_ptr()
        # remote_addr must be the source wrapper's address (no offset for chunk 0)
        assert call_kwargs["remote_addr"] == wrapper.remote_addr
        assert call_kwargs["rkey"] == wrapper.rkey
        assert call_kwargs["length"] == wrapper.length

        # finish_write must commit [obj_key]
        mock_ctx.storage_manager.finish_write.assert_called_once_with([obj_key])

    def test_store_finish_write_called_on_rdma_timeout(
        self,
        module: RdmaTransferModule,
        mock_ctx: MagicMock,
        transport: StubRdmaTransport,
    ) -> None:
        """Write lock is released via finish_write([]) even when RDMA times out."""
        obj_key = _make_obj_key(0)
        dst = torch.zeros(16, dtype=torch.float32)
        mem_obj = _make_mem_obj(dst)
        mock_ctx.resolve_obj_keys.return_value = [[obj_key]]
        mock_ctx.storage_manager.reserve_write.return_value = {obj_key: mem_obj}

        # Make poll_completion return False (simulated timeout)
        transport.poll_completion = lambda future, timeout_ms=5000: False  # type: ignore[method-assign]

        src = torch.ones(16, dtype=torch.float32)
        _, result_flag = module.store(
            key=_make_ipc_key(),
            instance_id=1,
            block_ids=[],
            rdma_descriptor_bytes=_serialized_wrapper(src),
        )

        assert result_flag is False
        # finish_write must still be called with empty list (abort, not commit)
        mock_ctx.storage_manager.finish_write.assert_called_once_with([])

    def test_store_finish_write_called_on_exception(
        self,
        module: RdmaTransferModule,
        mock_ctx: MagicMock,
        transport: StubRdmaTransport,
    ) -> None:
        """Write lock is released via finish_write([]) even when post_read raises."""
        obj_key = _make_obj_key(0)
        dst = torch.zeros(8, dtype=torch.float32)
        mem_obj = _make_mem_obj(dst)
        mock_ctx.resolve_obj_keys.return_value = [[obj_key]]
        mock_ctx.storage_manager.reserve_write.return_value = {obj_key: mem_obj}

        transport.post_read = MagicMock(side_effect=RuntimeError("RDMA post failed"))

        src = torch.ones(8, dtype=torch.float32)
        _, result_flag = module.store(
            key=_make_ipc_key(),
            instance_id=1,
            block_ids=[],
            rdma_descriptor_bytes=_serialized_wrapper(src),
        )

        assert result_flag is False
        mock_ctx.storage_manager.finish_write.assert_called_once_with([])

    def test_store_multi_chunk_uses_correct_offsets(
        self,
        module: RdmaTransferModule,
        mock_ctx: MagicMock,
        transport: StubRdmaTransport,
    ) -> None:
        """Second chunk's remote_addr == wrapper.remote_addr + chunk_bytes."""
        # Two chunks, each 32 bytes (8 × float32)
        numel_per_chunk = 8
        chunk_bytes = numel_per_chunk * 4  # float32 = 4 bytes each

        # Source: contiguous tensor covering both chunks
        src = torch.ones(numel_per_chunk * 2, dtype=torch.float32)

        obj_key0 = _make_obj_key(0)
        obj_key1 = _make_obj_key(1)

        dst0 = torch.zeros(numel_per_chunk, dtype=torch.float32)
        dst1 = torch.zeros(numel_per_chunk, dtype=torch.float32)

        mem_obj0 = _make_mem_obj(dst0)
        mem_obj1 = _make_mem_obj(dst1)

        mock_ctx.resolve_obj_keys.return_value = [[obj_key0, obj_key1]]
        mock_ctx.storage_manager.reserve_write.side_effect = [
            {obj_key0: mem_obj0},
            {obj_key1: mem_obj1},
        ]

        # Spy on post_read
        real_post_read = transport.post_read
        captured: list[dict] = []

        def spy(local_buf, remote_addr, rkey, length):
            captured.append({"remote_addr": remote_addr, "length": length})
            return real_post_read(
                local_buf=local_buf,
                remote_addr=remote_addr,
                rkey=rkey,
                length=length,
            )

        transport.post_read = spy  # type: ignore[method-assign]

        wrapper = _make_wrapper(src)
        descriptor = DeviceIPCWrapper.Serialize(wrapper)

        result = module.store(
            key=_make_ipc_key(),
            instance_id=1,
            block_ids=[],
            rdma_descriptor_bytes=descriptor,
        )

        assert result == (b"", True)
        assert len(captured) == 2
        assert captured[0]["remote_addr"] == wrapper.remote_addr
        assert captured[1]["remote_addr"] == wrapper.remote_addr + chunk_bytes
        assert captured[0]["length"] == chunk_bytes
        assert captured[1]["length"] == chunk_bytes

    def test_store_multi_chunk_reserve_write_uses_per_chunk_shape(
        self,
        module: RdmaTransferModule,
        mock_ctx: MagicMock,
        transport: StubRdmaTransport,
    ) -> None:
        """reserve_write's layout_desc must describe ONE chunk, not the
        full multi-chunk range wrapper.shape carries.

        Regression test for LMCache-9ew: layout_desc was built from the
        wrapper's full shape and reused unchanged for every chunk, so each
        reserve_write() call allocated a MemoryObj sized for the entire
        range instead of 1/num_chunks of it.
        """
        numel_per_chunk = 8
        num_chunks = 3
        src = torch.ones(numel_per_chunk * num_chunks, dtype=torch.float32)

        obj_keys = [_make_obj_key(i) for i in range(num_chunks)]
        dsts = [
            torch.zeros(numel_per_chunk, dtype=torch.float32) for _ in range(num_chunks)
        ]
        mem_objs = [_make_mem_obj(d) for d in dsts]

        mock_ctx.resolve_obj_keys.return_value = [obj_keys]
        mock_ctx.storage_manager.reserve_write.side_effect = [
            {k: mo} for k, mo in zip(obj_keys, mem_objs, strict=True)
        ]

        wrapper = _make_wrapper(src)
        descriptor = DeviceIPCWrapper.Serialize(wrapper)

        module.store(
            key=_make_ipc_key(),
            instance_id=1,
            block_ids=[],
            rdma_descriptor_bytes=descriptor,
        )

        assert mock_ctx.storage_manager.reserve_write.call_count == num_chunks
        for call_args in mock_ctx.storage_manager.reserve_write.call_args_list:
            _, layout_desc, _mode = call_args.args
            assert layout_desc.shapes == [torch.Size((numel_per_chunk,))]
            assert layout_desc.dtypes == [torch.float32]

    def test_store_validates_descriptor_is_rdma_wrapper(
        self,
        module: RdmaTransferModule,
        mock_ctx: MagicMock,
    ) -> None:
        """Non-RdmaWrapper descriptors must raise ValueError."""
        bad_bytes = DeviceIPCWrapper.Serialize(_FakeWrapper())
        with pytest.raises(ValueError, match="expected RdmaWrapper"):
            module.store(
                key=_make_ipc_key(),
                instance_id=1,
                block_ids=[],
                rdma_descriptor_bytes=bad_bytes,
            )

    def test_store_returns_false_on_poll_timeout(
        self,
        module: RdmaTransferModule,
        mock_ctx: MagicMock,
        transport: StubRdmaTransport,
    ) -> None:
        """store() returns (b'', False) when poll_completion times out."""
        obj_key = _make_obj_key(0)
        dst = torch.zeros(16, dtype=torch.float32)
        mock_ctx.resolve_obj_keys.return_value = [[obj_key]]
        mock_ctx.storage_manager.reserve_write.return_value = {
            obj_key: _make_mem_obj(dst)
        }

        transport.poll_completion = lambda future, timeout_ms=5000: False  # type: ignore[method-assign]

        src = torch.ones(16, dtype=torch.float32)
        out_bytes, ok = module.store(
            key=_make_ipc_key(),
            instance_id=1,
            block_ids=[],
            rdma_descriptor_bytes=_serialized_wrapper(src),
        )

        assert out_bytes == b""
        assert ok is False

    def test_store_zero_chunks_returns_success(
        self,
        module: RdmaTransferModule,
        mock_ctx: MagicMock,
    ) -> None:
        """When resolve_obj_keys returns empty list, store returns (b'', True)."""
        mock_ctx.resolve_obj_keys.return_value = [[]]
        src = torch.ones(16, dtype=torch.float32)
        out_bytes, ok = module.store(
            key=_make_ipc_key(),
            instance_id=1,
            block_ids=[],
            rdma_descriptor_bytes=_serialized_wrapper(src),
        )
        assert out_bytes == b""
        assert ok is True

    def test_store_data_actually_transferred(
        self,
        module: RdmaTransferModule,
        mock_ctx: MagicMock,
    ) -> None:
        """StubRdmaTransport post_read copies bytes from src into dst."""
        numel = 16
        src = torch.arange(numel, dtype=torch.float32)
        dst = torch.zeros(numel, dtype=torch.float32)

        obj_key = _make_obj_key(0)
        mock_ctx.resolve_obj_keys.return_value = [[obj_key]]
        mock_ctx.storage_manager.reserve_write.return_value = {
            obj_key: _make_mem_obj(dst)
        }

        wrapper = _make_wrapper(src)
        descriptor = DeviceIPCWrapper.Serialize(wrapper)

        out_bytes, ok = module.store(
            key=_make_ipc_key(),
            instance_id=1,
            block_ids=[],
            rdma_descriptor_bytes=descriptor,
        )

        assert ok is True
        assert torch.equal(dst, src)


# ---------------------------------------------------------------------------
# TestRdmaTransferModuleRetrieve
# ---------------------------------------------------------------------------


def _build_read_prefetched_cm(
    mem_objs: list[MagicMock] | None,
) -> MagicMock:
    """Return a mock context-manager that yields ``mem_objs`` (or None)."""
    cm = MagicMock()

    @contextmanager
    def _ctx_manager(keys):
        yield mem_objs

    cm.side_effect = _ctx_manager
    return cm


def _mock_prefetch_hit(num_chunks: int) -> PrefetchHandle:
    """Return a PrefetchHandle indicating all chunks are L1 hits."""
    return PrefetchHandle(
        prefetch_request_id=-1,
        external_request_id="",
        l1_found_indices=tuple(range(num_chunks)),
        total_requested_keys=num_chunks,
        submit_time=0.0,
    )


def _mock_prefetch_miss() -> PrefetchHandle:
    """Return a PrefetchHandle indicating no L1 hits."""
    return PrefetchHandle(
        prefetch_request_id=-1,
        external_request_id="",
        l1_found_indices=(),
        total_requested_keys=0,
        submit_time=0.0,
    )


class TestRdmaTransferModuleRetrieve:
    """retrieve() correctness against StubRdmaTransport."""

    def test_retrieve_posts_rdma_write_from_memory_obj_backing(
        self,
        module: RdmaTransferModule,
        mock_ctx: MagicMock,
        transport: StubRdmaTransport,
    ) -> None:
        """post_write() is called with the MemoryObj's data_ptr and wrapper
        descriptor fields (remote destination on initiator)."""
        numel = 16
        src = torch.arange(numel, dtype=torch.float32)   # stored locally
        dst = torch.zeros(numel, dtype=torch.float32)     # initiator buffer

        obj_key = _make_obj_key(0)
        mem_obj = _make_mem_obj(src)  # storage-manager side

        mock_ctx.resolve_obj_keys.return_value = [[obj_key]]
        mock_ctx.storage_manager.submit_prefetch_task.return_value = _mock_prefetch_hit(1)
        mock_ctx.storage_manager.read_prefetched_results = _build_read_prefetched_cm(
            [mem_obj]
        )

        # The wrapper describes the INITIATOR's destination buffer (dst)
        wrapper = _make_wrapper(dst)
        descriptor = DeviceIPCWrapper.Serialize(wrapper)

        real_post_write = transport.post_write
        captured: list[dict] = []

        def spy(local_buf, remote_addr, rkey, length):
            captured.append(
                {
                    "local_buf_addr": local_buf.addr,
                    "remote_addr": remote_addr,
                    "rkey": rkey,
                    "length": length,
                }
            )
            return real_post_write(
                local_buf=local_buf,
                remote_addr=remote_addr,
                rkey=rkey,
                length=length,
            )

        transport.post_write = spy  # type: ignore[method-assign]

        out_bytes, ok = module.retrieve(
            key=_make_ipc_key(),
            instance_id=1,
            block_ids=[],
            rdma_descriptor_bytes=descriptor,
        )

        assert ok is True
        assert out_bytes == b""
        assert len(captured) == 1
        assert captured[0]["local_buf_addr"] == src.data_ptr()
        assert captured[0]["remote_addr"] == wrapper.remote_addr
        assert captured[0]["rkey"] == wrapper.rkey

    def test_retrieve_finish_read_prefetched_after_rdma_completion(
        self,
        module: RdmaTransferModule,
        mock_ctx: MagicMock,
        transport: StubRdmaTransport,
    ) -> None:
        """finish_read_prefetched is called only AFTER poll_completion succeeds."""
        obj_key = _make_obj_key(0)
        src = torch.ones(8, dtype=torch.float32)
        dst = torch.zeros(8, dtype=torch.float32)

        mem_obj = _make_mem_obj(src)
        mock_ctx.resolve_obj_keys.return_value = [[obj_key]]
        mock_ctx.storage_manager.submit_prefetch_task.return_value = _mock_prefetch_hit(1)
        mock_ctx.storage_manager.read_prefetched_results = _build_read_prefetched_cm(
            [mem_obj]
        )

        poll_completed: list[bool] = []
        real_poll = transport.poll_completion

        def recording_poll(future, timeout_ms=5000):
            result = real_poll(future, timeout_ms=timeout_ms)
            poll_completed.append(result)
            return result

        transport.poll_completion = recording_poll  # type: ignore[method-assign]

        wrapper = _make_wrapper(dst)
        descriptor = DeviceIPCWrapper.Serialize(wrapper)

        _, ok = module.retrieve(
            key=_make_ipc_key(),
            instance_id=1,
            block_ids=[],
            rdma_descriptor_bytes=descriptor,
        )

        assert ok is True
        # poll must have run before finish_read_prefetched is called
        assert len(poll_completed) == 1
        assert poll_completed[0] is True
        mock_ctx.storage_manager.finish_read_prefetched.assert_called_once_with(
            [obj_key]
        )

    def test_retrieve_not_finished_before_rdma_completion(
        self,
        module: RdmaTransferModule,
        mock_ctx: MagicMock,
        transport: StubRdmaTransport,
    ) -> None:
        """finish_read_prefetched must NOT be called when poll_completion fails."""
        obj_key = _make_obj_key(0)
        src = torch.ones(8, dtype=torch.float32)
        dst = torch.zeros(8, dtype=torch.float32)

        mem_obj = _make_mem_obj(src)
        mock_ctx.resolve_obj_keys.return_value = [[obj_key]]
        mock_ctx.storage_manager.submit_prefetch_task.return_value = _mock_prefetch_hit(1)
        mock_ctx.storage_manager.read_prefetched_results = _build_read_prefetched_cm(
            [mem_obj]
        )

        transport.poll_completion = lambda future, timeout_ms=5000: False  # type: ignore[method-assign]

        wrapper = _make_wrapper(dst)
        descriptor = DeviceIPCWrapper.Serialize(wrapper)

        _, ok = module.retrieve(
            key=_make_ipc_key(),
            instance_id=1,
            block_ids=[],
            rdma_descriptor_bytes=descriptor,
        )

        assert ok is False
        mock_ctx.storage_manager.finish_read_prefetched.assert_not_called()

    def test_retrieve_returns_false_on_missing_keys(
        self,
        module: RdmaTransferModule,
        mock_ctx: MagicMock,
    ) -> None:
        """Cache miss (submit_prefetch_task finds no L1 hits) -> (b'', False)."""
        obj_key = _make_obj_key(0)
        mock_ctx.resolve_obj_keys.return_value = [[obj_key]]
        mock_ctx.storage_manager.submit_prefetch_task.return_value = _mock_prefetch_miss()

        dst = torch.zeros(16, dtype=torch.float32)
        wrapper = _make_wrapper(dst)
        descriptor = DeviceIPCWrapper.Serialize(wrapper)

        out_bytes, ok = module.retrieve(
            key=_make_ipc_key(),
            instance_id=1,
            block_ids=[],
            rdma_descriptor_bytes=descriptor,
        )

        assert out_bytes == b""
        assert ok is False
        mock_ctx.storage_manager.finish_read_prefetched.assert_not_called()

    def test_retrieve_multi_chunk_offsets(
        self,
        module: RdmaTransferModule,
        mock_ctx: MagicMock,
        transport: StubRdmaTransport,
    ) -> None:
        """Second chunk's remote_addr == wrapper.remote_addr + chunk_bytes."""
        numel_per_chunk = 8
        chunk_bytes = numel_per_chunk * 4  # float32

        # Local storage: two separate source tensors
        src0 = torch.ones(numel_per_chunk, dtype=torch.float32)
        src1 = torch.full((numel_per_chunk,), 2.0, dtype=torch.float32)

        # Initiator's contiguous destination buffer (both chunks)
        dst = torch.zeros(numel_per_chunk * 2, dtype=torch.float32)

        obj_key0 = _make_obj_key(0)
        obj_key1 = _make_obj_key(1)

        mem_obj0 = _make_mem_obj(src0)
        mem_obj1 = _make_mem_obj(src1)

        mock_ctx.resolve_obj_keys.return_value = [[obj_key0, obj_key1]]
        mock_ctx.storage_manager.submit_prefetch_task.return_value = _mock_prefetch_hit(2)
        mock_ctx.storage_manager.read_prefetched_results = _build_read_prefetched_cm(
            [mem_obj0, mem_obj1]
        )

        wrapper = _make_wrapper(dst)
        descriptor = DeviceIPCWrapper.Serialize(wrapper)

        real_post_write = transport.post_write
        captured: list[dict] = []

        def spy(local_buf, remote_addr, rkey, length):
            captured.append({"remote_addr": remote_addr, "length": length})
            return real_post_write(
                local_buf=local_buf,
                remote_addr=remote_addr,
                rkey=rkey,
                length=length,
            )

        transport.post_write = spy  # type: ignore[method-assign]

        out_bytes, ok = module.retrieve(
            key=_make_ipc_key(),
            instance_id=1,
            block_ids=[],
            rdma_descriptor_bytes=descriptor,
        )

        assert ok is True
        assert len(captured) == 2
        assert captured[0]["remote_addr"] == wrapper.remote_addr
        assert captured[1]["remote_addr"] == wrapper.remote_addr + chunk_bytes
        assert captured[0]["length"] == chunk_bytes
        assert captured[1]["length"] == chunk_bytes

    def test_retrieve_data_actually_transferred(
        self,
        module: RdmaTransferModule,
        mock_ctx: MagicMock,
    ) -> None:
        """StubRdmaTransport post_write copies bytes from src into dst."""
        numel = 16
        src = torch.arange(numel, dtype=torch.float32)
        dst = torch.zeros(numel, dtype=torch.float32)

        obj_key = _make_obj_key(0)
        mem_obj = _make_mem_obj(src)
        mock_ctx.resolve_obj_keys.return_value = [[obj_key]]
        mock_ctx.storage_manager.submit_prefetch_task.return_value = _mock_prefetch_hit(1)
        mock_ctx.storage_manager.read_prefetched_results = _build_read_prefetched_cm(
            [mem_obj]
        )

        wrapper = _make_wrapper(dst)
        descriptor = DeviceIPCWrapper.Serialize(wrapper)

        _, ok = module.retrieve(
            key=_make_ipc_key(),
            instance_id=1,
            block_ids=[],
            rdma_descriptor_bytes=descriptor,
        )

        assert ok is True
        assert torch.equal(dst, src)

    def test_retrieve_zero_chunks_returns_failure(
        self,
        module: RdmaTransferModule,
        mock_ctx: MagicMock,
    ) -> None:
        """resolve_obj_keys returns empty -> retrieve returns (b'', False)."""
        mock_ctx.resolve_obj_keys.return_value = [[]]
        dst = torch.zeros(16, dtype=torch.float32)
        wrapper = _make_wrapper(dst)
        out_bytes, ok = module.retrieve(
            key=_make_ipc_key(),
            instance_id=1,
            block_ids=[],
            rdma_descriptor_bytes=DeviceIPCWrapper.Serialize(wrapper),
        )
        assert out_bytes == b""
        assert ok is False

    def test_retrieve_validates_descriptor_is_rdma_wrapper(
        self,
        module: RdmaTransferModule,
        mock_ctx: MagicMock,
    ) -> None:
        """Non-RdmaWrapper descriptors must raise ValueError."""
        bad_bytes = DeviceIPCWrapper.Serialize(_FakeWrapper())
        with pytest.raises(ValueError, match="expected RdmaWrapper"):
            module.retrieve(
                key=_make_ipc_key(),
                instance_id=1,
                block_ids=[],
                rdma_descriptor_bytes=bad_bytes,
            )
