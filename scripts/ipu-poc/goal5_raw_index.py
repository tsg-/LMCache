# SPDX-License-Identifier: Apache-2.0
"""Verify fresh raw prefixes and reclaim only checkpointed successful Stores."""

# Future
from __future__ import annotations

# Standard
from pathlib import Path
from typing import Any
import hashlib
import json
import sys
import urllib.parse

# Third Party
from goal5_overnight import (
    DEPLOY,
    adapter,
    config_digest,
    validate_evidence,
    write_json,
    write_prefix,
)


def owned_entries(core: Any, prefix: str) -> dict[str, int]:
    """Read payload sizes for the exact encoded benchmark model namespace."""
    keys = [
        key
        for key in core.snapshot_indexed_keys()
        if urllib.parse.unquote(key.split("@", 1)[0]) == prefix + "-bench-model"
    ]
    result = {}
    for offset in range(0, len(keys), 4096):
        batch = keys[offset : offset + 4096]
        metadata = core.get_metadata_many(batch)
        if len(metadata) != len(batch) or any(meta is None for meta in metadata):
            raise ValueError("Raw Store metadata disappeared")
        result.update(
            {key: int(meta.size) for key, meta in zip(batch, metadata, strict=True)}
        )
    return result


def reclaim_index(
    core: Any,
    prefix: str,
    count: int,
    payload_bytes: int,
    manifest: dict[str, int] | None,
) -> dict[str, int]:
    """Delete unlocked owned keys, requiring exact evidence and a durable checkpoint.

    Args:
        core: Public RawBlockCore index/checkpoint interface.
        prefix: Cell's unique Store key prefix.
        count: Original successful Store key count.
        payload_bytes: Original successful Store payload bytes.
        manifest: Previously retained full manifest, if reclamation was interrupted.

    Returns:
        Full owned manifest, including already reclaimed entries.

    Raises:
        ValueError: Counts, bytes, ownership, locks, drain or persistence differ.
    """
    current = owned_entries(core, prefix)
    expected = current if manifest is None else manifest
    if len(expected) != count or sum(expected.values()) != payload_bytes:
        raise ValueError("Raw Store manifest count or bytes differs")
    if any(
        key not in expected or expected[key] != size for key, size in current.items()
    ):
        raise ValueError("Raw Store manifest changed")
    status = core.report_status()
    if status["inflight_io_count"] or status["locked_key_count"]:
        raise ValueError("Raw index is not drained and unlocked")
    keys = list(current)
    for offset in range(0, len(keys), 4096):
        if not all(core.delete_many(keys[offset : offset + 4096])):
            raise ValueError("Raw owned Store deletion failed")
    core.checkpoint_now()
    status = core.report_status()
    if status["metadata_persisted"] != status["metadata_dirty_total"]:
        raise ValueError("Raw deletion checkpoint did not persist")
    if owned_entries(core, prefix):
        raise ValueError("Raw owned prefix remained after deletion")
    return expected


def open_core(cfg: dict[str, Any], host: str) -> Any:
    """Open the bound raw index through the public adapter configuration API."""
    # First Party
    from lmcache.v1.distributed.l2_adapters.raw_block_l2_adapter import (
        RawBlockL2AdapterConfig,
    )
    from lmcache.v1.storage_backend.raw_block.core import RawBlockCore

    selected = adapter(cfg, host)
    selected["meta_enable_periodic"] = False
    return RawBlockCore(
        RawBlockL2AdapterConfig.from_dict(selected).to_core_config(),
        key_namespace="object",
    )


def key_digest(keys: list[str]) -> str:
    """Hash the sorted durable key universe to detect foreign deletion on restart."""
    digest = hashlib.sha256()
    for key in sorted(keys):
        digest.update(key.encode())
        digest.update(b"\0")
    return digest.hexdigest()


def restart_verify(core: Any, cfg: dict[str, Any]) -> dict[str, Any]:
    """Read every seeded KV byte after fresh checkpoint recovery using public APIs."""
    # Third Party
    import torch

    # First Party
    from lmcache.cli.commands.bench.l2_adapter_bench.data import (
        make_aligned_tensor,
        make_memory_objects_from_sizes,
        make_object_keys,
    )
    from lmcache.v1.storage_backend.raw_block.key_codec import object_key_to_string

    geometry = cfg["geometry"]
    count, size = geometry["objects_per_submit"], geometry["max_object_bytes"]
    if geometry["task_size_bytes"] != count * size:
        raise ValueError("Raw restart gate requires the bound uniform page geometry")
    prefix = f"{cfg['run_id']}-{geometry['profile_sha256'][:12]}"
    entries = owned_entries(core, prefix)
    if (
        len(entries) != cfg["dataset_submits"] * count
        or sum(entries.values()) != cfg["dataset_submits"] * count * size
    ):
        raise ValueError("Recovered raw seed key universe differs")
    buffer = make_aligned_tensor(count * size, 4096)
    objects = make_memory_objects_from_sizes(buffer, [size] * count, 0, fill_offset=1)
    before = core.report_status()
    if before["metadata_seq"] <= 0:
        raise ValueError("Raw seed was not recovered from a durable checkpoint")
    for submit in range(cfg["dataset_submits"]):
        keys = [
            object_key_to_string(key)
            for key in make_object_keys(
                count, model_name=prefix + "-bench-model", key_offset=submit * count
            )
        ]
        for index, obj in enumerate(objects):
            obj.raw_data.fill_((index + 1) & 255)
        if not all(core.load_many_into(keys, objects)) or any(
            not torch.all(obj.raw_data == (index & 255)).item()
            for index, obj in enumerate(objects)
        ):
            raise ValueError("Fresh-process raw checkpoint payload mismatch")
    status = core.report_status()
    if (
        status["native_io"]["issued_pending"]
        or status["native_io"]["outstanding_requests"]
        or status["inflight_io_count"]
        or status["locked_key_count"]
        or status["metadata_persisted"] != status["metadata_dirty_total"]
    ):
        raise ValueError("Raw checkpoint restart did not drain")
    return {
        "config_sha256": config_digest(cfg),
        "prefix": prefix,
        "verified_keys": len(entries),
        "verified_payload_bytes": sum(entries.values()),
        "before": before,
        "after": status,
        "scope": "Fresh process recovered the checkpoint, re-addressed the full "
        "seed key universe and compared every payload byte with the bound CLI "
        "uniform-page fill pattern. Load buffers began with a different pattern.",
    }


