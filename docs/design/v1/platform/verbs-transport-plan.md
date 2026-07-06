# VerbsRdmaTransport — Implementation Plan

Reference: [verbs-transport.md](verbs-transport.md)


## Steps

### Step 1: Evolve RegisteredBuffer to carry backing object

**File:** `lmcache/v1/platform/ipu/rdma_transport.py`

- Add `Closeable` protocol (single `close()` method)
- Add `backing: Closeable | None = None` field to `RegisteredBuffer` (public)
- Create a `_CtypesBackingWrapper` class with no-op `close()` to wrap the
  ctypes array the stub uses
- Update `StubRdmaTransport.allocate_buffer()` to set
  `backing=_CtypesBackingWrapper(buf)`
- Update `StubRdmaTransport.free_buffer()` to call `buf.backing.close()` (no-op)
- Add `drain_on_timeout()` to `RdmaTransport` protocol (returns `bool`)
- Add `StubRdmaTransport.drain_on_timeout()` → returns `True` (always safe)
- Add `release_buffer_tracking(buf: RegisteredBuffer) -> None` to
  `RdmaTransport` protocol
- Add `StubRdmaTransport.release_buffer_tracking()` → no-op (stub doesn't
  track buffers for close())

**Verify:** Existing tests still pass. `RegisteredBuffer` carries `backing`.


### Step 2: Evolve post_read to accept RegisteredBuffer (breaking change)

**File:** `lmcache/v1/platform/ipu/rdma_transport.py`

Change `RdmaTransport.post_read()` signature — this is a breaking change,
no overload:

```python
def post_read(
    self,
    local_buf: RegisteredBuffer,
    remote_addr: int,
    rkey: int,
    length: int,
) -> RdmaFuture:
```

Update `StubRdmaTransport.post_read()` to extract `local_buf.addr` for memcpy.

**File:** `lmcache/v1/platform/ipu/rdma_wrapper.py`

Update `IPURdmaWrapper.to_tensor()` to pass the `RegisteredBuffer`:

```python
future = transport.post_read(
    local_buf=buf,       # was: local_addr=buf.addr
    remote_addr=self.remote_addr,
    rkey=self.rkey,
    length=self.length,
)
```

**File:** `docs/design/v1/platform/ipu.md`

Update the RdmaTransport protocol listing (line ~332) to show new signature.

**Verify:** Existing unit test `test_ipu_rdma_wrapper.py` still passes.


### Step 3: Add MR deregistration finalizer to rdma_wrapper.py

**File:** `lmcache/v1/platform/ipu/rdma_transport.py`

- Add `deregister: Callable[[], None] | None = None` field to `MrInfo`
- Update `StubRdmaTransport.register_mr()` to set
  `deregister=lambda: self._registered.pop(rkey, None)`

**File:** `lmcache/v1/platform/ipu/rdma_wrapper.py`

- Add `_deregister_on_gc(data_ptr, mr)` function:
  - Pops from `_REGISTERED_MRS`
  - Calls `mr.deregister()` if not None (captured callback, NOT
    `get_rdma_transport()` — avoids global transport dependency at GC time)
  - Wrapped in try/except for interpreter shutdown safety
- In `_get_or_register_mr`:
  - On stale entry: just pop the dict entry (finalizer handles deregistration)
  - On new registration: `weakref.finalize(tensor, _deregister_on_gc, data_ptr, mr)`

**Verify:** After `del tensor; gc.collect()`, the MR's deregister callback
is called (test with mock that records calls).


### Step 4: Create verbs_transport.py skeleton

**File:** `lmcache/v1/platform/ipu/verbs_transport.py`

- Conditional pyverbs import block with explicit enum imports (no `*`)
- `HAS_PYVERBS` flag
- `VerbsRdmaTransport` class with:
  - `create_local(role, device, port, gid_index)` classmethod:
    creates QP in INIT state, writes endpoint file, returns instance
  - `connect(remote_qpn, remote_psn, remote_gid, remote_lid)` method:
    transitions QP INIT→RTR→RTS
  - `from_env()` classmethod: reads all env vars, validates ROLE is set
    (ValueError if missing), implements two-phase init:
    - If remote vars all set: create_local + connect immediately
    - Else: create_local, write endpoint file, poll for peer endpoint
      file (100ms interval, 30s timeout), then connect
  - `__init__`: raises ImportError if not HAS_PYVERBS, stores params
  - All 6 protocol methods + `drain_on_timeout()`: `raise NotImplementedError`
  - `close()` method: no-op placeholder
  - `_inflight_future`: None initially (serialization guard)

