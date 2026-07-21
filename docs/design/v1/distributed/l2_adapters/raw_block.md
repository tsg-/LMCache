# Raw Block L2 Adapter Design

This document describes the built-in `raw_block` L2 adapter for LMCache MP
mode. It covers the adapter shape, the shared raw-block core, and the recovery
model.

## Overview

`raw_block` is a persistent MP L2 adapter backed by a raw block device or a
dedicated file. It is designed to keep the MP request flow unchanged while
reusing the existing raw-block on-device metadata format and the low-level Rust
raw-device I/O path.

```text
StoreController / PrefetchController
                |
                v
        RawBlockL2Adapter
                |
                v
           RawBlockCore
      (index, locks, slots, checkpoints)
                |
                v
         lmcache_rust_raw_block_io
      (pwrite_from_buffer / pread_into)
                |
                v
         raw block device / file
```

## Goals

- Support LMCache MP mode using raw block storage as an L2 cache.
- Reuse the same durable metadata and checkpoint model as the existing
  non-MP raw-block backend.
- Reuse the existing Rust raw-device I/O layer.
- Preserve restart recovery semantics.
- Keep the MP controller flow unchanged: store, lookup-and-lock, load, unlock.

## TODO

- FDP / placement-hint support.
- A raw NVMe command path.

## Key Design Choice

The implementation is split into:

- `RawBlockCore` in `lmcache/v1/storage_backend/raw_block/`
- `RawBlockL2Adapter` in `lmcache/v1/distributed/l2_adapters/`
- `RustRawBlockBackend` as the legacy non-MP wrapper

`RawBlockCore` owns the durable state and blocking I/O:

- raw device open/close
- in-memory key index
- free-slot tracking
- lock refcounts used by MP lookup/load/unlock
- metadata checkpointing and recovery
- direct reads and writes through the Rust binding

This avoids maintaining separate raw-block implementations for MP and non-MP
mode.

## Adapter Contract

`RawBlockL2Adapter` implements `L2AdapterInterface` directly. It exposes:

- three distinct eventfds: store, lookup, load
- non-blocking task submission APIs
- worker-thread execution for blocking raw-device operations
- result maps keyed by adapter-local task id
- listener notifications for stored, accessed, and deleted keys

The adapter uses caller-provided `MemoryObj` buffers for load operations. It
does not allocate destination buffers on the load path.

## Locking Model

LMCache MP already uses L1 locks for CPU-memory object lifetime. `raw_block`
adds a separate L2-side lock refcount so a looked-up key cannot be deleted
between `lookup_and_lock` and `load`.

Rules:

- `exists_many(..., lock=True)` increments the refcount for hits
- `unlock_many(keys)` decrements and floors at zero
- `delete(keys)` skips locked entries

## Persistence and Recovery

`RawBlockCore` keeps the existing metadata checkpoint model:

- metadata region reserved on the same device
- periodic checkpointing
- optional checkpoint load on startup
- optional verification on load
- recovery by loading the latest durable checkpoint and rebuilding the in-memory
  index

The on-device format is intentionally unchanged by the MP adapter work.

Recovered keys are exposed to the shared L2 eviction policy on adapter startup,
so reclaimed slots come from global L2 eviction or explicit `delete()` calls.

### Non-guarantee: this is not a durable cache-commit protocol

The current model is a periodic snapshot of the in-memory index, not a
transactional commit protocol. It does **not** provide any of:

- data checksum stored with the payload
- durable ordering between payload write and index publish (no `fsync`,
  `fdatasync`, `FLUSH`, or FUA is issued on the write path)
- atomic (data, checksum, key→LBA map) visibility — the three are
  independent I/Os written to independent regions
- a durable intent log (WAL) or copy-on-write generation flip that would
  make map publication atomic with data persistence
- torn-write / stale-map cleanup after a crash between payload write and
  the next checkpoint

