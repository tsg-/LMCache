# io_uring reads in the native filesystem connector

Status: proposed design, not implemented. Builds on
[fs_native_read_depth.md](fs_native_read_depth.md), which introduced the read
pool this design replaces as the default read engine.

## Decision

`fs_native` gains a second read engine. Each connector worker owns an
io_uring and pipelines the reads of one batch through it, so a read no longer
crosses threads or takes a connector-wide lock per object. The read pool
stays as the fallback engine. Stores, deletes, sharding, in-shard recycling
and the store/delete lanes do not change.

io_uring is the default read engine when the extension was built with
liburing and the kernel accepts a ring at connector start. Otherwise the
connector uses the read pool and logs which engine it chose.

## The problem

The read pool runs each read as a blocking `do_single_get` (`::read` in a
loop) on one of
`num_read_workers` threads, and every read passes through one queue guarded by
`read_mu_`. The submitting worker holds the lock across its whole enqueue loop
for a batch, and each reader takes it once per object to record completion and
pop its next task. At 64 KiB objects, about 370,000 objects per second per
host, that is roughly 0.4 million reader acquisitions per second plus the
submitters' hold time, and adding readers adds contention instead of depth.

Measured on the MMG-400 rig, 2026-11-03: six NVMe-oF initiators, each with an
eight-namespace RAID0 over RDMA, Mixtral 8x22B FP8 at 64 KiB objects (56 per
submit), read-only, `bench l2`, one process per host, IF 16 unless noted,
300 s per cell. Both engines ran `num_workers` 64, `num_shard_dirs` 256,
O_DIRECT, recycling on, `store_workers` 64, `delete_workers` 16. Same-day fio
64 KiB random read on the same files (32 jobs x QD 32 per host): 1,301 Gb/s.

| read engine | depth setting | fleet Gb/s | of fio | cores per host |
|---|---|---|---|---|
| read pool | 128 readers | 1,105 | 85% | 4.9 |
| read pool | 192 readers | 1,117 | 86% | 5.3 |
| read pool | 256 readers (default) | 1,165 | 90% | 6.6 |
| read pool | 512 readers | 951 | 73% | 13 |
| read pool | 1,024 readers, IF 32 | 878 | 67% | 12 |
| io_uring PoC (`scripts/ipu-poc/fs_uring`) | `io_depth` 16 | 1,123 | 86% | 4.5 |
| io_uring PoC | `io_depth` 64 | **1,273** | **98%** | **4.6** |

The PoC shares this connector's sharding and Python adapter, and its key
parser accepts the four- and five-field keys this benchmark used, so the
difference is the read path. It rejects six-field tagged keys, which
`keys.h` accepts; the in-tree engine reuses fs_native's own `path_for_key`
helpers rather than the PoC's parser. At 256 KiB objects the two engines were
within 0.5% (1,301.8 vs 1,295.2 Gb/s, 5:1, 2026-11-01), which is why the PoC
stayed out of tree; the gap appears when objects are small enough for
per-object overhead to dominate.

The pool lock is the leading explanation for the 512/1,024-reader collapse
(CPU doubles while throughput falls, and kernel stacks show heavy futex
waits). It has not been confirmed with a lock-contention profiler.

## How the engine works

`choose_num_tiles` returns 1 for gets, as the read pool path already does, so
a whole batch goes to one worker. That worker:

1. Opens up to `io_depth` files of its batch on its own thread, using the
   shard-aware path and the same open-flag rules the read pool uses today
   (`try_enable_odirect`): O_DIRECT when the buffer address and length are
   block-aligned; buffered whenever the length is not block-aligned, whatever
   the address; and a failed key when the length is aligned but the address
   is not.
