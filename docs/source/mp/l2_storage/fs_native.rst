FS (native)
===========

A file-system L2 adapter backed by the native C++ ``LMCacheFSClient``
wrapped with ``NativeConnectorL2Adapter``.  I/O is dispatched through a
C++ worker-thread pool with eventfd-driven completions, giving a true
I/O queue depth on a single Python thread.

**Required fields:**

- ``base_path``: Directory for storing KV cache files.

**Optional fields:**

- ``num_workers`` (int, default ``4``, > 0): Number of C++ worker threads
  inside the connector.  With ``num_read_workers`` at ``0`` this is also the
  read queue depth against the device, since each worker reads one object
  at a time.
- ``relative_tmp_dir`` (str, default ``""``): Relative sub-directory for
  temporary files during writes (atomic rename on completion).
- ``use_odirect`` (bool, default ``false``): Bypass the page cache via
  ``O_DIRECT``.  Required to measure real disk bandwidth.  See alignment
  caveat below.
- ``read_ahead_size`` (int, optional): Trigger filesystem readahead by
  issuing a warm-up read of this many bytes at open time.  This is skipped
  for reads that use ``O_DIRECT`` because direct I/O bypasses the page cache.
- ``max_capacity_gb`` (float, default ``0``): Maximum L2 capacity in GB
  for client-side usage tracking.  Default ``0`` disables tracking.
- ``num_read_workers`` (int, default ``256``): Threads dedicated to reads,
  and so the maximum reads in flight.  ``0`` keeps the legacy path, where
  the depth equals ``num_workers``, usually far below what an array of
  several devices needs.  Each reader thread holds one file open at a time.
- ``read_max_bytes_in_flight`` (int, default ``0``): With ``num_read_workers``
  positive, the bytes kept outstanding against the device.  ``0``
  selects 1536 MiB, chosen for its worst
  case across local and network-latency storage; a single slow device
  does better with a smaller value.  Bytes in flight also never exceed
  ``num_read_workers`` x object size, so ``num_read_workers`` must be
  large enough for the budget to bind.  Sizing notes:
  ``docs/design/v1/distributed/l2_adapters/fs_native_read_depth.md``.
- ``num_shard_dirs`` (int, default ``0``, at most ``65536``): Spread
  objects over this many hashed subdirectories of ``base_path``.  Each
  store looks up, creates and renames a file, and each of those takes the
  directory's inode lock, so with many workers a single flat directory
  makes stores and loads wait on each other.  Use ``256`` when stores run
  at high concurrency.  Every reader of ``base_path`` must use the same
  value as the writer; objects stored with another value are not found.
- ``recycle_max_files`` (int, default ``0``): On delete, move up to this
  many object files to ``.pool-*`` names in their own shard directory
  instead of unlinking them; a later store of the same size overwrites a pooled file
  in place and renames it into position.  Overwriting allocated blocks
  keeps the SSD-visible footprint flat under steady store and delete
  churn without ``fstrim``.  A pooled file is reused only after a 1 s
  quarantine, and only if it was parked before every load still in
  flight started, so a slow load that opened the file before the delete
  returns the deleted object's bytes, never the next object's.  Pooled
  files left by an earlier process are adopted.  The client holds an
  exclusive lock on ``base_path/.lmcache-recycle.lock`` while it runs; a
  second client with ``recycle_max_files > 0`` on the same ``base_path``
  logs a warning and runs with recycling disabled (deletes unlink, stores
  create new files) and never touches the first client's pooled files.
  A child created with ``fork()`` that does not call ``exec`` keeps the
  lock until it exits, so a later client on that ``base_path`` runs
  without recycling.
  A client without recycling that reads a ``base_path`` another client
  recycles is not protected and is unsupported: its loads can read
  another object's bytes.
  Reuse needs a pooled file of the same size in the same shard, so mixed
  object sizes reuse less.  Recycled objects are not crash-safe: the
  overwrite is not synced before the rename, so after a power loss an
  object can hold another object's earlier bytes.  Use it only where
  objects are not read back after a restart.
- ``store_workers`` / ``delete_workers`` (int, default ``0``): Dedicated
  worker threads for stores and deletes.  ``0`` runs them on the shared
  ``num_workers`` pool, where their blocking metadata work queues ahead
  of loads.