Concretely, `RawBlockCore` publishes an entry into the in-memory index
immediately after writing the slot header and payload
(`lmcache/v1/storage_backend/raw_block/core.py:614` and `:1350`), and the
only durable metadata is the periodic mirrored checkpoint at
`lmcache/v1/storage_backend/raw_block/core.py:1627`. A crash between
payload write and the next checkpoint can expose (a) an unrecoverable
payload whose index entry is lost, or (b) after checkpoint restore, an
index entry whose payload was torn or never fully landed.

This is acceptable for the current storage-owned deployment where the
target-side LMCache agent re-verifies BLAKE3 on read and the storage node
is the durable authority. It is **not** acceptable for the initiator-owned
+ remote-NVMe-oF-L2 alternative (see
`docs/design/v1/platform/ipu-poc/nvmeof-initiator-only-alternative.md`),
which requires a WAL or COW-generation commit protocol before it can claim
durable cache correctness across restart or reconnect.

### Deployment modes

`RawBlockCore` today assumes the device path resolves to a locally-attached
NVMe namespace on the same host as the adapter (server-owned). A second
deployment mode — the device path resolves to a **remote namespace**
attached via `nvme connect -t rdma` from an `nvmet-rdma` target on a
different host — is under evaluation on the alt track. Additional
constraints apply in that mode:

- Use `/dev/disk/by-id/nvme-...` (or namespace WWN) paths rather than
  `/dev/nvmeXn1`, which is not stable across `nvme disconnect` /
  `nvme connect` cycles or reboots.
- Reconnect behavior (`ctrl-loss-tmo`, `reconnect_delay`) must be tuned so
  the adapter surfaces disconnect as an I/O error rather than hanging
  indefinitely.
- `use_uring_cmd=true` (NVMe char-device passthrough via `io_uring_cmd`)
  may not be portable to a remote namespace; validate on a specific
  kernel / `nvme-fabrics` version before enabling.
- The atomicity gap above is worse in this mode because the target-side
  device page cache and the `nvmet-rdma` completion do not imply the
  payload has reached durable media without an explicit `FLUSH` or FUA.

## Configuration

The MP adapter is configured through `--l2-adapter` JSON:

```json
{
  "type": "raw_block",
  "device_path": "/dev/nvme0n1",
  "slot_bytes": 1048576,
  "capacity_bytes": 0,
  "use_odirect": true,
  "block_align": 4096,
  "header_bytes": 4096,
  "meta_total_bytes": 268435456,
  "meta_magic": "LMCIDX01",
  "meta_version": 1,
  "meta_checkpoint_interval_sec": 60,
  "meta_enable_periodic": true,
  "load_checkpoint_on_init": true,
  "meta_verify_on_load": true,
  "num_store_workers": 2,
  "num_lookup_workers": 1,
  "num_load_workers": 4
}
```

Important validation rules:

- `block_align` must be a power of two
- `slot_bytes`, `header_bytes`, and `meta_total_bytes` must be aligned to
  `block_align`
- with `use_uring_cmd=true`, `block_align` must be a multiple of the NVMe
  namespace LBA size
- `slot_bytes >= header_bytes + 1`
- `per_tp_device_paths` is rejected in MP mode
- `load_checkpoint_on_init=false` starts with an empty in-memory index instead
  of loading the latest on-device metadata checkpoint
- with `use_odirect=true`, MP L1 alignment must satisfy
  `l1_align_bytes >= block_align`
- with `use_odirect=true`, raw-block I/O rejects offsets and total I/O lengths
  that are not aligned to `block_align`; misaligned write buffers use an
  aligned bounce buffer

## Relationship to Non-MP Mode

The legacy `RustRawBlockBackend` now acts as a thin facade over `RawBlockCore`.
It preserves non-MP behavior such as prefix-oriented contains/get semantics,
while the MP adapter uses the core's full-bitmap lookup/load API.

## References

- Implementation: `lmcache/v1/distributed/l2_adapters/raw_block_l2_adapter.py`
- Shared core: `lmcache/v1/storage_backend/raw_block/core.py`
- User docs: `docs/source/mp/l2_storage/raw_block.rst`
- Rust device layer: `rust/raw_block/README.md`