2. Queues a read SQE per open file and submits them together.
3. Waits for completions, re-submits short reads at the new offset and reads
   that complete with `-EINTR` (today's `read_all` retries `EINTR`), closes
   each file when its read finishes, and refills the ring from the remaining
   files until the batch is done.

Depth against the device is therefore

    concurrent batches x min(objects per batch, io_depth)

bounded above by `num_workers x io_depth`. At IF 16 with 56-object batches
that is about 900 reads per host at `io_depth` 64 and 256 at `io_depth` 16,
which matches the 1,273 vs 1,123 Gb/s rows above.

### The byte budget stays

The read pool bounds reads in flight by bytes (`read_max_bytes_in_flight`,
default 1,536 MiB) because a count means something different at every
`chunk_size`; see the read-depth doc. A per-worker `io_depth` alone would let
64 workers keep 24 GiB in flight at 6 MiB objects. The PoC has no byte budget;
this part is new, not ported.

The engine keeps today's admission rule and today's single mutex for it, but
takes the mutex per group of reads rather than per read:

- State under the budget mutex: bytes in flight, reads in flight, and a
  condition variable. Admission, accounting and waiting all happen under it,
  as in `read_budget_admits` today, so there is no separate-counter race and
  no lost wakeup.
- A worker about to fill its ring reserves for the next group of reads (up to
  its free ring slots) in one acquisition. A read is admitted if fewer than
  `kMinReadsInFlight` (2) reads are in flight connector-wide or its bytes fit;
  the worker reserves as many reads from the front of its batch as are
  admitted and stops at the first that is not.
- Released bytes are returned once per reaped group of completions, with one
  acquisition and a `notify_all`.
- A worker that could not reserve first submits any prepared SQEs, then reaps
  completions if it has any outstanding. Only a worker with nothing
  outstanding waits on the condition variable, so a waiting worker never
  holds prepared but unsubmitted requests.
- If preparation fails after a reservation (for example an open error), that
  read's reservation is returned with the next release.

Synchronous fallback reads (below) reserve and release through the same
mutex, so the budget covers every read.

At 64 KiB the budget never binds (about 900 reads x 64 KiB is 56 MiB against
1,536 MiB), so the headline benchmark cannot show its cost. The PR therefore
also benchmarks with the budget forced small (for example 64 MiB) and reports
mutex acquisitions per read; the target is well under one, against two
lock-protected handoffs per read in the pool.

### File descriptors

Every read in flight holds an open file. The engine allows up to
`num_workers x io_depth` open read files (4,096 at the defaults) on top of the
store lanes and the process's own descriptors. At construction the connector
reads `RLIMIT_NOFILE`. If `num_workers x io_depth` plus a margin of 1,024
exceeds the soft limit, `auto` lowers the effective `io_depth` to fit and logs
it, and an explicit `io_uring` setting fails construction with the numbers.
An `EMFILE`/`ENFILE` at open time stops refilling that worker's ring until a
read completes rather than failing the key, and fails the key only if the
worker has nothing outstanding.

### Recycling and slow reads

Recycling reuses a deleted object's inode after a fixed one-second quarantine
(`connector.cpp:109`, `:165`). A read that opened the file before the delete
and is still in flight after the quarantine would read the new object's bytes.
The risk exists with the read pool today. The io_uring engine can make it more
likely, because a read can wait longer between open and completion when its
worker's ring is full. So the engine adds a guard that also covers the pool:

- A read registers itself before it opens its file: under the budget mutex,
  in the same acquisition that reserves its bytes, it records a monotonic
  timestamp in an ordered set of in-flight read start times. It unregisters
  when it releases its bytes. Synchronous fallback reads and read-pool reads
  register the same way.
- Parking a deleted file records the monotonic time at which it was renamed
  into the pool.
- The store path, before reusing a parked file, takes the budget mutex and
  reads the oldest registered start time. It reuses the file only if it was
  parked before that time (and the quarantine has passed); otherwise it
  creates a new file, as when the pool is empty.

This guard covers readers within one connector. Recycling also adopts every
parked `.pool-*` file in `base_path` when a connector starts
(`connector.cpp:250`), so a second connector sharing the directory could reuse
an inode the first is still reading. That exposure exists today and is not
introduced by io_uring, but this design bounds it:

- A connector with `recycle_max_files > 0` takes an exclusive, non-blocking
  `flock` on `base_path/.lmcache-recycle.lock` at construction and holds it
  for its lifetime. If another connector holds it, the new connector runs with
  recycling disabled (deletes unlink, stores create new files) and logs a
  warning naming the directory. It does not fail.
- A connector without recycling that shares `base_path` with a recycling one
  is not protected and is documented as unsupported. Coordinating readers
  across connectors (a shared registry) is a follow-up if that setup is
  needed.

Why this is enough within one connector: a read that could have opened the old inode registered
before its `open()`. If it opened before the park, its start time is earlier
than the park time, so it blocks reuse until it unregisters. If it opened
after the park, the name no longer resolved to the old inode, and the open
failed as a miss. The cost is one mutex acquisition per reuse, on the store
path, which is not where the engine's lock pressure is.

A test delays a read past the quarantine while the same key is deleted and its
inode is offered for reuse, and checks the read returns the old bytes or fails
but never returns the new object's.

### Large reads

The PoC rejects reads longer than `INT_MAX`. Today's `read_all` handles any
`size_t` length. The engine sends reads longer than 1 GiB through the
synchronous fallback, so the large-read behaviour is unchanged and the ring
only ever carries lengths that fit an SQE.

### Errors and shutdown

The kernel owns a buffer from submission until its CQE is reaped. So:

Each read is in one of four states: *queued* (in the batch, nothing
allocated), *prepared* (file open, budget reserved, SQE in the ring but not
yet accepted by the kernel), *accepted* (counted from `io_uring_submit`'s
return value, the kernel owns the buffer) or *done* (CQE reaped, file closed,
budget released).

- `io_uring_submit` can accept fewer SQEs than were prepared. The engine adds
  the return value to its accepted count and resubmits the rest; it never
  infers acceptance from the number it prepared.
- On any engine error (a negative submit return, a CQE for an unknown request,
  a failed wait), the worker reaps CQEs until its accepted count reaches zero,
  then closes the files of prepared-but-unaccepted reads, releases all their
  budget, fails the batch's unfinished keys, tears down and recreates the ring
  (`io_uring_queue_exit` discards SQEs the kernel never saw), and continues.
  If the new ring cannot be created, that worker fails later batches; other
  workers continue.
- If reaping itself cannot make progress (the wait keeps failing for 10 s of
  retries), the worker cannot prove the kernel is done with the buffers. The
  buffers belong to the caller's L1 memory, so neither reporting the batch nor
  letting `close()` return is safe: either hands back memory the kernel may
  still write. The connector therefore logs the ring state and aborts the
  process. My call: a crash with a clear message is preferable to silent
  corruption of cached KV data, and this path needs a broken kernel or ring to
  reach. Reviewers may prefer another policy; whatever it is, it must not
  return the buffers while they are accepted.
- A per-read failure (open error, `res <= 0`, short read past the length, an
  O_DIRECT offset that is not block-aligned) fails only that key, as today.
- `close()` lets each worker finish or fail its current batch, reaping all
  accepted requests, before the rings are torn down. With the abort above,
  every worker either reaps its accepted requests or the process ends, so
  `close()` never returns while the kernel owns a buffer.

### Ring setup

Rings are created synchronously in the connector's constructor, one per
shared-pool worker, before any worker thread starts, as the PoC does.
`ConnectorBase` builds connections inside worker threads and only logs a
failure there (`connector_base.h:540`), so relying on it would let
construction succeed with missing workers. If any ring fails to initialize,
the constructor destroys the rings already created and either falls back to
the pool (`auto`) or throws (`io_uring`). Each worker thread is handed its ring
by index; store and delete lane threads never get one.

These rules are taken from the PoC, which implements them. They are the main
correctness risk, so each gets its own test (below).

## Configuration

| field | values | default | meaning |
|---|---|---|---|
| `read_engine` | `auto`, `io_uring`, `pool` | `auto` | `auto` picks io_uring when available, else the pool. `io_uring` fails connector construction if it is unavailable. `pool` forces today's path. |
| `io_depth` | 1 to 4096 | 64 | Reads in flight per worker ring. Ignored by the pool. |
| `read_max_bytes_in_flight` | bytes | 1,536 MiB | Unchanged; now applies to both engines. |
| `num_read_workers` | unchanged | 256 | Sizes the read pool. With io_uring the pool is not started and no read connections are created. |

The byte budget applies whenever the io_uring engine is selected, whatever
`num_read_workers` is: `read_max_bytes_in_flight` 0 means the 1,536 MiB
default under io_uring. Under the pool, `num_read_workers` 0 keeps today's
legacy path with no budget. If `auto` falls back to the pool at runtime, the
pool starts with the configured `num_read_workers`.

Rings exist only on the threads that run gets: the shared worker pool
(`num_workers`). The dedicated store and delete lanes get none. Each ring has
`io_depth` submission entries and the liburing default completion queue (twice
that).

`num_workers` keeps its meaning. With io_uring it also sets how many batches
can read concurrently, so the docs should say that a deployment issuing many
concurrent loads wants `num_workers` at least equal to its submit
concurrency.

The engine actually in use is exposed on the client (a read-only property or
a stats entry) so tests and benchmarks can assert it.

## Build and packaging

My call: link liburing statically into `lmcache_fs` when the build finds it,
and compile the io_uring engine out otherwise. A build without liburing
behaves exactly as today.

- Version: liburing 2.2 or newer (the PoC uses `io_uring_sqe_set_data64`).
- Source: the shared object needs a position-independent archive, and distro
  `liburing-devel` packages and manylinux images do not reliably ship one. The
  wheel and image builds therefore compile liburing from a pinned release with
  `-fPIC` in a build step, and link that archive. Local builds may instead
  point `LMCACHE_LIBURING_DIR` at an install; detection checks for the header,
  a PIC archive and version 2.2 or newer, and logs which it used or why it
  rejected a candidate. Linking is opt-in in PR A1: `setup.py` searches only
  when `LMCACHE_LIBURING_DIR` or `LMCACHE_WITH_LIBURING=1` (system
  directories) is set, so a build host that happens to have a PIC archive
  does not link it by accident.
- Runtime: no new dependency in wheels or images.
- Licence: liburing is dual-licensed, MIT among them. Until LMCache
  maintainers confirm static linking under MIT is acceptable, PR 1 builds the
  engine in source and local builds only, and published wheels keep it
  compiled out. Nothing else in PR 1 depends on that answer.

A ring that fails `io_uring_queue_init` at runtime (old kernel, container
seccomp profile blocking io_uring, `io_uring_disabled` sysctl) makes `auto`
fall back to the pool with one warning, not a failure.

## Testing

Unit tests run on Linux with O_DIRECT where available and skip otherwise,
like the existing connector tests. Each runs against both engines:

- round-trip correctness across `io_depth` 1, 2 and 64, batches smaller and
  larger than `io_depth`, and mixed object sizes;
- short reads and `-EINTR`: new fault hooks make a read CQE return fewer bytes
  than asked, or `-EINTR`, and the engine resubmits from the right offset;
- partial submission: a hook makes `io_uring_submit` accept fewer SQEs than
  prepared, both on its own and followed by an engine error;
- unrecoverable reaping: a hook makes the wait fail persistently, and a death
  test checks the process aborts with the logged message rather than
  returning the batch;
- descriptor exhaustion: with a low `RLIMIT_NOFILE`, `auto` lowers the depth,
  a forced `io_uring` fails construction, and `EMFILE` at open time throttles
  instead of failing keys;
- recycling overlap: the delayed-read test under "Recycling and slow reads",
  run against both engines and the synchronous fallback;
- recycling ownership: a second connector on the same `base_path` with
  recycling enabled starts with recycling disabled, logs it, and never adopts
  the first connector's parked files;
- large reads: an object longer than 1 GiB reads correctly through the
  fallback, and a read larger than the whole budget is admitted under the
  two-read exception while ordinary reads still respect the budget;
- alignment: address and length both misaligned reads buffered, as today;
- byte budget: with a small budget, peak bytes in flight never exceed it, a
  single oversized read still completes, and many concurrent batches neither
  deadlock nor starve;
- engine error: an injected `io_uring_submit` failure mid-batch reaps all
  outstanding requests before the batch reports, and later batches on that
  worker fail cleanly while other workers succeed;
- `close()` with reads in flight returns without use-after-free (run under
  ASan in CI if available);
- engine selection: `auto` without a usable ring picks the pool and logs it;
  forcing `io_uring` without one fails construction.

The PoC's suite (`scripts/ipu-poc/fs_uring/tests`) covers engine errors
through a forced-include fault API that wraps `io_uring_queue_init`,
`io_uring_submit` and `io_uring_wait_cqe`. It has no hook for short reads or
the budget, so those need new hooks in the same style.

Benchmarks to report in the PR, on the same rig and same day for both
engines and fio, with the setup stated as in the table above: Mixtral 64 KiB
and 256 KiB, read-only and 5:1; a 6 MiB object run to show the byte budget
still protects large objects; and the small-budget run described above.

Acceptance for flipping the default (PR 2): 64 KiB read-only within 3 points
of the PoC's share of same-day fio (that is, at least 95%), and no case slower
than the pool by more than 2%. PR 1 merges on correctness and tests alone,
since it changes no default.

## Rollout

1. PR 1: the io_uring engine behind `read_engine`, default `pool`, with the
   tests and build detection. Reviewable without changing any default.
2. PR 2: flip the default to `auto` once PR 1's benchmarks are in, and update
   `docs/source/mp/l2_storage/fs_native.rst`.

## Non-goals

- No change to on-disk layout, key format or sharding. Recycling changes only
  by the reuse guard above.
- No io_uring for stores or deletes. The store path's limit in mixed loads is
  being measured separately and may need different work.
- No asynchronous `openat`/`close` through the ring in this version. Opening
  56 files serially per batch is a known cost that a later `IORING_OP_OPENAT`
  change can remove; measure it before adding it.
- `scripts/ipu-poc/fs_uring` is not deleted by this work; it stays a PoC until
  this lands.

## Questions for review

- Is holding a batch on one worker right for large batches, or should a batch
  larger than `io_depth` be split across workers?
- Should `auto` stay on the pool for object sizes where the engines measured
  equal, to avoid changing behaviour without a gain?
- Is static linking of liburing acceptable for the published wheels?
- Is aborting the process on unrecoverable ring reaping the right policy?
- Is falling back to no recycling the right response to a second recycling
  connector on the same directory, or should construction fail?
