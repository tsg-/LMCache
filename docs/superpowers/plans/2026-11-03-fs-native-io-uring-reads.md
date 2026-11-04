# fs_native io_uring Reads Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-worker io_uring read engine to the `fs_native` connector (opt-in in PR A1, default in PR A2), keeping the byte budget, store path and recycling semantics.

**Architecture:** The engine lives in new files beside `csrc/storage_backends/fs/connector.cpp` and is compiled only when liburing is found. `FSConnector` owns one ring per shared-pool worker, created in its constructor before `start_workers()`. A budget object shared by both engines replaces the pool's inline budget logic and also carries the in-flight reader registry the recycling guard needs.

**Tech Stack:** C++17, liburing >= 2.2 (static, PIC), pybind11, pytest.

**Spec:** `docs/design/v1/distributed/l2_adapters/fs_native_io_uring_reads.md`

## Global Constraints

- `read_engine`: `auto` | `io_uring` | `pool`; default `pool` in PR A1, `auto` in PR A2.
- `io_depth`: integer in [1, 4096], default 64; ignored by the pool.
- `read_max_bytes_in_flight` default 1,536 MiB (`kDefaultReadMaxBytesInFlight`); applies to every io_uring read; 0 means the default under io_uring.
- Admission rule identical to `read_budget_admits`: admitted while reads in flight < `kMinReadsInFlight` (2) or bytes fit.
- Rings only on shared-pool workers (`num_workers`); never on store/delete lanes. SQ entries = `io_depth`, CQ = liburing default.
- Reads > 1 GiB and buffered reads with `read_ahead_size > 0` use the synchronous `do_single_get` path, budgeted.
- Open flags follow `try_enable_odirect` exactly; `-EINTR` CQEs are resubmitted.
- `RLIMIT_NOFILE` check: if `num_workers * io_depth + 1024 > soft limit`, `auto` lowers `io_depth` (logged), `io_uring` throws.
- Unrecoverable reaping: 10 s of failed waits, then log ring state and `std::abort()`.
- Recycling lock file: `base_path/.lmcache-recycle.lock`, exclusive non-blocking `flock`; if held elsewhere, recycling is disabled with a warning.
- liburing: built from a pinned release with `-fPIC`; `LMCACHE_LIBURING_DIR` overrides; published wheels keep the engine compiled out in PR A1.
- No change to on-disk layout, key format, sharding.
- Commits: Conventional Commits, `git commit -s`, no Co-Authored-By.

## Review Focus

- A batch whose keys mix O_DIRECT-eligible, buffered and > 1 GiB reads must return every key correctly; covered in Task 3 (`test_mixed_path_batch_round_trip`).
- Many concurrent batches with a budget smaller than one batch's bytes must all finish (no deadlock, no starvation); Task 3 (`test_budget_many_batches_progress`).
- `close()` while batches are mid-flight on every worker must return with no reads lost or doubled; Task 4 (`test_close_with_reads_in_flight`).
- A missing file inside an otherwise valid batch fails only that key, with the ring still usable for the next batch; Task 3 (`test_missing_key_fails_only_that_key`).
- Two connectors on one `base_path`, both with recycling, must not both recycle; Task 5 (`test_second_recycler_runs_without_recycling`).

## File Structure

- Create `csrc/storage_backends/fs/read_budget.h` / `.cpp`: `ReadBudget` (mutex, condvar, bytes/reads in flight, reader start-time registry). Used by the pool and the ring engine.
- Create `csrc/storage_backends/fs/uring_reader.h` / `.cpp`: `UringReader`, one ring and its batch loop; only compiled with `LMCACHE_HAVE_LIBURING`.
- Modify `csrc/storage_backends/fs/connector.h` / `.cpp`: engine selection, ring setup, recycling guard, flock, routing in `do_batch_get`.
- Modify `csrc/storage_backends/fs/pybind.cpp`: two new constructor args, `read_engine()` getter.
- Modify `setup_extensions/common_cpp.py`: liburing detection and static link for `lmcache_fs`.
- Modify `lmcache/v1/distributed/l2_adapters/fs_native_l2_adapter.py`: config fields, pass-through, log line.
- Modify `docs/source/mp/l2_storage/fs_native.rst`: new fields (PR A1), default change (PR A2).
- Create `scripts/ipu-poc/build_lmcache_fs.sh` (internal): rig build of `lmcache_fs` into an LMCache tree.
- Tests: `tests/inprocess/v1/storage_backend/test_fs_native_connector.py` (both engines, parametrized), `tests/inprocess/v1/storage_backend/test_fs_native_uring_faults.py` (fault-injection build only), `tests/v1/distributed/test_fs_native_l2_adapter_config.py`.

