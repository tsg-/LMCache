# SPDX-License-Identifier: Apache-2.0
"""
Filesystem native L2 adapter config and factory.

Backed by the native C++ filesystem connector wrapped with
``NativeConnectorL2Adapter``.
"""

# Future
from __future__ import annotations

# Standard
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from lmcache.v1.distributed.internal_api import (
        L1MemoryDesc,
    )

# First Party
from lmcache.logging import init_logger
from lmcache.v1.distributed.api import MemoryResidency, MemorySegment, MemoryTags
from lmcache.v1.distributed.l2_adapters.base import (
    L2AdapterInterface,
)
from lmcache.v1.distributed.l2_adapters.config import (
    L2AdapterConfigBase,
    register_l2_adapter_type,
)
from lmcache.v1.distributed.l2_adapters.factory import (
    register_l2_adapter_factory,
)

logger = init_logger(__name__)

# Enough reader threads for the default 1536 MiB budget to bind at 6 MiB
# objects (chunk_size 256), and for small objects to reach an array's
# ceiling, which takes a few hundred reads in flight.  Idle readers hold no
# file descriptor.
DEFAULT_NUM_READ_WORKERS = 256

# Shard directories are created up front; this bounds start-up work.
MAX_SHARD_DIRS = 65536

# Read engines the native connector accepts; "pool" stays the default until
# the io_uring engine's benchmarks are in.
READ_ENGINES = ("auto", "io_uring", "pool")
DEFAULT_READ_ENGINE = "pool"
DEFAULT_IO_DEPTH = 64
MAX_IO_DEPTH = 4096


def _validate_non_negative_int(name: str, raw: object) -> int:
    """Return ``raw`` if it is a non-negative ``int``.

    ``bool`` is rejected although it subclasses ``int``: ``true`` in a
    config would otherwise mean one.

    Raises:
        ValueError: If ``raw`` is a bool, not an int, or negative.
    """
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"{name} must be a non-negative integer, got {raw!r}")
    return raw


def _validate_read_engine(raw: object) -> str:
    """Return ``raw`` if it names a read engine.

    Raises:
        ValueError: If ``raw`` is not one of ``READ_ENGINES``.
    """
    if not isinstance(raw, str) or raw not in READ_ENGINES:
        raise ValueError(
            f"read_engine must be one of {', '.join(READ_ENGINES)}, got {raw!r}"
        )
    return raw


def _validate_io_depth(raw: object) -> int:
    """Return ``raw`` if it is an ``int`` in [1, MAX_IO_DEPTH].

    Raises:
        ValueError: If ``raw`` is a bool, not an int, or out of range.
    """
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int)
        or not 1 <= raw <= MAX_IO_DEPTH
    ):
        raise ValueError(
            f"io_depth must be an integer in [1, {MAX_IO_DEPTH}], got {raw!r}"
        )
    return raw


