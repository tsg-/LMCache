# Raw batch submit for native-connector L2 adapters

Status: proposed design, not implemented. Covers two changes to the issuing
path of `NativeConnectorL2Adapter`: a raw batch submit in the shared pybind
layer, and a shorter critical section in the adapter's completion thread.

## Decision

The shared binding layer (`csrc/storage_backends/connector_pybind_utils.h`)
gains `submit_batch_get_raw` and `submit_batch_set_raw`. Python passes the key
fields a submit's keys share once, the per-key fields as packed arrays, and
the buffers as packed address and length arrays. C++ builds the canonical key
strings and calls the connector's existing `submit_batch_get`/`set`, so no
connector changes. Every connector bound with `LMCACHE_BIND_CONNECTOR_METHODS`
gets the raw methods.

`NativeConnectorL2Adapter` uses the raw methods when the client has them and
the submit's keys share their common fields, and today's path otherwise.

Separately, load submits stop holding the adapter's `self._lock` across the
native call, and the completion thread builds result bitmaps outside the
lock. Store accounting keeps its current ordering.

## The problem

In a 5:1 read:write load the issuing thread, not the connector, is the limit.
Measured on the MMG-400 rig, 2026-11-03: six NVMe-oF initiators, Mixtral 8x22B
FP8 at 64 KiB objects (56 per submit), `fs_native`, 300 s per cell, same-day
fio 5:1 at 1,054 Gb/s:

| issuing processes per host | in flight per process | fleet Gb/s | of fio |
|---|---|---|---|
| 1 | 64 | 630 | 60% |
| 2 | 32 | 819 | 78% |
| 2 | 64 | 714 | 68% |

With one process the main thread was 66% on CPU and spent 2% waiting for
completions; the connector's threads were mostly idle and md0 held about 69
I/Os. py-spy of that thread (30 s, mid-run, sampled without idle):

| cost on the issuing thread | share |
|---|---|
| `submit_load_task` waiting on `self._lock` (line 332) | 20% |
| native `submit_batch_get` / `submit_batch_set` calls | 16% / 9% |
| building fresh store `ObjectKey`s (bench side) | 21% |
| `encode_storage_key` via `_object_key_to_string` | 9% |
| `_obj_to_memoryview` | 11% |

The native calls include pybind converting the key list and memoryview list
element by element under the GIL. The lock wait is time the completion thread
holds `self._lock` while it processes a drained batch (`_demux_loop`).

In read-only loads the same thread is not the fleet limit (it waits 37% of
the time), so these changes target mixed loads and single-host line rate.
The estimated saving below is from the profile, not measured.

## The raw submit

### Signature

    submit_batch_get_raw(
        model_name: str,
        cache_salt: str,
        tags_token: str,          # "" or the "x1-..." token from encode_tags
        kv_ranks: buffer,         # uint64, one per key
        object_group_ids: buffer, # uint64, one per key
        chunk_hashes: bytes,      # concatenated, hash_len bytes per key
        hash_len: int,
        buf_ptrs: buffer,         # uint64 addresses, one per key
        buf_lens: buffer,         # uint64 byte lengths, one per key
    ) -> int                      # future id, as submit_batch_get

`submit_batch_set_raw` takes the same arguments. The four arrays must be
one-dimensional, C-contiguous buffers with format `"Q"` or `"=Q"` (native
unsigned 64-bit; `array.array("Q")` qualifies on the 64-bit Linux hosts LMCache
supports) and item size 8. Anything else raises: a float, signed or
foreign-endian buffer would satisfy "8-byte items" and decode to different
values. `hash_len` must be positive, and `len(chunk_hashes)` must equal
`hash_len x n` with overflow-checked arithmetic.

Under the GIL the binding validates, then copies the shared strings, the
hash bytes and the four arrays into C++ vectors with `memcpy` (the buffers may
be unaligned), checking each address fits `uintptr_t` and each length fits
`size_t`. It then releases the GIL, builds the key strings without it, and
calls the existing method. Building strings after the release keeps that work
off the issuing thread's GIL hold.
Ranks and groups are unsigned 64-bit. The string path formats any Python int
(`kv_rank` is not range-checked, and `object_group_id` is only checked
non-negative), so a submit with a negative rank or a value at or above 2^64
falls back to the string path: the adapter catches `OverflowError` from
packing and takes today's path. Those keys stay valid; they are just not
accelerated.