Test runs that exercise io_uring happen on mmgi1 (XFS + O_DIRECT, liburing 2.12), as the existing O_DIRECT tests did: `cd ~/tsg-build/LMCache && bash scripts/ipu-poc/build_lmcache_fs.sh . && .venv-bench-l2/bin/python -m pytest <tests> -q`. On macOS they skip.

---

### Task 1: Rig build script and liburing detection

**Files:**
- Create: `scripts/ipu-poc/build_lmcache_fs.sh`
- Modify: `setup_extensions/common_cpp.py:50-66`
- Modify: `csrc/storage_backends/fs/pybind.cpp`

**Interfaces:**
- Produces: compile define `LMCACHE_HAVE_LIBURING` (1 when linked); module function `lmcache_fs.has_io_uring() -> bool`; optional define `LMCACHE_FS_FAULT_INJECTION`.

- [ ] **Step 1: Write the failing test** in `tests/inprocess/v1/storage_backend/test_fs_native_connector.py`

```python
def test_module_reports_io_uring_support():
    from lmcache_fs import has_io_uring
    assert isinstance(has_io_uring(), bool)
    if sys.platform == "linux" and os.environ.get("LMCACHE_EXPECT_IO_URING") == "1":
        assert has_io_uring()
```

- [ ] **Step 2: Run on mmgi1, expect FAIL** (`ImportError: cannot import name 'has_io_uring'`).

- [ ] **Step 3: Implement**
  - `common_cpp.py`: for `lmcache_fs` only, look for `liburing.h` and a PIC `liburing.a` under `$LMCACHE_LIBURING_DIR` then the system include/lib dirs. PIC check: `readelf -r liburing.a` shows no `R_X86_64_32` relocations, or a link probe of a one-line `-shared` object succeeds. If found, add the archive as an extra object, add `-DLMCACHE_HAVE_LIBURING=1`, and log `lmcache_fs: io_uring engine enabled (<path>)`; otherwise log that it is compiled out. `LMCACHE_FS_FAULT_INJECTION=1` in the environment adds that define.
  - `build_lmcache_fs.sh <tree>`: builds pinned liburing 2.12 with `CFLAGS=-fPIC` into `<tree>/build/liburing` if absent, then `c++ -O3 -std=c++17 -shared -fPIC $(python -m pybind11 --includes) -I csrc/storage_backends -I csrc/storage_backends/fs <sources> <archive> -DLMCACHE_HAVE_LIBURING=1 -o <tree>/lmcache/lmcache_fs$(python3-config --extension-suffix)`, using `.venv-bench-l2/bin/python`.
  - `pybind.cpp`: `m.def("has_io_uring", ...)` returning `LMCACHE_HAVE_LIBURING` presence.

- [ ] **Step 4: Run on mmgi1 with `LMCACHE_EXPECT_IO_URING=1`, expect PASS; on macOS, expect PASS (False).**

- [ ] **Step 5: Commit** `build: detect and statically link liburing for lmcache_fs` (the build script goes in a separate internal commit `chore: add rig build script for lmcache_fs`).

### Task 2: Shared `ReadBudget`, used by the read pool

**Files:**
- Create: `csrc/storage_backends/fs/read_budget.h`, `csrc/storage_backends/fs/read_budget.cpp`
- Modify: `csrc/storage_backends/fs/connector.h:30-45,~150-200`, `csrc/storage_backends/fs/connector.cpp:455-500`
- Test: `tests/inprocess/v1/storage_backend/test_fs_native_connector.py`