class FSNativeL2AdapterConfig(L2AdapterConfigBase):
    """
    Config for an L2 adapter backed by the native C++
    filesystem connector.

    Fields:
    - base_path: directory for storing KV cache files.
    - num_workers: C++ worker threads for I/O (default 4).
    - relative_tmp_dir: relative sub-dir for temp files.
    - use_odirect: bypass page cache via O_DIRECT.
    - num_read_workers: threads dedicated to executing reads, and so the
      maximum reads in flight (default 256).  0 keeps the legacy path,
      where reads run on the worker threads and the depth against the
      device therefore equals num_workers.
    - read_max_bytes_in_flight: bytes the connector may keep outstanding
      against the device.  0 selects 1536 MiB when num_read_workers is
      positive; ignored otherwise.
      See docs/source/mp/l2_storage/fs_native.rst for how to size both.
    - read_ahead_size: trigger filesystem readahead by
      reading this many bytes first (optional).
    - max_capacity_gb: declared L2 capacity in GB, used for usage
      accounting. ``0`` (default) disables it. A value ``> 0`` does
      **not** bound disk usage on its own -- eviction runs only when
      the adapter spec also carries an ``eviction`` block, e.g.
      ``{"eviction": {"eviction_policy": "LRU"}}``. Without one,
      files accumulate past the declared capacity.
    - num_shard_dirs: spread objects over this many hashed
      subdirectories (default 0 = one flat directory, at most 65536).
      Readers must use the same value as the writer.
    - recycle_max_files: keep up to this many deleted object
      files for in-place reuse by later stores of the same size
      (default 0 = unlink on delete).
    - store_workers / delete_workers: dedicated worker threads
      for stores / deletes (default 0 = shared num_workers pool).
    - read_engine: "pool" (default) reads through the read pool above;
      "io_uring" gives each of the num_workers workers an io_uring and
      fails adapter creation if that is unavailable; "auto" uses io_uring
      where available and the pool otherwise.  Under io_uring the read
      pool is not started, num_workers bounds how many batches read at
      once, and read_max_bytes_in_flight applies (0 = 1536 MiB) whatever
      num_read_workers is.
    - io_depth: reads in flight per io_uring worker, 1 to 4096
      (default 64); ignored by the pool.
    """

    def __init__(
        self,
        base_path: str,
        num_workers: int = 4,
        relative_tmp_dir: str = "",
        use_odirect: bool = False,
        read_ahead_size: Optional[int] = None,
        max_capacity_gb: float = 0,
        num_read_workers: int = DEFAULT_NUM_READ_WORKERS,
        read_max_bytes_in_flight: int = 0,
        num_shard_dirs: int = 0,
        recycle_max_files: int = 0,
        store_workers: int = 0,
        delete_workers: int = 0,
        read_engine: str = DEFAULT_READ_ENGINE,
        io_depth: int = DEFAULT_IO_DEPTH,
    ):
        """Build the config; see the class docstring for each field.

        Raises:
            ValueError: If ``num_read_workers``, ``read_max_bytes_in_flight``,
                ``recycle_max_files``, ``store_workers`` or
                ``delete_workers`` is not a non-negative integer,
                ``num_shard_dirs`` is not an integer in [0, 65536],
                ``read_engine`` is not one of ``READ_ENGINES``, or
                ``io_depth`` is not an integer in [1, 4096].
        """
        self.base_path = base_path
        self.num_workers = num_workers
        self.relative_tmp_dir = relative_tmp_dir
        self.use_odirect = use_odirect
        self.read_ahead_size = read_ahead_size
        self.max_capacity_gb = max_capacity_gb
        self.num_read_workers = _validate_non_negative_int(
            "num_read_workers", num_read_workers
        )
        self.read_max_bytes_in_flight = _validate_non_negative_int(
            "read_max_bytes_in_flight", read_max_bytes_in_flight
        )
        self.num_shard_dirs = _validate_non_negative_int(
            "num_shard_dirs", num_shard_dirs
        )
        if num_shard_dirs > MAX_SHARD_DIRS:
            raise ValueError(
                f"num_shard_dirs must be at most {MAX_SHARD_DIRS}, got {num_shard_dirs}"
            )
        self.recycle_max_files = _validate_non_negative_int(
            "recycle_max_files", recycle_max_files
        )
        self.store_workers = _validate_non_negative_int("store_workers", store_workers)
        self.delete_workers = _validate_non_negative_int(
            "delete_workers", delete_workers
        )
        self.read_engine = _validate_read_engine(read_engine)
        self.io_depth = _validate_io_depth(io_depth)
        # Under io_uring the budget applies whatever num_read_workers is;
        # for auto the adapter factory warns once the engine is known.
        if (
            read_engine == "pool"
            and num_read_workers == 0
            and read_max_bytes_in_flight > 0
        ):
            logger.warning(
                "fs_native: read_max_bytes_in_flight=%d is ignored because "
                "num_read_workers=0 selects the legacy read path.",
                read_max_bytes_in_flight,
            )

    @classmethod
    def from_dict(cls, d: dict) -> "FSNativeL2AdapterConfig":
        base_path = d.get("base_path")
        if not isinstance(base_path, str) or not base_path:
            raise ValueError("base_path must be a non-empty string")

        num_workers = d.get("num_workers", 4)
        if not isinstance(num_workers, int) or num_workers <= 0:
            raise ValueError("num_workers must be a positive integer")

        relative_tmp_dir = d.get("relative_tmp_dir", "")
        if not isinstance(relative_tmp_dir, str):
            raise ValueError("relative_tmp_dir must be a string")

        use_odirect = d.get("use_odirect", False)
        if not isinstance(use_odirect, bool):
            raise ValueError("use_odirect must be a boolean")

        read_ahead_size = d.get("read_ahead_size", None)
        if read_ahead_size is not None:
            if not isinstance(read_ahead_size, int) or read_ahead_size <= 0:
                raise ValueError("read_ahead_size must be a positive integer")

        max_capacity_gb = d.get("max_capacity_gb", 0)
        if not isinstance(max_capacity_gb, (int, float)) or max_capacity_gb < 0:
            raise ValueError("max_capacity_gb must be a non-negative number")
        if max_capacity_gb > 0 and d.get("eviction") is None:
            logger.warning(
                "fs_native: max_capacity_gb=%s is declared for %s but no "
                "'eviction' block is configured, so it is used for usage "
                "accounting only and will NOT bound disk usage. Add an "
                "eviction block to enforce it, e.g. "
                '"eviction": {"eviction_policy": "LRU"}.',
                max_capacity_gb,
                base_path,
            )

        return cls(
            base_path=base_path,
            num_workers=num_workers,
            relative_tmp_dir=str(relative_tmp_dir),
            use_odirect=use_odirect,
            read_ahead_size=read_ahead_size,
            max_capacity_gb=float(max_capacity_gb),
            num_read_workers=d.get("num_read_workers", DEFAULT_NUM_READ_WORKERS),
            read_max_bytes_in_flight=d.get("read_max_bytes_in_flight", 0),
            num_shard_dirs=d.get("num_shard_dirs", 0),
            recycle_max_files=d.get("recycle_max_files", 0),
            store_workers=d.get("store_workers", 0),
            delete_workers=d.get("delete_workers", 0),
            read_engine=d.get("read_engine", DEFAULT_READ_ENGINE),
            io_depth=d.get("io_depth", DEFAULT_IO_DEPTH),
        )

    @classmethod
    def help(cls) -> str:
        return (
            "FS native L2 adapter config fields:\n"
            "- base_path (str): directory for KV "
            "cache files (required)\n"
            "- num_workers (int): C++ worker threads "
            "for I/O (default 4, >0)\n"
            "- relative_tmp_dir (str): relative "
            "sub-dir for temp files (default empty)\n"
            "- use_odirect (bool): bypass page cache "
            "via O_DIRECT (default false)\n"
            "- num_read_workers (int): threads dedicated to "
            "reads, decoupling device queue depth from "
            "num_workers (default 256, 0 = legacy path)\n"
            "- read_max_bytes_in_flight (int): bytes kept "
            "outstanding against the device (default 0 = "
            "1536 MiB when num_read_workers > 0)\n"
            "- read_ahead_size (int): trigger fs "
            "readahead by reading this many bytes "
            "first (optional)\n"
            "- max_capacity_gb (float): declared L2 capacity in GB "
            "for usage accounting (default 0 = disabled). Does not "
            "bound disk usage by itself; add an 'eviction' block "
            "to enforce it\n"
            "- num_shard_dirs (int): hashed subdirectories "
            "for objects; readers must match the writer "
            "(default 0 = flat)\n"
            "- recycle_max_files (int): deleted files kept for "
            "in-place reuse (default 0 = unlink)\n"
            "- store_workers / delete_workers (int): dedicated "
            "worker threads (default 0 = shared pool)\n"
            "- read_engine (str): auto, io_uring or pool "
            "(default pool); io_uring fails if unavailable\n"
            "- io_depth (int): reads in flight per io_uring "
            "worker, 1 to 4096 (default 64); ignored by the pool"
        )


