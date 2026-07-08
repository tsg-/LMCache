# SPDX-License-Identifier: Apache-2.0
"""Unit tests for NixlWrapper.

All nixl imports are mocked — no NIXL hardware or Python bindings required.
"""

from __future__ import annotations

import sys
from types import ModuleType
from unittest.mock import MagicMock

import pytest
import torch

from lmcache.v1.platform.base_ipc_wrapper import DeviceIPCWrapper


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FAKE_XFER_DESCS = b"fake_xfer_descs_bytes"


def _make_nixl_mock():
    """Return a minimal nixl._api module mock with the key classes."""
    mod = ModuleType("nixl._api")

    agent_instance = MagicMock()
    agent_instance.name = "test_agent"
    agent_instance.get_agent_metadata.return_value = b"agent_meta"
    agent_instance.get_xfer_descs.return_value = MagicMock()
    agent_instance.get_serialized_descs.return_value = _FAKE_XFER_DESCS
    agent_instance.register_memory.return_value = MagicMock()
    agent_instance.deregister_memory.return_value = None
    agent_instance.get_reg_descs.return_value = MagicMock()

    agent_cls = MagicMock(return_value=agent_instance)
    config_cls = MagicMock(return_value=MagicMock())

    mod.nixl_agent = agent_cls
    mod.nixl_agent_config = config_cls
    mod._agent_instance = agent_instance
    return mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def patched_nixl(monkeypatch):
    """Inject a fake nixl._api and return both the module and agent instance."""
    fake_mod = _make_nixl_mock()
    monkeypatch.setitem(sys.modules, "nixl._api", fake_mod)
    monkeypatch.setitem(sys.modules, "nixl", MagicMock(_api=fake_mod))
    for key in list(sys.modules):
        if "nixl_wrapper" in key:
            monkeypatch.delitem(sys.modules, key, raising=False)
    yield fake_mod
    try:
        import importlib
        nw = importlib.import_module("lmcache.v1.platform.rdma.nixl_wrapper")
        nw._AGENT = None  # type: ignore[attr-defined]
        nw._REG_PTRS.clear()  # type: ignore[attr-defined]
    except Exception:
        pass


def _make_wrapper(**kwargs):
    """Build a NixlWrapper with test-friendly defaults."""
    from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper
    defaults = dict(
        agent_name="test_agent",
        agent_metadata=b"meta",
        serialized_xfer_descs=_FAKE_XFER_DESCS,
        shape=(16,),
        dtype=torch.float32,
        length=64,
        stride=(1,),
        storage_offset=0,
    )
    defaults.update(kwargs)
    return NixlWrapper(**defaults)


# ---------------------------------------------------------------------------
# Tests: NixlWrapper attributes and serialization
# ---------------------------------------------------------------------------


class TestNixlWrapperAttributes:
    """NixlWrapper field layout after construction."""

    def test_device_type_is_nixl(self) -> None:
        from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper
        assert NixlWrapper.device_type == "nixl"

    def test_init_stores_all_fields(self) -> None:
        w = _make_wrapper(
            agent_name="a",
            agent_metadata=b"meta",
            serialized_xfer_descs=b"descs",
            shape=(32,),
            dtype=torch.float16,
            length=128,
            stride=(1,),
            storage_offset=0,
        )
        assert w.agent_name == "a"
        assert w.agent_metadata == b"meta"
        assert w.serialized_xfer_descs == b"descs"
        assert w.shape == (32,)
        assert w.dtype == torch.float16
        assert w.length == 128

    def test_handle_is_tuple_of_name_len_length(self) -> None:
        w = _make_wrapper(
            agent_name="agt",
            serialized_xfer_descs=b"x" * 10,
            length=64,
        )
        assert w.handle == ("agt", 10, 64)