**Interfaces:**
- Produces:
  - `class ReadBudget { explicit ReadBudget(size_t max_bytes); size_t reserve(const size_t* lens, size_t n, bool block, uint64_t* start_ns); void release(size_t bytes, size_t reads, const uint64_t* start_ns, size_t n); uint64_t oldest_start_ns() const; size_t max_bytes() const; size_t peak_bytes() const; }`
  - `reserve` admits a prefix of `lens` by the `read_budget_admits` rule, records one monotonic start time per admitted read, returns the count admitted (blocks for at least one only when `block` is true), under one mutex acquisition.
  - `release` returns bytes/reads, removes the start times, and `notify_all`s.
  - `oldest_start_ns()` returns `UINT64_MAX` when nothing is in flight.
  - `FSConnector::read_peak_bytes() const` and pybind `read_peak_bytes()` (test observability).

- [ ] **Step 1: Write failing tests**

```python
def test_pool_peak_bytes_never_exceed_small_budget(tmp_path):
    client = LMCacheFSClient(str(tmp_path), 4, "", False, 0, 64, 1 << 20)
    # 32 objects of 256 KiB, read as 4 concurrent batches
    ...
    assert client.read_peak_bytes() <= (1 << 20)

def test_oversized_read_admitted_under_two_read_rule(tmp_path):
    client = LMCacheFSClient(str(tmp_path), 2, "", False, 0, 8, 64 << 10)
    # one 1 MiB object, budget 64 KiB: the read completes
    ...
```

- [ ] **Step 2: Run, expect FAIL** (`AttributeError: read_peak_bytes`).

- [ ] **Step 3: Implement `ReadBudget` and switch the pool's `do_batch_get_pooled` and readers to it**, keeping `kReadBudgetWakeSlices` wake behaviour. No change to observable pool results.

- [ ] **Step 4: Run the whole `test_fs_native_connector.py` on mmgi1 and macOS, expect PASS** (existing tests unchanged).

- [ ] **Step 5: Commit** `refactor: move the fs_native read budget into a shared ReadBudget`.

### Task 3: `UringReader` and engine selection in `FSConnector`

**Files:**
- Create: `csrc/storage_backends/fs/uring_reader.h`, `csrc/storage_backends/fs/uring_reader.cpp`
- Modify: `csrc/storage_backends/fs/connector.h`, `csrc/storage_backends/fs/connector.cpp:224-300,442-456`, `csrc/storage_backends/fs/pybind.cpp`
- Modify: `lmcache/v1/distributed/l2_adapters/fs_native_l2_adapter.py:96-310`
- Test: `tests/inprocess/v1/storage_backend/test_fs_native_connector.py`, `tests/v1/distributed/test_fs_native_l2_adapter_config.py`

**Interfaces:**
- Consumes: `ReadBudget` (Task 2).
- Produces:
  - `struct ReadItem { std::filesystem::path path; void* buf; size_t len; int open_flags; };`
  - `class UringReader { UringReader(unsigned io_depth, ReadBudget& budget); void read_batch(std::vector<ReadItem>& items, uint8_t* ok, const std::atomic<bool>& stopping); bool usable() const; }`
  - `read_batch` sets `ok[i]` to 1/0; throws only for engine errors after it has reaped every accepted request (state machine *queued/prepared/accepted/done* from the spec).
  - Fault hooks under `LMCACHE_FS_FAULT_INJECTION`: env `LMCACHE_FS_FAULT` = `submit_partial` | `submit_error` | `cqe_short` | `cqe_eintr` | `wait_error_forever` | `queue_init_error`; `LMCACHE_FS_FAULT_DELAY_MS=<n>` delays each io_uring CQE's processing (and each pool `do_single_get` read) by n ms, used by Task 5.

  - Constructor gains trailing `std::string read_engine = "pool", unsigned io_depth = 64`; pybind args `read_engine="pool"`, `io_depth=64`.
  - `std::string FSConnector::read_engine() const` returning the engine in use: `"io_uring"`, `"pool"` or `"legacy"`.
  - `FSNativeL2AdapterConfig.read_engine: str` (validated in {"auto","io_uring","pool"}), `.io_depth: int` (validated 1..4096); `from_dict` keys `read_engine`, `io_depth`.
  - Adapter log line adds `read_engine=<in use>, io_depth=<effective>`.