**Verify:** `import lmcache.v1.platform.ipu.verbs_transport` succeeds
even without pyverbs. `from_env()` with missing ROLE raises ValueError.


### Step 5: Implement device/PD/CQ/QP creation (in create_local)

**In `create_local()` classmethod:**

- `VerbsContext(name=device)` — or iterate device list for first active port
- Query port attributes, verify state is ACTIVE
- `PD(self._ctx)`
- `CQ(self._ctx, cqe=128)`
- `QP(self._pd, QPInitAttr(qp_type=IBV_QPT_RC, send_cq=cq, recv_cq=cq,
    cap=QPCap(max_send_wr=64, max_recv_wr=1, max_send_sge=1, max_recv_sge=1),
    sq_sig_all=True))`
- PSN: `self._local_psn` from `LMCACHE_RDMA_LOCAL_PSN` env var if set,
  else `random.randint(0, 0xFFFFFF)` (standard RDMA practice — each QP
  gets a unique PSN to avoid sequence collisions)
- Query local GID from port
- Transition QP to INIT (only needs local info: port, pkey, access_flags)
- Write endpoint JSON to `$LMCACHE_RDMA_ENDPOINT_FILE`:
  `{"qpn": qp.qp_num, "psn": local_psn, "gid": "fe80...", "lid": port_attr.lid}`

**Note on sq_sig_all:** Setting True ensures every Send WR generates a CQE.
No need for IBV_SEND_SIGNALED per WR.

**Verify:** QP in INIT state. Endpoint file written with valid JSON.


### Step 6: Implement QP state transitions (in connect())

The `connect()` method implements RTR and RTS transitions. INIT is done
in `create_local()` (step 5).

Two private methods called by `connect()`:

- `_modify_to_rtr(remote_qpn, remote_psn, remote_gid, remote_lid)`:
  - attr_mask: QP_STATE | AV | PATH_MTU | DEST_QPN | RQ_PSN |
    MAX_DEST_RD_ATOMIC | MIN_RNR_TIMER
  - path_mtu=IBV_MTU_4096
  - dest_qp_num=remote_qpn, rq_psn=remote_psn
  - ah_attr with dlid=remote_lid, is_global=1,
    grh(dgid=_parse_gid(remote_gid), sgid_index, hop_limit=64)
  - max_dest_rd_atomic=4, min_rnr_timer=12

- `_modify_to_rts()`:
  - attr_mask: QP_STATE | SQ_PSN | MAX_QP_RD_ATOMIC | RETRY_CNT |
    RNR_RETRY | TIMEOUT
  - sq_psn=local_psn, max_rd_atomic=4, retry_cnt=7, rnr_retry=7, timeout=14

**GID parsing:** `_parse_gid(hex_str)` converts 32-char hex to 16 bytes for
pyverbs GlobalRoute.dgid.

**Verify:** After connect(), QP state query returns RTS.


### Step 7: Implement register_mr / deregister_mr

```python
def register_mr(self, buffer_ptr: int, length: int) -> MrInfo:
    access = IBV_ACCESS_LOCAL_WRITE
    if self._role == "initiator":
        access |= IBV_ACCESS_REMOTE_READ
    mr_obj = MR(self._pd, length, access, address=buffer_ptr)
    mr_info = MrInfo(
        rkey=mr_obj.rkey, addr=buffer_ptr, length=length,
        handle=mr_obj,
        deregister=lambda: self._do_deregister(mr_obj),
    )
    return mr_info

def _do_deregister(self, mr_obj: MR) -> None:
    """Internal deregistration — called by deregister_mr and finalizer."""
    try:
        mr_obj.close()
    except Exception:
        pass  # Already closed or PD destroyed

def deregister_mr(self, mr: MrInfo) -> None:
    if mr.deregister is not None:
        mr.deregister()
    elif mr.handle is not None:
        mr.handle.close()
```

**Verify:** MR created, rkey non-zero. Deregister doesn't raise.
`mr.deregister` callback is set.