class TestNixlWrapperSerialization:
    """Round-trip pickle via DeviceIPCWrapper.Serialize / Deserialize."""

    def test_serialize_deserialize_roundtrip(self) -> None:
        original = _make_wrapper(
            agent_name="worker_42",
            agent_metadata=b"agent_bytes",
            serialized_xfer_descs=b"descs_bytes",
            shape=(64,),
            dtype=torch.float32,
            length=256,
        )
        data = DeviceIPCWrapper.Serialize(original)
        recovered = DeviceIPCWrapper.Deserialize(data)

        assert isinstance(recovered, type(original))
        assert recovered.agent_name == "worker_42"
        assert recovered.agent_metadata == b"agent_bytes"
        assert recovered.serialized_xfer_descs == b"descs_bytes"
        assert recovered.length == 256
        assert recovered.shape == (64,)
        assert recovered.dtype == torch.float32

    def test_to_tensor_raises(self) -> None:
        w = _make_wrapper()
        with pytest.raises(NotImplementedError):
            w.to_tensor()


class TestNixlWrapperWrap:
    """NixlWrapper.wrap() registers memory and builds xfer dlist."""

    def test_wrap_returns_nixl_wrapper(self, patched_nixl) -> None:
        from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper

        tensor = torch.zeros(32, dtype=torch.float32)
        wrapper = NixlWrapper.wrap(tensor)

        assert isinstance(wrapper, NixlWrapper)
        assert wrapper.length == tensor.numel() * tensor.element_size()
        assert wrapper.dtype == tensor.dtype
        assert wrapper.shape == tuple(tensor.shape)
        assert wrapper.serialized_xfer_descs == _FAKE_XFER_DESCS

    def test_wrap_calls_register_memory(self, patched_nixl) -> None:
        from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper

        tensor = torch.zeros(16, dtype=torch.float32)
        NixlWrapper.wrap(tensor)

        patched_nixl._agent_instance.register_memory.assert_called_once()

    def test_wrap_calls_get_xfer_descs(self, patched_nixl) -> None:
        from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper

        tensor = torch.zeros(16, dtype=torch.float32)
        NixlWrapper.wrap(tensor)

        patched_nixl._agent_instance.get_xfer_descs.assert_called_once()
        patched_nixl._agent_instance.get_serialized_descs.assert_called_once()

    def test_wrap_non_contiguous_raises(self, patched_nixl) -> None:
        from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper

        tensor = torch.zeros(4, 4)[::2]  # non-contiguous
        with pytest.raises(ValueError, match="contiguous"):
            NixlWrapper.wrap(tensor)

    def test_wrap_idempotent_mr_registration_for_same_tensor(
        self, patched_nixl
    ) -> None:
        """Wrapping the same tensor twice reuses the registered MR."""
        from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper

        tensor = torch.zeros(32, dtype=torch.float32)
        NixlWrapper.wrap(tensor)
        call_count_after_first = patched_nixl._agent_instance.register_memory.call_count

        NixlWrapper.wrap(tensor)
        # register_memory must NOT be called again for the same tensor.
        assert patched_nixl._agent_instance.register_memory.call_count == call_count_after_first

    def test_wrap_always_builds_fresh_xfer_dlist(self, patched_nixl) -> None:
        """get_xfer_descs is called on every wrap() for a fresh dlist."""
        from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper

        tensor = torch.zeros(32, dtype=torch.float32)
        NixlWrapper.wrap(tensor)
        NixlWrapper.wrap(tensor)

        # xfer dlist is built fresh on every wrap.
        assert patched_nixl._agent_instance.get_xfer_descs.call_count == 2

    def test_wrap_sets_agent_name_from_agent(self, patched_nixl) -> None:
        from lmcache.v1.platform.rdma.nixl_wrapper import NixlWrapper

        tensor = torch.zeros(8, dtype=torch.float32)
        wrapper = NixlWrapper.wrap(tensor)

        assert wrapper.agent_name == "test_agent"
        assert wrapper.agent_metadata == b"agent_meta"