**io_uring read engine (experimental, opt-in).**  Instead of the read pool,
each worker can pipeline the reads of a batch through its own io_uring.

- ``read_engine`` (str, default ``pool``): ``pool`` uses the read pool;
  ``io_uring`` uses the io_uring engine and fails construction if it is
  unavailable; ``auto`` uses io_uring when available and otherwise falls
  back to the pool with a logged reason.
- ``io_depth`` (int, default ``64``, 1 to 4096): Reads in flight per worker
  ring.  Under ``auto``, a depth that the descriptor limit forces below 16
  selects the pool instead.

The engine is compiled in only when ``lmcache_fs`` is built with
``LMCACHE_WITH_LIBURING=1`` (system prefixes) or ``LMCACHE_LIBURING_DIR``
(an install prefix) pointing at liburing 2.2 or newer with a
position-independent ``liburing.a``, which is linked statically.  Published
wheels do not include it yet.  ``auto`` falls back to the pool when the
extension was built without liburing, when a ring cannot be created, or when
the kernel does not support ``IORING_OP_READ``; ``io_uring`` raises in each
case.

Under ``auto`` and ``io_uring`` the connector raises the process-wide soft
``RLIMIT_NOFILE`` toward ``num_workers`` x ``io_depth`` + 1024, never past the
hard limit and never down.  The raise stays in effect even if ``auto`` then
falls back to the pool.  If the limit is still short, ``io_uring`` raises and
``auto`` lowers ``io_depth`` to fit.  If the fitted depth would be below 16,
``auto`` uses the pool.  If reaping completions fails for good after an
engine error, the process aborts, because the kernel may still be writing
into the caller's buffers.

.. important::

   ``O_DIRECT`` has two independent alignment requirements:

   1. **Length alignment.**  The transfer length must be a multiple of
      the filesystem's block size.  With ``use_odirect: true`` the adapter
      automatically transfers each object's full alignment-padded L1 slot
      (logical bytes plus padding up to ``--l1-align-bytes``), so this
      requirement holds even when KV chunk byte sizes are not multiples of
      the block size; on-disk files are correspondingly padded, and loads
      must run with the same ``use_odirect`` and ``--l1-align-bytes``
      settings as the stores.  Raise ``--l1-align-bytes`` to match the
      block size on filesystems with large blocks (GPFS and similar
      parallel filesystems often use several MiB).

   2. **Memory-buffer alignment.**  The I/O buffer pointer itself must
      also be aligned (typically to 4096 bytes on local disks, or to the
      FS block size on parallel filesystems).  This is controlled by
      ``--l1-align-bytes`` (default ``4096``) -- raise it to match the
      FS block size when running on a filesystem with larger blocks.  If
      the buffer is misaligned, the connector reports a runtime error instead
      of silently falling back to buffered I/O.  This protects real-disk
      benchmark runs from accidentally measuring the page cache.

   If unsure, start with ``use_odirect: false`` and confirm correctness
   before enabling ``O_DIRECT``.

**Configuration examples:**

.. code-block:: bash

    # Basic native FS adapter
    --l2-adapter '{"type": "fs_native", "base_path": "/data/lmcache/l2"}'

    # Many worker threads for a parallel filesystem (e.g. GPFS, Lustre)
    --l2-adapter '{"type": "fs_native", "base_path": "/data/lmcache/l2", "num_workers": 32}'

    # O_DIRECT for real-disk benchmarking
    --l2-adapter '{"type": "fs_native", "base_path": "/data/lmcache/l2", "num_workers": 32, "use_odirect": true}'

**Buffer-only mode example.**  L1 acts as a pure write buffer that
absorbs the peak burst of in-flight chunks while the C++ worker pool
drains them to disk; nothing is retained in L1 once a store completes:

.. code-block:: bash

    lmcache server \
        --host 0.0.0.0 --port 5555 \
        --max-workers 32 \
        --l1-size-gb 32 --l1-use-lazy \
        --eviction-policy noop \
        --l2-store-policy skip_l1 \
        --l2-adapter '{"type": "fs_native", "base_path": "/data/lmcache/l2", "num_workers": 32, "use_odirect": true}'