### Step 8: Implement allocate_buffer / free_buffer / release_buffer_tracking

```python
def allocate_buffer(self, length: int) -> RegisteredBuffer:
    aligned = (length + 4095) & ~4095
    backing = mmap.mmap(-1, aligned,
                        flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,
                        prot=mmap.PROT_READ | mmap.PROT_WRITE)
    addr = ctypes.addressof(ctypes.c_char.from_buffer(backing))
    mr = self.register_mr(addr, aligned)
    buf = RegisteredBuffer(addr=addr, length=aligned, mr=mr, backing=backing)
    self._allocated_buffers.add(buf)
    return buf

def release_buffer_tracking(self, buf: RegisteredBuffer) -> None:
    """Transfer ownership from transport to caller (tensor finalizer)."""
    self._allocated_buffers.discard(buf)

def free_buffer(self, buf: RegisteredBuffer) -> None:
    self._allocated_buffers.discard(buf)
    self.deregister_mr(buf.mr)
    if buf.backing is not None:
        buf.backing.close()
```

**Verify:** Buffer address is page-aligned. free_buffer releases mmap.
`_allocated_buffers` tracks unreleased buffers for close() cleanup.


### Step 9: Implement post_read (target only, lock-serialized)

```python
def post_read(self, local_buf: RegisteredBuffer, remote_addr: int,
              rkey: int, length: int) -> RdmaFuture:
    if self._role != "target":
        raise RuntimeError("post_read only valid for target role")

    self._qp_lock.acquire()
    try:
        if self._closed or self._drained:
            raise RuntimeError("transport is closed or drained")

        future = RdmaFuture()
        wr_id = self._next_wr_id
        self._next_wr_id += 1
        self._inflight_wr_id = wr_id
        self._inflight_future = future

        sge = SGE(addr=local_buf.addr, length=length,
                  lkey=local_buf.mr.handle.lkey)
        wr = SendWR(wr_id=wr_id, opcode=IBV_WR_RDMA_READ,
                    num_sge=1, sg=[sge])
        wr.set_wr_rdma(rkey=rkey, addr=remote_addr)
        self._qp.post_send(wr)
        return future
    except BaseException:
        self._inflight_future = None
        self._qp_lock.release()
        raise
```

**Verify:** post_send returns without error. Initiator role raises.
Concurrent callers block on _qp_lock. Closed transport raises.


### Step 10: Implement poll_completion (lock-serialized, wr_id verified)

```python
def poll_completion(self, future: RdmaFuture, timeout_ms: int = 5000) -> bool:
    if future is not self._inflight_future:
        raise RuntimeError("poll_completion called with non-inflight future")
    try:
        deadline = time.monotonic() + timeout_ms / 1000.0
        while time.monotonic() < deadline:
            wcs = self._cq.poll(num_entries=1)
            for wc in wcs:
                if wc.wr_id != self._inflight_wr_id:
                    # Stale/unexpected CQE — skip, keep polling for ours
                    logger.warning("CQE wr_id=%d != expected %d (skipped)",
                                   wc.wr_id, self._inflight_wr_id)
                    continue
                success = (wc.status == IBV_WC_SUCCESS)
                self._inflight_future.set_complete(success=success)
                self._inflight_future = None
                return success
        # Timeout — WR still in flight, lock stays held
        return False
    finally:
        # Release lock only on completion (success or error CQE)
        if self._inflight_future is None:
            self._qp_lock.release()
```

**Verify:** After success, lock released and future completed. Passing wrong
future raises RuntimeError. On timeout, lock stays held — caller must
drain_on_timeout() which releases it. Mismatched wr_ids are skipped.


### Step 11: Implement drain_on_timeout (releases lock)

