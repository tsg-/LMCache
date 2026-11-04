# Native Connector Issuing-Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the lock convoy and per-object Python work on `NativeConnectorL2Adapter`'s issuing path: PR B1 makes load submits lock-free and builds bitmaps outside the lock; PR B2 adds a raw batch submit to the shared pybind layer.

**Architecture:** PR B1 is Python-only, in `native_connector_l2_adapter.py`: loads call the native submit without `self._lock` and register afterwards, early completions are parked and handed back to the demux thread, timed-out deletes are recorded as abandoned. PR B2 adds `submit_batch_get_raw` / `submit_batch_set_raw` to `LMCACHE_BIND_CONNECTOR_METHODS`; C++ copies inputs under the GIL, builds canonical keys after releasing it, and calls the connector's existing batch methods.

**Tech Stack:** Python 3.12, pybind11, C++17, pytest, py-spy (measurement only).

**Spec:** `docs/design/v1/distributed/l2_adapters/native_connector_raw_batch.md`

## Global Constraints

- Store submits keep holding `self._lock` across the native `submit_batch_set`; store size accounting stays in completion order.
- Listener callbacks (`_notify_keys_*`) run only on the demux thread.
- Key format must be byte-identical to `encode_storage_key`: `{model}@{rank:08x}@{group:x}@{hash hex}` + `@{salt}` if salt + `@{salt}@{token}` if tags (salt may be empty).
- Raw arrays: 1-D, C-contiguous, format `"Q"` or `"=Q"`, itemsize 8; ranks and groups uint64; `hash_len > 0`, `len(chunk_hashes) == hash_len * n` (overflow-checked).
- Raw path eligibility: client has the raw method; all keys share model, salt, tags and a non-zero hash length; every object is a `TensorMemoryObj`; ranks/groups fit uint64. Otherwise the string path.
- Raw methods are public with a trusted-input docstring; the binding rejects `@` in model, salt or token and nothing else beyond the array checks.
- `batch_chunk_num_bytes` = first buffer length, as `bind_submit_batch_get` does.
- No caching of encoded keys across submits.
- PR B1 ships only if single-host and fleet 5:1 rise and issuing-thread lock wait falls.
- Commits: Conventional Commits, `git commit -s`, no Co-Authored-By.

## Review Focus

- A load whose native submit raises after a completion was parked for a previous id must not leave the parked entry behind or mis-route it; Task 3 (`test_submit_raising_leaves_no_parked_state`).
- Many threads submitting loads concurrently (the adapter is called from several controller threads) must each get exactly their own results; Task 3 (`test_concurrent_load_submitters_each_get_own_result`).
- A raw store followed by a string-path load of the same keys (mixed fleet during rollout) must hit; Task 5 (`test_raw_store_string_load_round_trip`).
- A submit containing exactly one key, and one containing 4,096 keys, take the raw path and round-trip; Task 6 (`test_raw_path_single_and_large_submit`).
- A padded `TensorMemoryObj` (alignment padding on) must submit the padded length, as `_obj_to_memoryview` does today; Task 6 (`test_raw_path_uses_padded_length`).

## File Structure

- Modify `lmcache/v1/distributed/l2_adapters/native_connector_l2_adapter.py`: demux restructuring, lock-free loads, abandoned ids, raw-path use.
- Modify `csrc/storage_backends/connector_pybind_utils.h`: `bind_submit_batch_get_raw`, `bind_submit_batch_set_raw`, key builder, macro entries.
- Test: `tests/v1/distributed/test_native_connector_l2_adapter.py` (fake-client interleavings), `tests/inprocess/v1/storage_backend/test_fs_native_connector.py` (raw binding through `lmcache_fs`), new `tests/v1/distributed/test_native_raw_submit_bench.py` (microbenchmark, marked `slow`).
- Internal: `scripts/ipu-poc/fleet_5to1/` sequence for the gate measurements (the existing `fleet.sh`, `fleet_np_ro.sh`, and the GIL/lock profile script from 2026-11-03).