def process(request: dict[str, Any]) -> dict[str, Any]:
    """Check a fresh prefix or produce a deletion receipt after checkpoint restart."""
    cfg, host = request["cfg"], request["host"]
    if cfg["backend"] != "raw_io_uring":
        raise ValueError("Raw index helper requires ordinary io_uring")
    if request["action"] == "restart-verify":
        core = open_core(cfg, host)
        try:
            return restart_verify(core, cfg)
        finally:
            core.close()
    cell = request["cell"] if "cell" in request else request["row"]["cell"]
    prefix = (
        f"{cfg['run_id']}-{cfg['geometry']['profile_sha256'][:12]}"
        + ("-canary" if cell["mode"] == "canary" else "")
        if cell["mode"] in ("canary", "store")
        else write_prefix(cfg, host, cell["in_flight"], cell["repetition"])
    )
    core = open_core(cfg, host)
    try:
        if request["action"] == "guard":
            status = core.report_status()
            if owned_entries(core, prefix):
                raise ValueError("Mixed Store prefix is not fresh")
            free_slots = status["max_slots"] - status["indexed_key_count"]
            # Match the filesystem gate's ceiling and reserve, including raw headers.
            needed = 1024**4 + 100_000_000_000 * cfg["duration_sec"] // 6
            if free_slots * cfg["geometry"]["max_object_bytes"] < needed:
                raise ValueError("Insufficient raw capacity for mixed Store window")
            return {"prefix": prefix, "fresh": True, "status": status}
        if request["action"] != "reclaim":
            raise ValueError("Unknown raw index action")
        row = request["row"]
        if not validate_evidence(
            row, cfg, host, cell["in_flight"], cell["repetition"], "mixed"
        ):
            raise ValueError("Raw reclamation requires successful mixed evidence")
        store = next(
            value
            for key, value in row["result"]["metrics"].items()
            if key.startswith("op_") and value["operation"] == "Store"
        )
        binding = {
            "config_sha256": config_digest(cfg),
            "prefix": prefix,
            "files": store["total_success"],
            "bytes": store["total_success_bytes"],
        }
        path = Path(DEPLOY) / "host-results" / cfg["run_id"] / f"{prefix}.reclaim.json"
        manifest_path = path.with_suffix(".manifest.json")
        if path.exists():
            saved = json.loads(path.read_text())
            if any(saved[key] != value for key, value in binding.items()):
                raise ValueError("Raw reclamation receipt differs")
            if owned_entries(core, prefix):
                raise ValueError("Reclaimed raw prefix reappeared")
            return saved
        entries = owned_entries(core, prefix)
        foreign = key_digest(
            [key for key in core.snapshot_indexed_keys() if key not in entries]
        )
        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text())
            if (
                manifest["receipt"] != binding
                or manifest["foreign_keys_sha256"] != foreign
            ):
                raise ValueError("Raw reclamation manifest identity differs")
            expected = manifest["entries"]
        else:
            if (
                len(entries) != binding["files"]
                or sum(entries.values()) != binding["bytes"]
            ):
                raise ValueError("Raw Store manifest count or bytes differs")
            expected = entries
            write_json(
                manifest_path,
                {
                    "receipt": binding,
                    "entries": entries,
                    "foreign_keys_sha256": foreign,
                },
            )
        reclaim_index(core, prefix, binding["files"], binding["bytes"], expected)
        status = core.report_status()
    finally:
        core.close()
    restarted = open_core(cfg, host)
    try:
        if (
            owned_entries(restarted, prefix)
            or key_digest(restarted.snapshot_indexed_keys()) != foreign
        ):
            raise ValueError("Raw checkpoint restart changed retained keys")
        receipt = {
            **binding,
            "checkpoint": status,
            "restart": restarted.report_status(),
            "foreign_keys_sha256": foreign,
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        }
        write_json(path, receipt)
        return receipt
    finally:
        restarted.close()


if __name__ == "__main__":
    print(json.dumps(process(json.load(sys.stdin))))
