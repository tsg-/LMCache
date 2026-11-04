# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the fs_native L2 adapter config (no native extension required).

``max_capacity_gb`` only declares capacity for usage accounting; eviction runs
only when the adapter spec also carries an ``eviction`` block. These tests pin
the warning that makes that non-enforcement visible, so a declared cap can no
longer be silently ignored.
"""

# Standard
from unittest.mock import patch

# Third Party
import pytest

# First Party
from lmcache.v1.distributed.l2_adapters import fs_native_l2_adapter
from lmcache.v1.distributed.l2_adapters.fs_native_l2_adapter import (
    FSNativeL2AdapterConfig,
)

BASE = {"type": "fs_native", "base_path": "/data/lmcache"}


def _from_dict(**extra):
    """Parse a config spec, returning (config, warning_calls)."""
    with patch.object(fs_native_l2_adapter.logger, "warning") as warn:
        cfg = FSNativeL2AdapterConfig.from_dict({**BASE, **extra})
    return cfg, warn.call_args_list


class TestFSNativeCapacityEvictionWarning:
    def test_capacity_without_eviction_warns(self):
        cfg, warnings = _from_dict(max_capacity_gb=4000)
        assert cfg.max_capacity_gb == 4000
        assert len(warnings) == 1
        msg = warnings[0][0][0] % tuple(warnings[0][0][1:])
        assert "eviction" in msg
        assert "4000" in msg
        assert "/data/lmcache" in msg

    def test_capacity_with_eviction_does_not_warn(self):
        cfg, warnings = _from_dict(
            max_capacity_gb=4000,
            eviction={"eviction_policy": "LRU"},
        )
        assert cfg.max_capacity_gb == 4000
        assert warnings == []

    def test_no_capacity_does_not_warn(self):
        cfg, warnings = _from_dict()
        assert cfg.max_capacity_gb == 0
        assert warnings == []

    def test_zero_capacity_does_not_warn(self):
        cfg, warnings = _from_dict(max_capacity_gb=0)
        assert cfg.max_capacity_gb == 0
        assert warnings == []


class TestFSNativeCapacityHelpText:
    def test_help_does_not_claim_to_bound_disk_usage(self):
        help_text = FSNativeL2AdapterConfig.help()
        assert "max_capacity_gb" in help_text
        # The old wording ("max L2 capacity ... for usage tracking / eviction")
        # read as an enforced cap; it must name the eviction requirement.
        assert "eviction" in help_text
        assert "max L2 capacity" not in help_text


class TestFSNativeReadPoolDefault:
    def test_read_pool_is_on_by_default(self):
        cfg, _ = _from_dict()
        assert cfg.num_read_workers > 0
        assert FSNativeL2AdapterConfig(base_path="/data").num_read_workers > 0

    def test_zero_selects_the_legacy_path(self):
        cfg, _ = _from_dict(num_read_workers=0)
        assert cfg.num_read_workers == 0

    def test_budget_without_read_pool_warns_that_it_is_ignored(self):
        _, warnings = _from_dict(num_read_workers=0, read_max_bytes_in_flight=1 << 30)
        assert len(warnings) == 1
        assert "ignored" in warnings[0].args[0]

    def test_budget_with_read_pool_does_not_warn(self):
        _, warnings = _from_dict(read_max_bytes_in_flight=1 << 30)
        assert warnings == []


class TestFSNativeReadPoolValidation:
    @pytest.mark.parametrize("field", ["num_read_workers", "read_max_bytes_in_flight"])
    @pytest.mark.parametrize("bad", [-1, True, 1.5, "256", None])
    def test_from_dict_rejects(self, field, bad):
        with pytest.raises(ValueError, match=field):
            FSNativeL2AdapterConfig.from_dict({**BASE, field: bad})

    @pytest.mark.parametrize("field", ["num_read_workers", "read_max_bytes_in_flight"])
    def test_constructor_rejects(self, field):
        with pytest.raises(ValueError, match=field):
            FSNativeL2AdapterConfig(base_path="/data", **{field: -1})


_LAYOUT_FIELDS = ["recycle_max_files", "store_workers", "delete_workers"]


class TestFSNativeLayoutAndLaneValidation:
    def test_defaults_keep_flat_layout_and_shared_pool(self):
        cfg, _ = _from_dict()
        assert cfg.num_shard_dirs == 0
        assert cfg.recycle_max_files == 0
        assert cfg.store_workers == 0
        assert cfg.delete_workers == 0

    def test_from_dict_reads_every_field(self):
        cfg, _ = _from_dict(
            num_shard_dirs=256,
            recycle_max_files=200000,
            store_workers=64,
            delete_workers=16,
        )
        assert cfg.num_shard_dirs == 256
        assert cfg.recycle_max_files == 200000
        assert cfg.store_workers == 64
        assert cfg.delete_workers == 16

    @pytest.mark.parametrize("field", ["num_shard_dirs", *_LAYOUT_FIELDS])
    @pytest.mark.parametrize("bad", [-1, True, 1.5, "8", None])
    def test_from_dict_rejects(self, field, bad):
        with pytest.raises(ValueError, match=field):
            FSNativeL2AdapterConfig.from_dict({**BASE, field: bad})

    def test_shard_count_above_limit_is_rejected(self):
        limit = fs_native_l2_adapter.MAX_SHARD_DIRS
        assert _from_dict(num_shard_dirs=limit)[0].num_shard_dirs == limit
        with pytest.raises(ValueError, match="num_shard_dirs"):
            FSNativeL2AdapterConfig.from_dict({**BASE, "num_shard_dirs": limit + 1})

    @pytest.mark.parametrize("field", ["num_shard_dirs", *_LAYOUT_FIELDS])
    def test_constructor_rejects(self, field):
        with pytest.raises(ValueError, match=field):
            FSNativeL2AdapterConfig(base_path="/data", **{field: -1})


class TestFSNativeReadEngine:
    def test_defaults_keep_the_pool_and_depth_64(self):
        cfg, _ = _from_dict()
        assert cfg.read_engine == "pool"
        assert cfg.io_depth == 64
        direct = FSNativeL2AdapterConfig(base_path="/data")
        assert direct.read_engine == "pool"
        assert direct.io_depth == 64

    @pytest.mark.parametrize("engine", ["auto", "io_uring", "pool"])
    def test_from_dict_reads_engine_and_depth(self, engine):
        cfg, _ = _from_dict(read_engine=engine, io_depth=16)
        assert cfg.read_engine == engine
        assert cfg.io_depth == 16

    @pytest.mark.parametrize("depth", [1, 4096])
    def test_depth_limits_are_accepted(self, depth):
        assert _from_dict(io_depth=depth)[0].io_depth == depth

    @pytest.mark.parametrize("bad", ["bogus", "", "POOL", 1, None])
    def test_from_dict_rejects_engine(self, bad):
        with pytest.raises(ValueError, match="read_engine"):
            FSNativeL2AdapterConfig.from_dict({**BASE, "read_engine": bad})

    @pytest.mark.parametrize("bad", [0, 4097, -1, "8", True, 1.5, None])
    def test_from_dict_rejects_depth(self, bad):
        with pytest.raises(ValueError, match="io_depth"):
            FSNativeL2AdapterConfig.from_dict({**BASE, "io_depth": bad})

    def test_constructor_rejects(self):
        with pytest.raises(ValueError, match="read_engine"):
            FSNativeL2AdapterConfig(base_path="/data", read_engine="bogus")
        with pytest.raises(ValueError, match="io_depth"):
            FSNativeL2AdapterConfig(base_path="/data", io_depth=0)

    @pytest.mark.parametrize("engine", ["auto", "io_uring"])
    def test_budget_without_read_pool_is_not_ignored_under_io_uring(self, engine):
        _, warnings = _from_dict(
            read_engine=engine, num_read_workers=0, read_max_bytes_in_flight=1 << 30
        )
        assert warnings == []

    def test_help_names_both_fields(self):
        help_text = FSNativeL2AdapterConfig.help()
        assert "read_engine" in help_text
        assert "io_depth" in help_text


class _StubFSClient:
    """Stands in for LMCacheFSClient, reporting a chosen engine."""

    engine = "pool"
    depth = 0

    def __init__(self, *args: object) -> None:
        self.args = args

    def read_budget_bytes(self) -> int:
        return 1 << 30

    def read_engine(self) -> str:
        return self.engine

    def io_depth(self) -> int:
        return self.depth


def _create_with_engine(cfg, engine: str, depth: int):
    """Run the adapter factory against a stub client that reports `engine`;
    return (info_calls, warning_calls)."""
    # Standard
    import sys
    import types

    stub_cls = type("Stub", (_StubFSClient,), {"engine": engine, "depth": depth})
    fake_module = types.ModuleType("lmcache.lmcache_fs")
    fake_module.LMCacheFSClient = stub_cls  # type: ignore[attr-defined]
    # First Party
    from lmcache.v1.distributed.l2_adapters import native_connector_l2_adapter

    with (
        patch.dict(sys.modules, {"lmcache.lmcache_fs": fake_module}),
        patch.object(native_connector_l2_adapter, "NativeConnectorL2Adapter"),
        patch.object(fs_native_l2_adapter.logger, "info") as info,
        patch.object(fs_native_l2_adapter.logger, "warning") as warn,
    ):
        fs_native_l2_adapter._create_fs_native_l2_adapter(cfg)
    return info.call_args_list, warn.call_args_list


def _formatted(call) -> str:
    return call[0][0] % tuple(call[0][1:])


class TestFSNativeAdapterEngineReporting:
    def test_auto_fallback_to_legacy_warns_budget_ignored(self):
        cfg = FSNativeL2AdapterConfig(
            base_path="/data",
            read_engine="auto",
            num_read_workers=0,
            read_max_bytes_in_flight=1 << 30,
        )
        _, warnings = _create_with_engine(cfg, "legacy", 0)
        assert len(warnings) == 1
        assert "ignored" in warnings[0].args[0]

    def test_auto_on_io_uring_does_not_warn(self):
        cfg = FSNativeL2AdapterConfig(
            base_path="/data",
            read_engine="auto",
            num_read_workers=0,
            read_max_bytes_in_flight=1 << 30,
        )
        infos, warnings = _create_with_engine(cfg, "io_uring", 64)
        assert warnings == []
        line = _formatted(infos[-1])
        assert "read_engine=io_uring" in line and "io_depth=64" in line
        assert "num_read_workers" not in line

    def test_pool_log_line_names_num_read_workers(self):
        cfg = FSNativeL2AdapterConfig(base_path="/data", num_read_workers=8)
        infos, _ = _create_with_engine(cfg, "pool", 0)
        line = _formatted(infos[-1])
        assert "num_read_workers=8" in line and "read_engine=pool" in line
