# SPDX-License-Identifier: Apache-2.0
"""Unit tests for RdmaThinClient."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import torch

from lmcache.v1.multiprocess.custom_types import IPCCacheServerKey
from lmcache.v1.multiprocess.futures import MessagingFuture
from lmcache.v1.multiprocess.protocols.base import RequestType
from lmcache.v1.platform.rdma.thin_client import RdmaThinClient


@pytest.fixture
def mock_mq_client() -> MagicMock:
    return MagicMock()


@pytest.fixture
def client(mock_mq_client: MagicMock) -> RdmaThinClient:
    with patch(
        "lmcache.v1.platform.rdma.thin_client.MessageQueueClient",
        return_value=mock_mq_client,
    ):
        c = RdmaThinClient(
            server_url="tcp://localhost:5601",
            model_name="test-model",
            timeout=5.0,
        )
    return c


class TestStore:
    def test_store_sends_request_and_returns_true(
        self, client: RdmaThinClient, mock_mq_client: MagicMock
    ) -> None:
        future: MessagingFuture = MessagingFuture()
        future.set_result((b"", True))
        mock_mq_client.submit_request.return_value = future

        data = torch.arange(4, dtype=torch.float32)
        ok = client.store("req-0", token_ids=[1, 2, 3, 4], data=data)

        assert ok is True
        mock_mq_client.submit_request.assert_called_once()
        call_args = mock_mq_client.submit_request.call_args
        assert call_args[0][0] == RequestType.STORE

    def test_store_returns_false_on_server_failure(
        self, client: RdmaThinClient, mock_mq_client: MagicMock
    ) -> None:
        future: MessagingFuture = MessagingFuture()
        future.set_result((b"", False))
        mock_mq_client.submit_request.return_value = future

        data = torch.zeros(4, dtype=torch.float32)
        ok = client.store("req-1", token_ids=[1, 2, 3, 4], data=data)

        assert ok is False

    def test_store_rejects_non_contiguous_tensor(
        self, client: RdmaThinClient
    ) -> None:
        data = torch.arange(8, dtype=torch.float32)[::2]
        with pytest.raises(ValueError, match="contiguous"):
            client.store("req-2", token_ids=[1, 2, 3, 4], data=data)

    def test_store_rejects_non_cpu_tensor(
        self, client: RdmaThinClient
    ) -> None:
        if not torch.cuda.is_available():
            pytest.skip("No CUDA device available")
        data = torch.zeros(4, dtype=torch.float32, device="cuda")
        with pytest.raises(ValueError, match="CPU"):
            client.store("req-3", token_ids=[1, 2, 3, 4], data=data)


class TestRetrieve:
    def test_retrieve_returns_tensor_on_hit(
        self, client: RdmaThinClient, mock_mq_client: MagicMock
    ) -> None:
        future: MessagingFuture = MessagingFuture()
        future.set_result((b"", True))
        mock_mq_client.submit_request.return_value = future

        result = client.retrieve(
            "req-0", token_ids=[1, 2, 3, 4], numel=4, dtype=torch.float32
        )

        assert result is not None
        assert result.shape == (4,)
        assert result.dtype == torch.float32
        mock_mq_client.submit_request.assert_called_once()
        call_args = mock_mq_client.submit_request.call_args
        assert call_args[0][0] == RequestType.RETRIEVE

    def test_retrieve_returns_none_on_miss(
        self, client: RdmaThinClient, mock_mq_client: MagicMock
    ) -> None:
        future: MessagingFuture = MessagingFuture()
        future.set_result((b"", False))
        mock_mq_client.submit_request.return_value = future

        result = client.retrieve(
            "req-1", token_ids=[99, 100, 101, 102], numel=4
        )

        assert result is None


class TestLifecycle:
    def test_context_manager(self, mock_mq_client: MagicMock) -> None:
        with patch(
            "lmcache.v1.platform.rdma.thin_client.MessageQueueClient",
            return_value=mock_mq_client,
        ):
            with RdmaThinClient("tcp://localhost:5601") as c:
                assert c is not None
        mock_mq_client.close.assert_called_once()

    def test_close_closes_mq_client(
        self, client: RdmaThinClient, mock_mq_client: MagicMock
    ) -> None:
        client.close()
        mock_mq_client.close.assert_called_once()