Unit tests run locally (`uv run pytest ...`, macOS fine for Python-only tasks); binding tests need the rebuilt `lmcache_fs` (Linux: mmgi1 with `scripts/ipu-poc/build_lmcache_fs.sh`, from the io_uring plan's Task 1; macOS: `uv pip install -e .` builds it).

---

## PR B1: lock-free load submits

### Task 1: Build result bitmaps outside the lock

**Files:**
- Modify: `lmcache/v1/distributed/l2_adapters/native_connector_l2_adapter.py:457-605`
- Test: `tests/v1/distributed/test_native_connector_l2_adapter.py`

**Interfaces:**
- Produces: `_build_bitmap(result_bools: list[bool]) -> Bitmap` (module-level helper), and `_route_completion(self, fid: int, ok: bool, error: str, result_bools: list[bool] | None, bitmap: Bitmap | None, out: _Notifications) -> None`, called under `self._lock`, which does every state transition today's loop does for one completion. `_Notifications` is a small dataclass holding the five lists the loop collects today (`keys_stored`, `sizes_stored`, `keys_accessed`, `keys_deleted`, `sizes_deleted`) plus `delete_done_events`.

- [ ] **Step 1: Write failing tests** (fake client pushes the drained batch in one `drain_completions` call)

```python
def test_store_then_delete_same_key_in_one_drain(adapter_with_fake):  # [STORE(K), DELETE(K)]
    ...
    assert adapter._key_sizes.get(K) is None and adapter.get_usage().bytes == 0
def test_delete_then_store_same_key_in_one_drain(...): ...  # size recorded, usage == size
def test_store_delete_store_same_key_in_one_drain(...): ...  # size recorded once
def test_listener_calls_match_previous_loop(...):  # recorded listener call list equals a golden list
```

Golden lists are captured from the current loop before refactoring (record them in the test as literals).

- [ ] **Step 2: Run** `uv run pytest tests/v1/distributed/test_native_connector_l2_adapter.py -k "same_key or listener_calls" -q` **, expect PASS on today's code** (they pin current behaviour; this is a characterization step), then refactor.
- [ ] **Step 3: Refactor** `_demux_loop`: drain, build bitmaps for completions with `result_bools` outside the lock, then one `with self._lock:` that calls `_route_completion` per completion in order and the eventfd `notify()`s; listeners after release. Completions with `result_bools is None` get `bitmap=None` and are built inside `_route_completion`.
- [ ] **Step 4: Run the whole adapter test file, expect PASS.**
- [ ] **Step 5: Commit** `refactor: build native adapter result bitmaps outside the lock`.

### Task 2: Record abandoned delete ids

**Files:**
- Modify: `native_connector_l2_adapter.py:369-400` (`delete()` timeout path), `_route_completion`
- Test: `tests/v1/distributed/test_native_connector_l2_adapter.py`

**Interfaces:**
- Produces: `self._abandoned: set[int]` (guarded by `self._lock`).

- [ ] **Step 1: Write failing test** `test_timed_out_delete_completion_is_dropped`: fake client holds the delete completion; patch `_DELETE_TIMEOUT_S` to 0.05; after `delete()` returns, push the completion; assert no listener fires for it, and a following store and load of other keys succeed.
- [ ] **Step 2: Run, expect FAIL** (`AttributeError: _abandoned`).
- [ ] **Step 3: Implement**: timeout path adds the popped future id to `_abandoned`; `_route_completion` drops and discards abandoned ids before the unknown-id branch. Make the 30 s timeout a class constant `_DELETE_TIMEOUT_S` so the test can patch it.
- [ ] **Step 4: Run, expect PASS.**
- [ ] **Step 5: Commit** `fix: drop late completions of timed-out native adapter deletes`.

### Task 3: Lock-free load submit with early-completion parking

**Files:**
- Modify: `native_connector_l2_adapter.py` (`__init__`, `submit_load_task:322-342`, `_demux_loop`, `_route_completion`, `close:436-447`)
- Test: `tests/v1/distributed/test_native_connector_l2_adapter.py`

**Interfaces:**
- Consumes: `_route_completion`, `_abandoned`.
- Produces:
  - `self._task_ids = itertools.count(start)` replacing `_next_task_id` for all ops (`_get_next_task_id` returns `next(self._task_ids)`).
  - `self._early_completions: dict[int, tuple[bool, str, list[bool] | None]]`, `self._adopted: collections.deque[tuple[int, bool, str, list[bool] | None]]`, `self._wake_efd = create_event_notifier()` polled by `_demux_loop` with the client fd.
  - `close()` sets `self._closing = True` under the lock; `submit_load_task` after that returns a task whose result is an all-failure bitmap.

- [ ] **Step 1: Write failing tests** with a fake client whose `submit_batch_get` can (a) push the completion before returning, (b) push it on the next drain, (c) block until the test releases it after the demux has drained:

```python
def test_early_completion_before_submit_returns(...): # one result, one load-efd notify
def test_completion_in_next_drain(...): ...
def test_register_between_drain_and_route(...): ...
def test_listeners_only_on_demux_thread(...):  # record threading.get_ident() in listener
def test_adopted_load_recency_and_deleted_key(...):
    # [LOAD(A), LOAD(B)] with B adopted -> LRU order B newer than A;
    # B's key deleted before adoption -> not re-touched
def test_concurrent_load_submitters_each_get_own_result(...):  # 8 threads x 200 submits
def test_submit_raising_leaves_no_parked_state(...):
def test_close_drops_parked_and_fails_late_submit(...):
```

- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** per the spec's "Loads submit without the lock": native call outside the lock; register then claim any parked completion into `_adopted` and `notify()` `_wake_efd`, all in one critical section; `_demux_loop` drains `_adopted` first each pass through `_route_completion`; unknown, non-abandoned ids go to `_early_completions`. `submit_store_task`, lookup and delete paths keep the lock across the native call.
- [ ] **Step 4: Run the adapter test file 20 times** (`for i in $(seq 20); do uv run pytest tests/v1/distributed/test_native_connector_l2_adapter.py -q -p no:randomly || break; done`), **expect PASS every time.**
- [ ] **Step 5: Commit** `perf: submit native adapter loads without holding the adapter lock`.

### Task 4: PR B1 gate measurement

- [ ] **Step 1: Deploy** the branch's `lmcache/` to `/root/dig5to1/lmc_b1` on all six initiators (copy of `lmc_samedir` plus the changed adapter file).
- [ ] **Step 2: Run, same day, baseline then B1:** single-host mmgi0 5:1 Mixtral 64 KiB IF 64 (300 s, others idle), fleet 5:1 IF 64, fleet RO IF 16; during each 5:1 cell capture the thread-state profile (`--gil`, `--threads --idle`, per-thread CPU) exactly as `gil_seq.sh` did.
- [ ] **Step 3: Decide:** ship if single-host and fleet 5:1 both rise and the issuing thread's `self._lock` wait falls; otherwise record the numbers on the bead and stop B1.
- [ ] **Step 4: Open PR B1** with the before/after table and heat maps.

## PR B2: raw batch submit

### Task 5: Raw submit binding

**Files:**
- Modify: `csrc/storage_backends/connector_pybind_utils.h`
- Test: `tests/inprocess/v1/storage_backend/test_fs_native_connector.py`

**Interfaces:**
- Produces (on every client bound with `LMCACHE_BIND_CONNECTOR_METHODS`):
  - `submit_batch_get_raw(model_name: str, cache_salt: str, tags_token: str, kv_ranks: Buffer, object_group_ids: Buffer, chunk_hashes: bytes, hash_len: int, buf_ptrs: Buffer, buf_lens: Buffer) -> int`
  - `submit_batch_set_raw(...)` with the same arguments.
  - Internal: `std::vector<std::string> build_raw_keys(const std::string& model, const std::string& salt, const std::string& token, const std::vector<uint64_t>& ranks, const std::vector<uint64_t>& groups, const std::vector<uint8_t>& hashes, size_t hash_len)`.

- [ ] **Step 1: Write failing tests**

```python
def test_raw_store_writes_canonical_filenames(tmp_path): # files on disk == encode_fs_filename(key)
    # parametrize: salt "" / "tenant"; tags {} / {"lora": "a1"}; rank 0, 2**64-1; group 0, 2**64-1; hash_len 8, 16, 32
def test_raw_store_string_load_round_trip(tmp_path): ...
def test_string_store_raw_load_round_trip(tmp_path): ...
@pytest.mark.parametrize("bad", ["len_mismatch", "fmt_d", "fmt_q", "fmt_be", "two_d", "noncontig",
                                 "empty", "hash_len_0", "hash_len_mismatch", "at_in_model",
                                 "at_in_salt", "at_in_token"])
def test_raw_submit_rejects_bad_arguments(tmp_path, bad):  # raises ValueError, nothing submitted
def test_raw_submit_accepts_unaligned_arrays(tmp_path):  # memoryview slice at odd offset
```

- [ ] **Step 2: Build `lmcache_fs`, run, expect FAIL** (`AttributeError: submit_batch_get_raw`).
- [ ] **Step 3: Implement**: validate with `py::buffer_info` (format, ndim, strides, itemsize), copy into vectors with `memcpy`, check pointer and length conversions, `py::gil_scoped_release`, build keys with one preallocated `std::string` per key (`%08x`, `%x`, lowercase hex), call `self.submit_batch_get(keys, bufs, lens, lens[0])`. Add both to the macro with docstrings stating the trusted-input contract.
- [ ] **Step 4: Run, expect PASS** (mmgi1 and macOS).
- [ ] **Step 5: Commit** `feat: add raw batch submit to native connector bindings`.

### Task 6: Adapter uses the raw path

**Files:**
- Modify: `native_connector_l2_adapter.py` (`__init__`, `submit_load_task`, `submit_store_task`)
- Test: `tests/v1/distributed/test_native_connector_l2_adapter.py`, `tests/v1/distributed/test_native_raw_submit_bench.py`

**Interfaces:**
- Consumes: Task 5 methods; `encode_tags` from `key_codec`.
- Produces: `_pack_raw(self, keys: list[ObjectKey], objects: list[MemoryObj]) -> _RawBatch | None` (None means use the string path); `_RawBatch` holds the nine arguments; `self._has_raw_get`, `self._has_raw_set` set from `hasattr` at construction.

- [ ] **Step 1: Write failing tests**
  - `test_raw_path_used_when_eligible` (fake client records which method ran);
  - `test_string_path_for_mixed_salt`, `..._for_bytes_buffer_obj`, `..._for_negative_rank`, `..._for_rank_2_64`, `..._for_empty_hash`, `..._for_client_without_raw`;
  - `test_raw_path_single_and_large_submit` (1 and 4,096 keys, fs_native round trip);
  - `test_raw_path_uses_padded_length` (padding on: submitted length == `len(_obj_to_memoryview(obj, True))`);
  - `test_raw_store_sizes_match_string_path` (`_pending_store_sizes` identical for both paths).
  - Microbenchmark (`@pytest.mark.slow`): 56-key Mixtral 64 KiB submit, raw vs string path through `_pack_raw` and a no-op client; assert raw per-submit time < string per-submit time, print both.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement** `_pack_raw`: one pass over keys and objects reading `key.identity` once, checking eligibility as it goes, filling `array("Q")` for ranks, groups, pointers (`obj.data_ptr`) and lengths (padded length when `_pad_buffers_to_alignment`), `b"".join` for hashes; catch `OverflowError` and return None. Use it in both submit methods before falling back.
- [ ] **Step 4: Run tests and the microbenchmark** (`uv run pytest tests/v1/distributed/test_native_raw_submit_bench.py -m slow -s`), **expect PASS**; if the microbenchmark fails, stop and report the numbers.
- [ ] **Step 5: Commit** `perf: use the raw batch submit in the native connector adapter`.

### Task 7: PR B2 measurement

- [ ] **Step 1: Deploy** B2 (with B1 if it shipped) to `/root/dig5to1/lmc_b2` and the rebuilt `lmcache_fs` on all six initiators.
- [ ] **Step 2: Run, same day:** baseline, B2: single-host and fleet 5:1 IF 64, fleet RO IF 16, each with the thread-state profile; plus padded-buffer and warm-view cases called out in the spec.
- [ ] **Step 3: Open PR B2** with the table; state the measured issuing-thread share removed, not the profile estimate.
