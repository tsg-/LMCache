# SPDX-License-Identifier: Apache-2.0
"""Public checkpoint and prefix ownership contracts for the raw experiment."""

# Standard
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import sys

# Third Party
import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts/ipu-poc"))


class Core:
    """Model only the public index, deletion and checkpoint interface."""

    def __init__(self, entries: dict[str, int], persist: bool = True) -> None:
        self.entries = entries
        self.persist = persist
        self.dirty = 0
        self.persisted = 0

    def snapshot_indexed_keys(self) -> list[str]:
        """Return the durable key universe."""
        return list(self.entries)

    def get_metadata_many(self, keys: list[str]) -> list[Any]:
        """Return the recorded payload sizes."""
        return [
            SimpleNamespace(size=self.entries[k]) if k in self.entries else None
            for k in keys
        ]

    def delete_many(self, keys: list[str]) -> list[bool]:
        """Delete only existing unlocked keys."""
        result = [k in self.entries for k in keys]
        for key in keys:
            self.entries.pop(key, None)
        self.dirty += sum(result)
        return result

    def checkpoint_now(self) -> None:
        """Persist deletions unless the checkpoint writer rejects them."""
        if self.persist:
            self.persisted = self.dirty

    def report_status(self) -> dict[str, int]:
        """Expose the public checkpoint and drain counters."""
        return {
            "metadata_dirty_total": self.dirty,
            "metadata_persisted": self.persisted,
            "inflight_io_count": 0,
            "locked_key_count": 0,
        }


def test_raw_manifest_matches_exact_model_and_payload() -> None:
    """Neighboring prefixes must survive; counts and bytes are mandatory."""
    # Third Party
    from goal5_raw_index import owned_entries, reclaim_index

    core = Core(
        {
            "cell-bench-model@0@0@aa": 65536,
            "cell-bench-model@0@0@bb": 65536,
            "cell-other-bench-model@0@0@cc": 65536,
            "seed-bench-model@0@0@dd": 65536,
        }
    )
    entries = owned_entries(core, "cell")
    assert len(entries) == 2
    with pytest.raises(ValueError, match="count or bytes"):
        reclaim_index(core, "cell", 3, 3 * 65536, None)
    assert len(core.entries) == 4
    manifest = reclaim_index(core, "cell", 2, 2 * 65536, None)
    assert manifest == entries
    assert len(core.entries) == 2
    assert core.persisted == core.dirty == 2
    reclaim_index(core, "cell", 2, 2 * 65536, manifest)
    core.entries["cell-bench-model@0@0@new"] = 65536
    with pytest.raises(ValueError, match="manifest changed"):
        reclaim_index(core, "cell", 2, 2 * 65536, manifest)


def test_raw_reclaim_rejects_unpersisted_deletion() -> None:
    """A warning-only checkpoint failure cannot produce a success receipt."""
    # Third Party
    from goal5_raw_index import reclaim_index

    core = Core({"cell-bench-model@0@0@aa": 65536}, persist=False)
    with pytest.raises(ValueError, match="checkpoint"):
        reclaim_index(core, "cell", 1, 65536, None)


def test_restart_checks_every_byte_and_rejects_a_noop_load() -> None:
    """A recovered index and successful bitmap alone cannot establish byte integrity."""
    # Third Party
    from goal5_raw_index import restart_verify

    # First Party
    from lmcache.cli.commands.bench.l2_adapter_bench.data import make_object_keys
    from lmcache.v1.storage_backend.raw_block.key_codec import object_key_to_string

    prefix = "goal5-test-123456789abc"
    entries = {
        object_key_to_string(key): 4096
        for key in make_object_keys(4, prefix + "-bench-model")
    }

    class RecoveredCore(Core):
        """Expose recovered status and either real payload copies or a no-op."""

        noop = False

        def report_status(self) -> dict[str, Any]:
            """Expose a recovered, durable and drained checkpoint."""
            return {
                **super().report_status(),
                "metadata_seq": 1,
                "native_io": {"issued_pending": 0, "outstanding_requests": 0},
            }

        def load_many_into(self, keys: list[str], objs: list[Any]) -> list[bool]:
            """Copy the known payload or falsely claim a successful no-op."""
            assert all(key in self.entries for key in keys)
            if not self.noop:
                for index, obj in enumerate(objs):
                    obj.raw_data.fill_(index)
            return [True] * len(keys)

    core = RecoveredCore(entries)
    cfg = {
        "run_id": "goal5-test",
        "dataset_submits": 2,
        "geometry": {
            "profile_sha256": "123456789abc",
            "objects_per_submit": 2,
            "max_object_bytes": 4096,
            "task_size_bytes": 8192,
        },
    }
    result = restart_verify(core, cfg)
    assert result["verified_keys"] == 4 and result["verified_payload_bytes"] == 16384
    core.noop = True
    with pytest.raises(ValueError, match="payload mismatch"):
        restart_verify(core, cfg)