```python
def drain_on_timeout(self) -> bool:
    """Move QP to ERROR, poll until inflight WR flush CQE observed.

    MUST be called while _qp_lock is held (after poll_completion timeout).
    Always releases _qp_lock before returning.

    Returns True only if the timed-out WR's flush CQE was seen —
    meaning the buffer is provably safe to free.
    """
    try:
        attr = QPAttr(qp_state=IBV_QPS_ERR)
        self._qp.modify(attr, mask=IBV_QP_STATE)

        deadline = time.monotonic() + 2.0
        flushed = False
        while time.monotonic() < deadline:
            wcs = self._cq.poll(num_entries=16)
            for wc in wcs:
                if wc.wr_id == self._inflight_wr_id:
                    flushed = True
                    self._inflight_future.set_complete(success=False)
                    self._inflight_future = None
            if flushed:
                break
            if not wcs:
                time.sleep(0.001)

        self._drained = True
        if not flushed:
            logger.error(
                "drain_on_timeout: WR wr_id=%d not flushed after 2s",
                self._inflight_wr_id,
            )
        return flushed
    finally:
        self._qp_lock.release()
```

**Verify:** Returns True when flush CQE observed. Returns False (buffer NOT
safe to free) if 2s expires. Lock always released.


### Step 12: Implement close() and __del__

```python
def close(self) -> None:
    self._qp_lock.acquire()
    try:
        if self._closed:
            return
        self._closed = True
        # Free any tracked allocated buffers (those not transferred to tensors)
        for buf in list(self._allocated_buffers):
            self.free_buffer(buf)
        self._allocated_buffers.clear()
        # Tear down in reverse order
        if self._qp:
            self._qp.close()
            self._qp = None
        if self._cq:
            self._cq.close()
            self._cq = None
        if self._pd:
            self._pd.close()
            self._pd = None
        if self._ctx:
            self._ctx.close()
            self._ctx = None
    finally:
        self._qp_lock.release()
    # Idempotent, ignores FileNotFoundError
    self._cleanup_endpoint_file()

def __del__(self) -> None:
    self.close()
```

**Buffer ownership lifecycle:**
- `allocate_buffer()` → adds to `_allocated_buffers`
- After successful RDMA Read in `to_tensor()`:
  `transport.release_buffer_tracking(buf)` → removes from tracking
  (tensor finalizer takes ownership)
- `free_buffer()` → removes from tracking + deregisters + unmaps
- `close()` → frees only remaining tracked buffers (not tensor-owned ones)

**Verify:** No resource leak warnings. PD close succeeds (no orphan MRs).
Post_read after close raises RuntimeError. Close on drained transport works.


### Step 13: Wire into get_rdma_transport()

**File:** `lmcache/v1/platform/ipu/rdma_transport.py`

```python
elif backend == "verbs":
    from lmcache.v1.platform.ipu.verbs_transport import VerbsRdmaTransport
    _global_transport = VerbsRdmaTransport.from_env()
```

**Verify:** `LMCACHE_RDMA_TRANSPORT=verbs LMCACHE_RDMA_ROLE=target ...`
instantiates VerbsRdmaTransport. Missing ROLE raises ValueError.


### Step 14: Update IPURdmaWrapper.to_tensor() — both success and timeout paths

**File:** `lmcache/v1/platform/ipu/rdma_wrapper.py`

**A) Replace success path** (replaces `_BUFFER_KEEP_ALIVE` / `_release_buffer`):

```python
# After successful poll_completion:
transport.release_buffer_tracking(buf)  # ownership to tensor

# Wrap the local buffer as a tensor (existing code)
...
out = torch.as_strided(typed, self.shape, self.stride, self.storage_offset)

# Pin buffer lifetime to tensor storage via captured callback
storage = out.untyped_storage()
free_fn = transport.free_buffer  # capture at allocation time
weakref.finalize(storage, lambda: free_fn(buf))

return out
```

Remove the old `_BUFFER_KEEP_ALIVE` dict and `_release_buffer` function —
they are replaced by the captured `free_fn` lambda.

**B) Replace timeout path:**

```python
if not transport.poll_completion(future, timeout_ms=5000):
    flushed = transport.drain_on_timeout()
    if flushed:
        transport.free_buffer(buf)
    else:
        # Buffer quarantined — intentional leak to prevent corruption.
        transport.release_buffer_tracking(buf)
        logger.error(
            "RDMA buffer quarantined (leak): addr=0x%x len=%d",
            buf.addr, buf.length,
        )
    raise RuntimeError(
        f"RDMA Read timed out: rkey={self.rkey} "
        f"remote_addr=0x{self.remote_addr:x} length={self.length}"
    )
```

**Verify:** Success path: buffer freed when tensor GC'd (not when transport
closes). Timeout path: buffer freed only on confirmed flush, quarantined
otherwise. No calls to `get_rdma_transport()` from finalizers.