`exists` and `delete` keep string keys; they are not on the hot path.

### Key format is a contract

The raw methods are public binding methods with a trusted-input contract,
stated in their docstrings: callers pass fields taken from already validated
`ObjectKeyIdentity` objects and a tags token produced by `encode_tags`.
`NativeConnectorL2Adapter` is the only caller in tree. As cheap defence, the
binding still rejects `@` in the model name, salt or token. It does not
re-implement salt-length or tag validation, and it accepts whatever the
string path accepts that the format can express, including an empty model
name.

The C++ builder must produce exactly what `encode_storage_key` produces:

    {model}@{rank:08x}@{group:x}@{hash hex}             no salt, no tags
    {model}@{rank:08x}@{group:x}@{hash hex}@{salt}      salt, no tags
    {model}@{rank:08x}@{group:x}@{hash hex}@{salt}@{t}  tags (salt may be "")

A connector that maps keys to storage names (for example `key_to_filename` in
`keys.h`) sees byte-identical input, so existing caches stay readable. The
guard against a silent cache-miss regression is a round-trip test through
`fs_native`: store through the raw path, then check that the files on disk are
exactly `encode_fs_filename(key)` for each key, and that the string path loads
them; then the reverse. No production method exists only for testing.

### Adapter use

`submit_load_task` and `submit_store_task` take the raw path when:

- the client has `submit_batch_get_raw` / `submit_batch_set_raw`, and
- every key in the submit has the same `model_name`, `cache_salt` and tags,
  and the same chunk-hash length, and that length is not zero (the string
  path accepts an empty hash; the raw arrays cannot express it, so such keys
  take the string path).

- every object is a `TensorMemoryObj` (others, for example
  `BytesBufferMemoryObj`, whose `data_ptr` cannot be taken from read-only
  `bytes`, use the string path).

Otherwise they use today's path. A mixed submit is not split (my call): it is
rare in serving, and splitting would change completion semantics.

The eligibility check is itself per-key Python work: one attribute walk per
key for model, salt, tags and hash length. PR 2 includes a microbenchmark of
the full raw path, check included, against the string path on a 56-key
submit, and ships only if the net per-key cost is lower.

Buffer addresses and lengths come from each `MemoryObj`'s `data_ptr` and the
same length `_obj_to_memoryview` uses today, including alignment padding when
`_pad_buffers_to_alignment` is set. Buffer lifetime is unchanged: the caller
already keeps the objects alive until the task completes, and the memoryview
never owned the memory.

Packing is done with `array.array` (no new dependency). The adapter still
touches each object once in Python to read its address and size; what goes
away is the key string, the memoryview, and pybind's per-element casts. An
optional fast path for callers that already hold packed arrays is out of
scope.

## The shorter critical section

`_demux_loop` today holds `self._lock` across the whole drained batch:
popping `_pending_ops`, converting result lists to `Bitmap`s, updating
`_key_sizes`, `_pending_store_sizes` and `_locked_keys`, publishing results,
and calling the eventfd `notify()`s. Only the listener callbacks and delete
events run after the lock is released.

The state transitions must stay in completion order. A drained batch can hold
`[STORE(K), DELETE(K)]`; applying the delete first finds no size record, and
the store then records a size for an object that no longer exists. So the new
loop changes only what is pure computation:

1. without the lock, convert each completion that carries a result list into
   a `Bitmap`. A completion carries only its future id, success flag, error
   and result booleans (`connector_pybind_utils.h:153`), and that bitmap needs
   nothing else, so this step reads no shared state. A completion without a
   result list (`result_bools` is `None`, for example an all-success or
   all-failure load) needs the pending entry's `num_keys`, so its bitmap is
   built in step 2, inside the lock, as today;
2. under the lock, in completion order, do every state transition exactly as
   today: pop `_pending_ops`, compute store byte totals from
   `_pending_store_sizes` and `_key_sizes`, update both and `_locked_keys`,
   publish results with the prebuilt bitmaps, and call the eventfd
   `notify()`s;