- [ ] **Step 1: Write failing tests**

```python
@pytest.fixture(params=["pool", "io_uring"])
def engine(request): ...   # skips io_uring unless has_io_uring()

def test_reads_return_identical_bytes(tmp_path, engine): ...   # existing test, parametrized
@pytest.mark.parametrize("io_depth", [1, 2, 64])
def test_uring_round_trip_batch_sizes(tmp_path, io_depth):
    # batches of 1, io_depth, 3*io_depth objects, sizes 4 KiB..1 MiB aligned
def test_budget_many_batches_progress(tmp_path, engine):
    # 16 concurrent 56-object batches of 64 KiB, budget 256 KiB: all complete
def test_mixed_path_batch_round_trip(tmp_path, engine):
    # one batch: aligned O_DIRECT, unaligned-length (buffered), 1.5 GiB object (fallback)
def test_missing_key_fails_only_that_key(tmp_path, engine):
    # result bools: [1, 0, 1]; next batch on the same client all 1
def test_alignment_both_misaligned_reads_buffered(tmp_path, engine): ...
def test_auto_without_ring_falls_back_to_pool(tmp_path, monkeypatch):
    # LMCACHE_FS_FAULT=queue_init_error (fault build) -> read_engine() == "pool"
def test_forced_io_uring_without_ring_raises(tmp_path, monkeypatch): ...
def test_nofile_limit_lowers_depth_or_raises(tmp_path): ...  # resource.setrlimit in a subprocess
```

Config test: `read_engine` "bogus" and `io_depth` 0 / 4097 / "8" raise `ValueError` naming the field.

- [ ] **Step 2: Run on mmgi1, expect FAIL** (unknown argument `read_engine`).

- [ ] **Step 3: Implement `UringReader::read_batch`** following the spec's "How the engine works", "The byte budget stays" and "Errors and shutdown": reservation per group via `ReadBudget::reserve(..., block = outstanding == 0)`; submit prepared SQEs before blocking; resubmit short reads and `-EINTR`; on engine error drain accepted, close prepared, release all, `io_uring_queue_exit` and re-init; abort after 10 s of failed waits.

- [ ] **Step 4: Implement selection in `FSConnector`.** Construction order: validate; create `ReadBudget`; if engine is `auto`/`io_uring` and `has_io_uring`, check `RLIMIT_NOFILE`, create `num_workers` `UringReader`s (all or none); else start the read pool as today; then `start_workers()`. `create_connection()` hands shared-pool worker *i* reader *i*. `choose_num_tiles` returns 1 for gets under either engine. `do_batch_get` builds `ReadItem`s with `path_for_key` and `try_enable_odirect` flags, routes fallback items to `do_single_get` (reserving through `ReadBudget`), the rest to the worker's reader.

- [ ] **Step 5: Run both test files on mmgi1 (engine build) and macOS, expect PASS.**

- [ ] **Step 6: Commit** `feat: add an opt-in io_uring read engine to fs_native`.

### Task 4: Error-path and shutdown tests (fault-injection build)

**Files:**
- Create: `tests/inprocess/v1/storage_backend/test_fs_native_uring_faults.py`

**Interfaces:**
- Consumes: fault hooks (Task 3), `read_engine="io_uring"`.

- [ ] **Step 1: Write the tests** (module-level skip unless the module was built with `LMCACHE_FS_FAULT_INJECTION`; each case runs in a subprocess so env-selected faults stay isolated):
  - `test_partial_submission_completes_batch` (`submit_partial`): all keys 1.
  - `test_submit_error_mid_batch_fails_keys_and_recovers` (`submit_error` once): batch keys 0, next batch on the same client 1, no fd leak (`/proc/self/fd` count back to baseline).
  - `test_short_and_eintr_reads_resubmitted` (`cqe_short`, `cqe_eintr`): data matches.
  - `test_unrecoverable_wait_aborts` (`wait_error_forever`): child exits by `SIGABRT`, stderr contains `io_uring reaping failed`.
  - `test_close_with_reads_in_flight`: 64 batches in flight, `close()` returns, every submitted future completes exactly once.