### Step 15: Unit tests

**File:** `tests/v1/platform/test_verbs_transport.py`

Tests (all use mocked pyverbs, test public API via `.wait()`):

- `test_from_env_missing_role_raises` — no ROLE set → ValueError
- `test_from_env_parses_all_vars` — mock env, verify from_env() attributes
- `test_import_error_without_pyverbs` — patch HAS_PYVERBS=False → ImportError
- `test_role_target_post_read_returns_future` — mock QP.post_send, verify called
- `test_role_initiator_post_read_raises` — assert RuntimeError
- `test_concurrent_post_read_blocks` — second thread blocks on _qp_lock until first completes
- `test_register_mr_target_local_write_only` — verify access flags
- `test_register_mr_initiator_remote_read` — verify REMOTE_READ included, no REMOTE_WRITE
- `test_poll_completion_success` — mock CQ.poll → WC(status=SUCCESS), future.wait() True
- `test_poll_completion_timeout` — mock CQ.poll → [], verify returns False
- `test_poll_completion_releases_lock` — after success, _qp_lock is released
- `test_poll_skips_mismatched_wr_id` — stale CQE with wrong wr_id is skipped, keeps polling
- `test_poll_completion_error_wc` — mock bad status, future.wait() False
- `test_drain_returns_true_on_flush` — mock CQ.poll → error CQE with matching wr_id
- `test_drain_returns_false_on_no_flush` — mock CQ.poll → [], verify False
- `test_drain_sets_qp_error` — verify modify_qp called with ERR state
- `test_allocate_buffer_page_aligned` — check addr % 4096 == 0
- `test_free_buffer_deregisters_and_unmaps` — verify deregister + backing.close()
- `test_endpoint_file_written` — from_env writes valid JSON with nonce/pid/timestamp
- `test_peer_file_polling` — mock file appearing after delay, connect succeeds
- `test_stale_peer_file_rejected` — peer file with wrong nonce is ignored
- `test_drain_releases_lock` — after drain, _qp_lock.acquire() succeeds from another thread
- `test_close_tears_down_resources` — verify close order

**Verify:** `pytest tests/v1/platform/test_verbs_transport.py -v` all pass.


### Step 16: Update existing rdma_wrapper test

**File:** `tests/v1/platform/test_ipu_rdma_wrapper.py`

- Update test to pass `RegisteredBuffer` to `post_read` (signature change)
- Add test for MR deregistration on tensor GC (weakref finalizer)

**Verify:** `pytest tests/v1/platform/test_ipu_rdma_wrapper.py -v` passes.


## Execution Order

```
Steps 1-3: Protocol evolution (RegisteredBuffer, post_read, drain_on_timeout, MR lifecycle)
Steps 4-6: VerbsRdmaTransport skeleton + device/QP creation + two-phase connect
Steps 7-11: Verb methods (register_mr, allocate, post_read, poll, drain)
Step 12: Cleanup/close
Step 14: Timeout safety in wrapper (MUST land before or atomically with step 13)
Step 13: Factory wiring (activates verbs backend — all safety code must be in place)
Steps 15-16: Tests
```

**Critical:** Steps 13 and 14 must be in the same atomic commit. If the
factory wiring is active but the timeout path still frees buffers unsafely,
a timeout under real RDMA causes memory corruption. Step 14 is listed
first to emphasize that the safety code must exist before the backend is
reachable.

Estimated size: ~450 lines for `verbs_transport.py`, ~300 lines for tests,
~50 lines of changes to existing files (rdma_transport.py, rdma_wrapper.py, ipu.md).


## Dependencies

- `pyverbs` from rdma-core ≥ 44 (system package: `apt install rdma-core python3-pyverbs`)
- No new pip dependencies
- Optional — absence handled gracefully at import time


## Risks

1. **pyverbs API variance** across rdma-core versions. Mitigated by targeting
   ≥ 44 and testing on Pat's IPU nodes.
2. **mmap flags** differ on macOS (dev machines) vs Linux (target). The
   implementation is Linux-only; macOS falls back to stub.
3. **PSN selection** — using a fixed PSN from env var rather than random
   avoids non-determinism. Both sides must agree.