3. after releasing it, fire the `_notify_keys_*` listeners and set delete
   events, as today.

Deletes, lookups and `delete()`'s timeout path are unchanged, because step 2
is today's critical section minus the bitmap and list work.

### Measured: lock convoy, not GIL

A 2026-11-03 profile of the same 5:1 cell (mmgi0, py-spy `--gil` and
`--threads --idle`, plus per-thread CPU, 30 s windows) settled the GIL
question. The raw profiles and a thread-state heat map are attached to the PR.

| thread | on CPU | holds the GIL | blocked on `self._lock` | idle |
|---|---|---|---|---|
| issuing thread | 75% | 50% | 23% | 2% |
| completion thread (`_demux_loop`) | 27% | 18% | 35% | 39% |
| retention deleters (8, mean) | 1% | 1% | 1% | 99% |

The GIL is held 75% of wall time overall, so it is not saturated. The issuing
thread's wait is real lock blocking: its CPU time plus lock wait cover 98% of
wall time. The completion thread is blocked on the same lock 35% of the time,
because `submit_load_task` and `submit_store_task` hold `self._lock` across
the native `submit_batch_*` call, which releases the GIL but not the lock. The
two threads take turns: while one is in native code or routing completions,
the other waits.

### Loads submit without the lock

The lock is held across the native call today so that a completion cannot
reach `_demux_loop` before its pending entry exists. For loads, this is
replaced with early-completion parking:

1. The submitter allocates the task id from an `itertools.count` (atomic
   under the GIL), calls the native `submit_batch_get` (or the raw variant)
   without `self._lock`, then takes the lock to register the pending entry.
2. `_demux_loop` drains completions outside the lock, as today, then routes
   them under it. A completion whose future id is neither registered nor in
   `_abandoned` (below) is parked in `_early_completions` instead of being
   warned about and dropped.
3. When the submitter registers, it checks `_early_completions` for its
   future id in the same critical section. If one is parked, it removes it and
   appends it to `_adopted`, then wakes the demux thread through an internal
   eventfd that `_demux_loop` polls alongside the client's. The submitter
   never publishes results or fires listeners itself, so listener callbacks
   stay on the demux thread only.
4. At the start of each pass, before routing newly drained completions, the
   demux thread finishes everything in `_adopted` through the same routine,
   publishing the result before calling the load eventfd's `notify()`. The
   native wakeup for that completion was consumed when it was drained, so
   this `notify()` is required.

Registration, the parked entry's removal and its hand-off to `_adopted` happen
in one critical section, so a completion is finished exactly once.

**Retired operations are not early loads.** `delete()`'s timeout path removes
its pending entry (`native_connector_l2_adapter.py:382`), so its completion
can arrive for an id that is no longer registered. The timeout path records
the id in `_abandoned` under the same lock, and the demux drops completions
for abandoned ids, as it warns and drops them today. Only ids in neither map
are parked.

**What changes for eviction.** Today the accessed keys of one drained batch
reach `_notify_keys_accessed` as one list, which LRU applies in reverse order
(`eviction_policy/lru.py:107`), so the first-drained load becomes the most
recent. An adopted load is notified in a later pass, so it becomes more
recent than loads drained alongside it. This design accepts that: loads
drained in one batch completed concurrently, and their relative order is
already set by native completion timing, not by the caller. To avoid
reporting an access after a deletion, an adopted load's accessed keys are
filtered against `_key_sizes` at notification time, so keys deleted in the
meantime are left out. A test pins both behaviours.

**Shutdown.** `close()` requires that callers have stopped submitting, as
`StorageManager.close()` already ensures by stopping its controllers first
(`storage_manager.py:1257`); the docstring states it. Under the lock,
`close()` sets the stop flag, and a submitter that registers after it fails
its task immediately instead of parking. Anything left in
`_early_completions` or `_adopted` is logged with its count and dropped.

Stores keep today's protocol: the lock is held across the native
`submit_batch_set`, so a store is always registered before the demux routes
its completion, and size accounting stays in completion order. Stores are one
submit in six in a 5:1 load, and the profile puts most of the lock wait on
the load path (`submit_load_task`, 20.6% of the issuing thread). Lookups and
deletes keep their current protocol apart from the `_abandoned` record.