def _create_fs_native_l2_adapter(
    config: L2AdapterConfigBase,
    l1_memory_desc: "Optional[L1MemoryDesc]" = None,
) -> L2AdapterInterface:
    """Create a NativeConnectorL2Adapter backed by the
    C++ filesystem connector."""
    try:
        # First Party
        from lmcache.lmcache_fs import (
            LMCacheFSClient,
        )
    except ImportError as e:
        raise RuntimeError(
            "FS native L2 adapter requires the C++ FS "
            "extension. Build with: pip install -e ."
        ) from e

    # Lazy import to avoid circular dependency
    # First Party
    from lmcache.v1.distributed.l2_adapters.native_connector_l2_adapter import (  # noqa: E501
        NativeConnectorL2Adapter,
    )

    assert isinstance(config, FSNativeL2AdapterConfig)
    native_client = LMCacheFSClient(
        config.base_path,
        config.num_workers,
        config.relative_tmp_dir,
        config.use_odirect,
        config.read_ahead_size or 0,
        config.num_read_workers,
        config.read_max_bytes_in_flight,
        config.num_shard_dirs,
        config.recycle_max_files,
        config.store_workers,
        config.delete_workers,
        config.read_engine,
        config.io_depth,
    )
    effective_read_budget = native_client.read_budget_bytes()
    read_engine = native_client.read_engine()
    io_depth = native_client.io_depth()
    if (
        config.read_engine == "auto"
        and read_engine == "legacy"
        and config.read_max_bytes_in_flight > 0
    ):
        logger.warning(
            "fs_native: read_max_bytes_in_flight=%d is ignored because "
            "read_engine auto fell back to the pool and num_read_workers=0 "
            "selects the legacy read path.",
            config.read_max_bytes_in_flight,
        )
    # The read pool is not started under io_uring, so its size is not shown.
    pool_part = (
        ""
        if read_engine == "io_uring"
        else f"num_read_workers={config.num_read_workers}, "
    )
    logger.info(
        "Created FS native L2 adapter: %s (workers=%d, odirect=%s, "
        "read_ahead=%s, %sread_max_bytes_in_flight=%d, read_engine=%s, "
        "io_depth=%d)",
        config.base_path,
        config.num_workers,
        config.use_odirect,
        config.read_ahead_size,
        pool_part,
        effective_read_budget,
        read_engine,
        io_depth,
    )
    residency = MemoryResidency.REMOTE if config.shared else MemoryResidency.LOCAL
    memory_tags = MemoryTags(MemorySegment.STORAGE, residency)
    return NativeConnectorL2Adapter(
        native_client,
        memory_tags,
        max_capacity_gb=config.max_capacity_gb,
        type_name="FSNativeL2Adapter",
        pad_buffers_to_alignment=config.use_odirect,
        extra_status={
            "base_path": config.base_path,
            "use_odirect": config.use_odirect,
            "num_workers": config.num_workers,
            "read_ahead_size": config.read_ahead_size,
            "num_read_workers": config.num_read_workers,
            "read_max_bytes_in_flight": effective_read_budget,
            "num_shard_dirs": config.num_shard_dirs,
            "recycle_max_files": config.recycle_max_files,
            "store_workers": config.store_workers,
            "delete_workers": config.delete_workers,
            "read_engine": read_engine,
            "io_depth": io_depth,
            "pad_buffers_to_alignment": config.use_odirect,
        },
    )


register_l2_adapter_type("fs_native", FSNativeL2AdapterConfig)
register_l2_adapter_factory("fs_native", _create_fs_native_l2_adapter)