- [ ] **Step 2: Build on mmgi1 with `LMCACHE_FS_FAULT_INJECTION=1 bash scripts/ipu-poc/build_lmcache_fs.sh .` and run; fix until PASS.**

- [ ] **Step 3: Commit** `test: cover io_uring engine failure paths in fs_native`. Add a CI job note in the PR: the fault build runs this file.

### Task 5: Recycling reader guard and directory ownership

**Files:**
- Modify: `csrc/storage_backends/fs/connector.h` (`RecyclePool` entries gain `parked_ns`), `csrc/storage_backends/fs/connector.cpp:100-200,240-260`
- Test: `tests/inprocess/v1/storage_backend/test_fs_native_connector.py`

**Interfaces:**
- Consumes: `ReadBudget::oldest_start_ns()`.
- Produces: `bool FSConnector::recycling_enabled() const` and pybind `recycling_enabled()`.

- [ ] **Step 1: Write failing tests**
  - `test_delayed_read_never_returns_new_object` (both engines, fault build, `LMCACHE_FS_FAULT_DELAY_MS=1500` for both engines): store K1, start load of K1, delete K1, after 1.2 s store K2 of the same size; the load returns K1's bytes or fails, never K2's.
  - `test_second_recycler_runs_without_recycling`: two clients, same `base_path`, both `recycle_max_files=8`; first `recycling_enabled()` True, second False; second never reuses first's parked files.

- [ ] **Step 2: Run on mmgi1, expect FAIL.**

- [ ] **Step 3: Implement** `flock(LOCK_EX|LOCK_NB)` on `.lmcache-recycle.lock` when `recycle_max_files > 0` (disable recycling and warn if held; skip pool adoption too), record `parked_ns` at park, and in `store_into_recycled` reuse only if `parked_ns < budget.oldest_start_ns()` and quarantine has passed. Pool readers register through `ReadBudget` (Task 2), so the guard covers both engines.

- [ ] **Step 4: Run, expect PASS** (plus the full connector suite).

- [ ] **Step 5: Commit** `fix: never recycle a file a read may still hold in fs_native`. Update `fs_native.rst` recycling section in the same commit.

### Task 6: PR A1 benchmarks and docs

**Files:**
- Modify: `docs/source/mp/l2_storage/fs_native.rst` (new fields, engine selection, liburing build note)

- [ ] **Step 1: Build both engines into `/root/dig5to1/lmc_iouring` on all six initiators with `build_lmcache_fs.sh`.**
- [ ] **Step 2: Run on the rig, same day, settle gate as today's sequences:** fio 64 KiB RO 120 s; Mixtral 64 KiB RO IF 16 and 5:1 IF 64, 256 KiB RO and 5:1, 6 MiB-object RO, and 64 KiB RO with `read_max_bytes_in_flight` 64 MiB, each for `read_engine` `pool` and `io_uring`. Record fleet Gb/s, cores per host, target `dmesg` error count.
- [ ] **Step 3: Write the PR description** with the table, the setup line from the spec, and PR A2's gate result.
- [ ] **Step 4: Commit docs** `docs: document the fs_native io_uring read engine`.

### Task 7: PR A2, flip the default

**Files:**
- Modify: `csrc/storage_backends/fs/connector.h`, `pybind.cpp`, `fs_native_l2_adapter.py` (default `"auto"`), `fs_native.rst`

- [ ] **Step 1: Check the gate from Task 6:** 64 KiB RO >= 95% of same-day fio with `io_uring`, and no row slower than `pool` by more than 2%. Stop here if either fails and report.
- [ ] **Step 2: Change the default to `auto`; update the config test expecting `"auto"`; run the suites; commit** `feat: make io_uring the default fs_native read engine where available`.