### Gate

PR 1 ships if, on the same rig, single-host and fleet 5:1 throughput rise and
the issuing thread's lock wait falls, measured the same way as the table
above.

## Expected effect

Not estimated as a single number, because the profile overstates what moves:

- PR 1 targets the 23% the issuing thread spends blocked on `self._lock` and
  the 35% the completion thread spends blocked on it. How much of that turns
  into throughput depends on what limits next, which is why it is gated on a
  measurement.
- PR 2 removes the Python key strings (9%) and pybind's per-element list and
  memoryview casts. It does not remove the memoryview cost the profile shows
  for unpadded `TensorMemoryObj`s, since their `byte_array` view is already
  cached; the remaining `_obj_to_memoryview` time is wrapper overhead that the
  raw path replaces with `data_ptr` and length reads plus array packing. The
  key strings are still built, in C++ and after the GIL is released.

The two-process 5:1 result (819 Gb/s) is not a prediction: two processes also
double the native worker pools and change queueing. Each PR reports measured
single-host and fleet throughput before and after on the same rig, with warm
and padded buffers measured separately, and counts all packing and accounting
work in the comparison.

## Testing

- Key-format equivalence through `fs_native` filenames, as above, including
  tagged and salted keys, ranks 0 and 2^64-1, groups 0 and 2^64-1, and hash
  lengths 8, 16 and 32. `batch_chunk_num_bytes` is taken from the first
  length, as `bind_submit_batch_get` does today.
- Round-trip get/set through the raw path for `fs_native` and one other bound
  connector, comparing data with the string path.
- Argument validation: mismatched array lengths, wrong formats (`"d"`, `"q"`,
  `">Q"`), wrong item sizes, two-dimensional and non-contiguous buffers,
  unaligned buffers (accepted and read correctly), empty submit, `hash_len` 0
  or not dividing `chunk_hashes`, and `@` in model, salt or token all raise
  before anything is submitted, except the unaligned case.
- Adapter fallback: a mixed-salt submit, a non-`TensorMemoryObj` object, and
  a rank outside [0, 2^64) each take the string path; a client without the raw
  methods (a plugin connector) works unchanged.
- Early completions, as deterministic interleavings with a fake client: a
  load that completes before `submit_batch_get` returns, one that completes in
  the next drained batch, and one whose submitter registers between the
  demux's drain and its routing. Each delivers exactly one result and one
  load-eventfd notification, and listeners only ever run on the demux thread.
- Retired ids: a delete that times out, then completes, is dropped, not
  parked.
- Eviction semantics: for `[LOAD(A), LOAD(B)]` with B adopted, LRU recency is
  B newer than A, and an adopted load whose key was deleted in between does
  not re-touch it.
- Shutdown: `close()` after callers stop logs and drops parked entries; a
  submit racing `close()` fails its task instead of hanging.
- Demux ordering: deterministic tests feed drained batches of
  `[STORE(K), DELETE(K)]`, `[DELETE(K), STORE(K)]` and
  `[STORE(K), DELETE(K), STORE(K)]` and check `_key_sizes`, usage totals and
  listener calls match today's loop exactly.
- Demux stress: concurrent submits, completions, unlocks and timing-out
  deletes; every task completes exactly once and `close()` does not hang.

## Rollout

Two PRs, each small enough to review alone:

1. Lock-free load submit and bitmaps outside the lock (Python only), gated
   on the measurement above.
2. Raw submit binding, adapter use, tests.

## Non-goals and follow-ups

- Store-key construction cost (21% in the 5:1 profile) is in `ObjectKey` and
  `ObjectKeyIdentity` validation, a public API. It is a separate change.
- No caching of encoded keys across submits. The bench reuses load keys and a
  serving engine mostly does not, so a cache would inflate benchmark numbers
  without helping serving.
- No change to completion delivery (`drain_completions`) or to the connector
  interface.

## Questions for review

- Is a single shared salt/tags per submit the right fast-path condition, or
  do real callers mix them often enough to need per-key fields?
- Does any bound connector depend on receiving Python `str` keys in a way the
  raw path would bypass (for example, logging or key transforms in pybind)?
