Raw Block (Rust)
================

A built-in L2 adapter that stores KV objects in fixed-size slots on a raw block
device or pre-sized file using the Rust raw-device I/O bindings. It reuses the
existing raw-block metadata checkpoint model and writes directly into the
caller-provided load buffers during prefetch.

**Required fields:**

- ``device_path``: Raw device path or pre-sized file path.
- ``slot_bytes``: Fixed slot size in bytes. Must be aligned to ``block_align``.

**Optional fields:**

- ``capacity_bytes``: Optional cap on the usable device bytes. Default ``0``
  means use the full device/file size.
- ``use_odirect``: ``true`` or ``false`` (default ``true``).
- ``block_align``: Device alignment in bytes (default ``4096``). Must be a
  power of two.
- ``header_bytes``: Per-slot header reservation (default ``4096``).
- ``meta_total_bytes``: Reserved metadata checkpoint region (default ``256MiB``).
- ``meta_magic`` / ``meta_version``: Metadata checkpoint identity/version knobs.
- ``meta_checkpoint_interval_sec`` / ``meta_idle_quiet_ms`` /
  ``meta_enable_periodic`` / ``meta_verify_on_load``: Checkpoint and recovery
  controls carried over from the legacy raw-block backend.
- ``load_checkpoint_on_init``: Load an existing on-device metadata checkpoint
  during startup (default ``true``). Set to ``false`` to start with an empty
  in-memory index instead.
- ``enable_zero_copy``: Try aligned direct-buffer I/O when possible.
- ``io_engine``: Rust raw-block I/O engine. Valid values are ``"posix"``
  (default synchronous ``pread``/``pwrite`` path), ``"io_uring"`` (direct Rust
  io_uring syscall path).
- ``use_uring_cmd``: Enable NVMe passthrough via io_uring command interface
  for direct device access. Requires ``io_engine="io_uring"`` and NVMe
  character device node (e.g., ``/dev/ng0n1``).
- ``iouring_queue_depth``: Queue depth for ``io_engine="io_uring"``.
- ``max_data_transfer_size``: Maximum data transfer size for
  ``use_uring_cmd=true``. Large transfers are split into smaller chunks
  that fit within device limits.
- ``num_store_workers`` / ``num_lookup_workers`` / ``num_load_workers``:
  Worker-thread counts for each operation type.

**Notes:**

- ``raw_block`` is an MP adapter that owns on-device slot allocation,
  checkpointing, and recovery through ``RawBlockCore``. It does **not**
  support per-TP device-path mappings in MP mode.
- ``raw_block`` remains ``"type": "raw_block"`` for all supported engines.
- Slot reclamation is driven by the shared/global L2 eviction controller
  or explicit ``delete()`` calls.
- ``slot_bytes``, ``header_bytes``, and ``meta_total_bytes`` must be multiples
  of ``block_align``.
- If ``use_odirect`` is enabled, the server's ``--l1-align-bytes`` should be
  at least ``block_align``.
- With ``O_DIRECT``, raw-block I/O rejects offsets and total I/O lengths that
  are not multiples of ``block_align``. Misaligned write buffers use an aligned
  bounce buffer.
- ``persist_enabled`` must remain ``true`` for this adapter.
- For ``use_uring_cmd=true``, ``device_path`` must use the NVMe character
  device node (e.g., ``/dev/ng0n1``) instead of the block device node
  (``/dev/nvme0n1``). The character device provides direct NVMe
  command passthrough.
- For ``use_uring_cmd=true``, ``block_align`` must be a multiple of the NVMe
  namespace LBA size. An incompatible value is rejected when the device opens.
- ``use_uring_cmd`` requires ``io_engine="io_uring"`` to be set.
- When ``use_uring_cmd=true``, ``use_odirect`` is ignored for NVMe namespace
  character devices.

**Deployment modes:**

- **Locally-attached namespace (default).** ``device_path`` points at a
  namespace on the same host as the adapter (e.g., ``/dev/nvme0n1``,
  ``/dev/ng0n1``). All examples below use this mode.
- **Remote namespace over NVMe-oF/RDMA (alt track).** The host attaches a
  remote namespace via ``nvme connect -t rdma ...`` and ``device_path``
  resolves to that attached device. In this mode:

  - Prefer a stable identifier such as ``/dev/disk/by-id/nvme-<eui>...``
    over ``/dev/nvmeXn1``. The kernel enumerator number is not stable
    across ``nvme disconnect`` / ``nvme connect`` cycles or reboots.
  - Tune ``ctrl-loss-tmo`` and ``reconnect_delay`` on the ``nvme connect``
    call so the adapter surfaces disconnect as an I/O error rather than
    hanging indefinitely.
  - ``use_uring_cmd=true`` (NVMe char-device passthrough via
    ``io_uring_cmd``) may not be portable to a remote namespace served
    by ``nvmet-rdma``. Validate against a specific kernel and
    ``nvme-fabrics`` version before enabling; fall back to
    ``io_engine="io_uring"`` with a block device node if unsure.

**Non-guarantee — this is not a durable cache-commit protocol:**

``raw_block`` publishes its in-memory index immediately after writing the
slot header and payload, and the only durable metadata is a periodic
mirrored checkpoint. There is no ``fsync`` / ``fdatasync`` / ``FLUSH`` /
FUA on the write path, no data checksum stored with the payload, and no
atomic (data, checksum, key→LBA map) publication. A crash between a
payload write and the next checkpoint can drop an index entry or, after
checkpoint restore, leave a stale index entry pointing at a torn or
absent payload.

That has been acceptable so far for the storage-owned deployment because
the storage node is the durable authority and dropped keys are recreated
by re-fetch on miss; there is no general on-read payload-integrity check
in ``RawBlockCore`` today. It is **not** safe for the initiator-owned +
remote-NVMe-oF-L2 alternative, which requires both a WAL or copy-on-write
generation commit protocol AND a payload-side digest recomputed on read
before it can claim durable cache correctness across restart or
reconnect. See ``docs/design/v1/distributed/l2_adapters/raw_block.md``
and ``docs/design/v1/platform/ipu-poc/nvmeof-initiator-only-alternative.md``.

**Configuration examples:**

.. code-block:: bash

    # Basic raw_block with posix I/O
    --l2-adapter '{"type": "raw_block", "device_path": "/dev/nvme0n1", "slot_bytes": 1048576, "block_align": 4096, "header_bytes": 4096, "meta_total_bytes": 268435456, "use_odirect": true, "num_store_workers": 2, "num_lookup_workers": 1, "num_load_workers": 4}'

    # With io_uring
    --l2-adapter '{"type": "raw_block", "device_path": "/dev/nvme0n1", "slot_bytes": 1048576, "io_engine": "io_uring", "iouring_queue_depth": 256, "use_odirect": true}'

    # With io_uring_cmd (NVMe passthrough)
    --l2-adapter '{"type": "raw_block", "device_path": "/dev/ng0n1", "slot_bytes": 1048576, "io_engine": "io_uring", "use_uring_cmd": true, "iouring_queue_depth": 256, "max_data_transfer_size": 131072, "use_odirect": false}'

    # With eviction
    --l2-adapter '{"type": "raw_block", "device_path": "/dev/nvme0n1", "slot_bytes": 1048576, "load_checkpoint_on_init": false, "eviction": {"eviction_policy": "LRU", "trigger_watermark": 0.9, "eviction_ratio": 0.1}}'
