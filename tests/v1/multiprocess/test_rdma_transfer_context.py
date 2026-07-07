# SPDX-License-Identifier: Apache-2.0
"""Tests for RdmaTransferContext (worker-side RDMA transfer context)."""

from unittest.mock import MagicMock

import pytest

from lmcache.v1.multiprocess.futures import MessagingFuture
from lmcache.v1.multiprocess.protocols.base import RequestType
from lmcache.v1.multiprocess.transfer_context.worker_transfer import (
    MPTransferMode,
    RdmaTransferContext,
)


class _FakeEvent:
    """Minimal IPCEvent stand-in returning a fixed descriptor payload."""

    def __init__(self, handle: bytes) -> None:
        self._handle = handle

    def ipc_handle(self) -> bytes:
        return self._handle


@pytest.fixture
def context() -> RdmaTransferContext:
    ctx = RdmaTransferContext()
    ctx.register(
        instance_id=1,
        kv_caches={},
        model_name="test-model",
        world_size=1,
        blocks_in_chunk=1,
        mq_client=MagicMock(),
        mq_timeout=5.0,
        send_request=MagicMock(),
    )
    return ctx


class TestSubmitStore:
    def test_returns_send_request_future_directly(
        self, context: RdmaTransferContext
    ) -> None:
        """submit_store must not pre-resolve; it returns the MQ future so the
        caller blocks until the server confirms the RDMA Read completed."""
        pending: MessagingFuture[bool] = MessagingFuture()
        context._send_request.return_value = pending  # type: ignore[union-attr]

        result = context.submit_store(
            "req-0",
            key="key",
            instance_id=1,
            _kv_caches={},
            _block_ids=[],
            event=_FakeEvent(b"descriptor-bytes"),
            _blocks_in_chunk=1,
        )

        assert result is pending
        assert not result.query()

    def test_sends_store_request_with_descriptor_and_empty_block_ids(
        self, context: RdmaTransferContext
    ) -> None:
        context._send_request.return_value = MessagingFuture()  # type: ignore[union-attr]

        context.submit_store(
            "req-0",
            key="my-key",
            instance_id=42,
            _kv_caches={},
            _block_ids=[[1, 2, 3]],
            event=_FakeEvent(b"src-descriptor"),
            _blocks_in_chunk=1,
        )

        context._send_request.assert_called_once_with(  # type: ignore[union-attr]
            context._mq_client,
            RequestType.STORE,
            ["my-key", 42, [], b"src-descriptor"],
        )

    def test_raises_if_not_registered(self) -> None:
        ctx = RdmaTransferContext()
        with pytest.raises(RuntimeError, match="not registered"):
            ctx.submit_store(
                "req-0",
                key="key",
                instance_id=1,
                _kv_caches={},
                _block_ids=[],
                event=_FakeEvent(b"x"),
                _blocks_in_chunk=1,
            )


class TestSubmitRetrieve:
    def test_returns_send_request_future_directly(
        self, context: RdmaTransferContext
    ) -> None:
        pending: MessagingFuture[bool] = MessagingFuture()
        context._send_request.return_value = pending  # type: ignore[union-attr]

        result = context.submit_retrieve(
            "req-0",
            key="key",
            instance_id=1,
            _kv_caches={},
            _block_ids=[],
            event=_FakeEvent(b"dst-descriptor"),
            _blocks_in_chunk=1,
        )

        assert result is pending

    def test_sends_retrieve_request_with_descriptor_and_skip_tokens(
        self, context: RdmaTransferContext
    ) -> None:
        context._send_request.return_value = MessagingFuture()  # type: ignore[union-attr]

        context.submit_retrieve(
            "req-0",
            key="my-key",
            instance_id=7,
            _kv_caches={},
            _block_ids=[[1]],
            event=_FakeEvent(b"dst-descriptor"),
            _blocks_in_chunk=1,
            skip_first_n_tokens=16,
        )

        context._send_request.assert_called_once_with(  # type: ignore[union-attr]
            context._mq_client,
            RequestType.RETRIEVE,
            ["my-key", 7, [], b"dst-descriptor", 16],
        )

    def test_raises_if_not_registered(self) -> None:
        ctx = RdmaTransferContext()
        with pytest.raises(RuntimeError, match="not registered"):
            ctx.submit_retrieve(
                "req-0",
                key="key",
                instance_id=1,
                _kv_caches={},
                _block_ids=[],
                event=_FakeEvent(b"x"),
                _blocks_in_chunk=1,
            )


class TestRegister:
    def test_register_does_not_send_register_kv_cache(self) -> None:
        """No REGISTER_KV_CACHE round-trip; the RDMA QP is set up at
        transport init time, not at worker registration time."""
        ctx = RdmaTransferContext()
        send_request = MagicMock()
        ctx.register(
            instance_id=1,
            kv_caches={},
            model_name="test-model",
            world_size=1,
            blocks_in_chunk=1,
            mq_client=MagicMock(),
            mq_timeout=5.0,
            send_request=send_request,
        )
        send_request.assert_not_called()


class TestClose:
    def test_close_clears_mq_state(self, context: RdmaTransferContext) -> None:
        context.close()
        with pytest.raises(RuntimeError, match="not registered"):
            context.submit_store(
                "req-0",
                key="key",
                instance_id=1,
                _kv_caches={},
                _block_ids=[],
                event=_FakeEvent(b"x"),
                _blocks_in_chunk=1,
            )


def test_mp_transfer_mode_rdma_value() -> None:
    assert MPTransferMode.RDMA.value == "rdma"
