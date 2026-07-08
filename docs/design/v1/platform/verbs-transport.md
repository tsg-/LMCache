# VerbsRdmaTransport — Design Spec

> Part of: [IPU RDMA Platform Backend](ipu.md) |
> POC scope: [ipu-poc.md](ipu-poc.md)

Implements `RdmaTransport` protocol backed by real libibverbs via `pyverbs`
(rdma-core Python bindings). Activated by `LMCACHE_RDMA_TRANSPORT=verbs`.

**Issue:** LMCache-2g3 (P1)
**Status:** Implemented


## Scope

A single `VerbsRdmaTransport` class that:

- Satisfies the `RdmaTransport` protocol (register_mr, deregister_mr,
  post_read, post_write, poll_completion, allocate_buffer, free_buffer,
  release_buffer_tracking, drain_on_timeout)
- Operates as **initiator** (exposes registered DRAM) or **target** (posts
  RDMA Reads/Writes) based on `LMCACHE_RDMA_ROLE` env var (required, no default)
- Connects a single RC QP to one remote peer at init time
- Uses busy-poll CQ for completion with wr_id-based future dispatch
- Serialized usage: caller must poll_completion before the next posted operation
- Falls back gracefully when pyverbs is not installed


## Out of Scope (Filed as P3)

- Multi-peer / multi-QP striping (LMCache-9he)
- Dynamic reconnection (LMCache-6c9)
- Event-driven CQ (LMCache-kyc)
- GID resolution / rdma_cm (LMCache-ztb)
- Pre-registered buffer pool (LMCache-br4)


## Environment Variables

| Variable | Values | Required | Default | Purpose |
|----------|--------|----------|---------|---------|
| `LMCACHE_RDMA_TRANSPORT` | `stub`, `verbs` | no | `stub` | Backend selection |
| `LMCACHE_RDMA_ROLE` | `initiator`, `target` | **yes** | — | Process role (fail fast if unset) |
| `LMCACHE_RDMA_DEVICE` | IB device name | no | first active | Device selection |
| `LMCACHE_RDMA_PORT` | integer | no | `1` | IB port number |
| `LMCACHE_RDMA_GID_INDEX` | integer | no | `0` | GID table index (RoCE) |
| `LMCACHE_RDMA_LOCAL_PSN` | integer | no | random | Local packet sequence number |
| `LMCACHE_RDMA_REMOTE_LID` | integer | no | `0` | Peer LID (InfiniBand) |
| `LMCACHE_RDMA_REMOTE_QPN` | integer | preconfigured mode | — | Peer QP number |
| `LMCACHE_RDMA_REMOTE_PSN` | integer | preconfigured mode | — | Peer packet sequence number |
| `LMCACHE_RDMA_REMOTE_GID` | hex string (32 chars) | preconfigured mode (RoCE) | — | Peer GID |
| `LMCACHE_RDMA_ENDPOINT_FILE` | path | no | `/tmp/lmcache_rdma_{role}_{nonce}.json` | Write local endpoint |
| `LMCACHE_RDMA_PEER_ENDPOINT_FILE` | path | no | `/tmp/lmcache_rdma_{peer_role}_{nonce}.json` | Poll for peer endpoint |
| `LMCACHE_RDMA_NONCE` | string | rendezvous mode | — | Session nonce (shared by both sides) |

**Connection modes:**
- **Preconfigured:** All three `REMOTE_*` vars set → connect immediately,
  no file rendezvous. For operator-managed or scripted two-node setups.
- **Rendezvous:** `REMOTE_*` vars absent → two-phase init with endpoint
  files (see QP Bootstrap section).

**Rationale (separate vars):** GIDs in RoCE are colon-delimited IPv6
addresses. GID is passed as a 32-character hex string (no colons):
`fe80000000000000a0369fffff01abcd`.

**PSN choice:** Default is `random.randint(0, 0xFFFFFF)` generated once at
QP creation. Override via `LMCACHE_RDMA_LOCAL_PSN` for reproducible testing.


## Architecture

### File Location

```
lmcache/v1/platform/rdma/verbs_transport.py
```

Lives alongside `rdma_transport.py` (protocol + stub). Changes to existing
files: wiring `"verbs"` in `get_rdma_transport()`.


### Protocol Evolution: post_read Takes RegisteredBuffer

**Issue 4 resolution:** The `RdmaTransport.post_read()` protocol currently
takes `local_addr` but verbs needs the local MR's `lkey`. Rather than
maintain a brittle address-to-MR interval lookup, we evolve the protocol.

**This is a breaking change to the `RdmaTransport` protocol.** The old
`local_addr: int` parameter is replaced by `local_buf: RegisteredBuffer`.
All implementations and call sites are updated:

```python
# New protocol signature (replaces old local_addr: int)
def post_read(
    self,
    local_buf: RegisteredBuffer,
    remote_addr: int,
    rkey: int,
    length: int,
) -> RdmaFuture:
```

- `StubRdmaTransport.post_read()`: updated to accept `RegisteredBuffer`,
  extracts `.addr` for memcpy. No overload.
- `VerbsRdmaTransport.post_read()`: extracts `.mr.handle` for lkey.
- `RdmaWrapper.to_tensor()`: passes the `RegisteredBuffer` from
  `allocate_buffer()` directly.
- Design doc `docs/design/v1/platform/ipu.md` "RdmaTransport Protocol" section:
  updated to show new signature.


### post_write (LMCache-zbg)

Adds RDMA Write for the retrieve push path: the target posts a Write into
the initiator's registered DRAM instead of the initiator posting a Read
against the target (see `ipu.md` "Data Flow — Retrieve Path"). Mirrors
`post_read`'s lock/future/wr_id bookkeeping exactly; only the opcode and
transfer direction differ:

```python
def post_write(
    self,
    local_buf: RegisteredBuffer,
    remote_addr: int,
    rkey: int,
    length: int,
) -> RdmaFuture:
    if self._role != "target":
        raise RuntimeError("post_write only valid for target role")
    # same lock/future/wr_id setup as post_read, but:
    wr = SendWR(wr_id=wr_id, opcode=IBV_WR_RDMA_WRITE, num_sge=1, sg=[sge])
    wr.set_wr_rdma(rkey=rkey, addr=remote_addr)
    self._qp.post_send(wr)
```

`poll_completion` and `drain_on_timeout` are reused unchanged — completion
dispatch is by `wr_id` and is opcode-agnostic.

Enabling target-initiated writes into the initiator's MR requires the
initiator to register with `REMOTE_WRITE` in addition to `REMOTE_READ`
(see "Access Flags by Role" below).


### Class Structure

```python
class VerbsRdmaTransport:
    """RdmaTransport implementation backed by libibverbs via pyverbs."""

    def __init__(self, role: str, device: str, port: int,
                 gid_index: int, local_psn: int) -> None:
        """Create transport with local resources only (QP in INIT state).

        Remote peer info is NOT required at construction — call connect()
        separately to transition QP to RTS.
        """
        # 1. Validate role is "initiator" or "target" (fail fast)
        # 2. Open IB device context
        # 3. Query port for active state
        # 4. Allocate PD
        # 5. Create CQ (depth=128)
        # 6. Create RC QP (sq_sig_all=True, max_send_wr=64, max_recv_wr=1)
        # 7. Transition QP: RESET -> INIT (only needs local info)
        # 8. Store local QPN/PSN/GID for bootstrap export
        # 9. Init _qp_lock, _inflight_future=None, _drained=False
        ...

    def connect(self, remote_qpn: int, remote_psn: int,
                remote_gid: str, remote_lid: int = 0) -> None:
        """Transition QP from INIT -> RTR -> RTS using peer info."""
        ...
```


### QP Bootstrap / Two-Phase Init

**Issue 1 resolution:** RC QPs require peer info (qpn, psn, gid) to
transition to RTR/RTS, but a process's QPN is only known after QP creation.
The POC uses a **two-phase init with a rendezvous file**:

**Phase 1 — Create QP, export local endpoint:**

```python
transport = VerbsRdmaTransport.create_local(role, device, port, gid_index)
# QP in INIT state (no peer info needed yet)
# Writes local endpoint to LMCACHE_RDMA_ENDPOINT_FILE (JSON):
# {"qpn": 42, "psn": 12345, "gid": "fe80...abcd", "lid": 1}
```

**Phase 2 — Read peer endpoint, connect:**

```python
transport.connect(remote_qpn, remote_psn, remote_gid, remote_lid)
# Transitions QP: INIT -> RTR -> RTS
# Transport is now ready for post_read / register_mr
```

**`from_env()` implements both phases atomically** when all remote vars are
already set (pre-configured by operator or test harness). When remote vars
are absent, it performs Phase 1 only and blocks on a **rendezvous file**:

```
1. Create QP (RESET -> INIT)
2. Delete any stale local endpoint file (from prior run)
3. Write local endpoint to $LMCACHE_RDMA_ENDPOINT_FILE via atomic rename:
   write to .tmp, then os.rename() -> final path
4. Poll for peer endpoint file at $LMCACHE_RDMA_PEER_ENDPOINT_FILE
   (check every 100ms, timeout after 30s)
5. Validate peer file: nonce must match $LMCACHE_RDMA_NONCE
6. Read peer file -> connect() -> RTR -> RTS
7. Ready
```

**Stale file protection:**
- Each endpoint file includes a `nonce`, `pid`, and `timestamp` field.
- **Nonce semantics:** `LMCACHE_RDMA_NONCE` is required for rendezvous
  mode. `from_env()` raises ValueError if using rendezvous (remote vars
  absent) and nonce is not set. This forces the operator/harness to
  coordinate a shared nonce, preventing stale-file confusion.
- On read, the peer file's nonce is validated against `LMCACHE_RDMA_NONCE`.
  Mismatch → delete stale file, keep polling.
- Endpoint file format:
  ```json
  {"qpn": 42, "psn": 12345, "gid": "fe80...abcd", "lid": 1,
   "nonce": "a1b2c3d4", "pid": 99821, "timestamp": 1751622400.0}
  ```
- `from_env()` deletes its own endpoint file on startup (before write)
  and on `close()` (cleanup).
- Atomic write via temp file + `os.rename()` prevents partial reads.
- The helper script `scripts/ipu_bootstrap.py` generates a shared nonce
  and passes it to both processes.

**For two-node testing:** A helper script `scripts/ipu_bootstrap.py` runs
both processes with a shared nonce, copies endpoint files over SSH, and
signals readiness.

**For single-node testing (SoftRoCE / loopback):** Set the same
`LMCACHE_RDMA_NONCE` on both processes. Launch either order — each deletes
its own stale file on startup.

**Pre-configured mode:** If `LMCACHE_RDMA_REMOTE_QPN`, `LMCACHE_RDMA_REMOTE_PSN`,
and `LMCACHE_RDMA_REMOTE_GID` are all set, `from_env()` skips the file
rendezvous and connects immediately.


### QP State Machine — Full Required Fields

```
RESET ─── ibv_modify_qp(INIT) ──► INIT
    attr_mask: QP_STATE | PKEY_INDEX | PORT | ACCESS_FLAGS
    attrs:
      qp_state        = IBV_QPS_INIT
      pkey_index       = 0
      port_num         = <LMCACHE_RDMA_PORT>
      qp_access_flags  = IBV_ACCESS_REMOTE_READ | IBV_ACCESS_REMOTE_WRITE |
                         IBV_ACCESS_LOCAL_WRITE
                         (initiator adds REMOTE_READ | REMOTE_WRITE; target: LOCAL_WRITE only)

INIT ──── ibv_modify_qp(RTR) ───► RTR
    attr_mask: QP_STATE | AV | PATH_MTU | DEST_QPN | RQ_PSN |
               MAX_DEST_RD_ATOMIC | MIN_RNR_TIMER
    attrs:
      qp_state              = IBV_QPS_RTR
      path_mtu              = IBV_MTU_4096
      dest_qp_num           = <remote_qpn>
      rq_psn                = <remote_psn>
      max_dest_rd_atomic    = 4
      min_rnr_timer         = 12
      ah_attr:
        dlid        = <remote_lid>        (IB only; 0 for RoCE)
        sl          = 0
        src_path_bits = 0
        port_num    = <LMCACHE_RDMA_PORT>
        is_global   = 1                   (always for RoCE; IB if GID set)
        grh:
          dgid       = <remote_gid parsed to bytes>
          sgid_index = <LMCACHE_RDMA_GID_INDEX>
          hop_limit  = 64
          flow_label = 0
          traffic_class = 0

RTR ───── ibv_modify_qp(RTS) ───► RTS
    attr_mask: QP_STATE | SQ_PSN | MAX_QP_RD_ATOMIC | RETRY_CNT |
               RNR_RETRY | TIMEOUT
    attrs:
      qp_state         = IBV_QPS_RTS
      sq_psn           = <local_psn>
      max_rd_atomic    = 4
      retry_cnt        = 7
      rnr_retry        = 7
      timeout          = 14  (~ 4.096s)
```


### Method Behavior by Role

| Method | `target` | `initiator` |
|--------|----------|-------------|
| `register_mr` | ibv_reg_mr (LOCAL_WRITE) | ibv_reg_mr (REMOTE_READ \| REMOTE_WRITE \| LOCAL_WRITE) |
| `deregister_mr` | ibv_dereg_mr | ibv_dereg_mr |
| `post_read` | ibv_post_send(RDMA_READ) | raises RuntimeError |
| `post_write` | ibv_post_send(RDMA_WRITE) | raises RuntimeError |
| `poll_completion` | busy-poll ibv_poll_cq | raises RuntimeError |
| `allocate_buffer` | mmap + register_mr | mmap + register_mr |
| `free_buffer` | deregister + munmap | deregister + munmap |


### Access Flags by Role

- **Initiator** registers MRs with `REMOTE_READ | REMOTE_WRITE | LOCAL_WRITE`
  — REMOTE_WRITE grants the target permission to push data via
  `post_write` (LMCache-zbg) into the initiator's registered DRAM.
- **Target** registers local buffers with `LOCAL_WRITE` only (data lands
  here via the posted Read, or is sourced from here for the posted Write).


### Buffer Allocation and Ownership

**Issue 3 resolution:** `RegisteredBuffer` must retain the backing mmap
object to prevent GC and enable munmap on free.

**Issue 4 (medium) resolution:** The field is public `backing` (not
`_backing`) since `free_buffer()` in other classes accesses it. Typed as
a protocol with `close()`:

```python
class Closeable(Protocol):
    def close(self) -> None: ...

@dataclass(frozen=True)
class RegisteredBuffer:
    addr: int
    length: int
    mr: MrInfo
    backing: Closeable | None = None  # mmap or ctypes wrapper — prevents GC
```

The stub sets `backing` to a simple wrapper around the ctypes array (with a
no-op `close()`). Verbs sets it to the mmap object (whose `close()` does
munmap).

```python
def allocate_buffer(self, length: int) -> RegisteredBuffer:
    aligned = (length + 4095) & ~4095
    backing = mmap.mmap(-1, aligned, mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS,
                        mmap.PROT_READ | mmap.PROT_WRITE)
    addr = ctypes.addressof(ctypes.c_char.from_buffer(backing))
    mr = self.register_mr(addr, aligned)
    buf = RegisteredBuffer(addr=addr, length=aligned, mr=mr, backing=backing)
    self._allocated_buffers.add(buf)
    return buf

def release_buffer_tracking(self, buf: RegisteredBuffer) -> None:
    """Transfer buffer ownership from transport to caller (e.g., tensor).

    After this call, close() will NOT free the buffer — the caller is
    responsible via its own finalizer.
    """
    self._allocated_buffers.discard(buf)

def free_buffer(self, buf: RegisteredBuffer) -> None:
    self._allocated_buffers.discard(buf)
    self.deregister_mr(buf.mr)
    if buf.backing is not None:
        buf.backing.close()
```


### Completion Model — Lock-Serialized Post/Poll

**Concurrency contract:** The transport is process-global (`get_rdma_transport()`).
Multiple server threads may call `RdmaWrapper.to_tensor()` concurrently.
A `threading.Lock` serializes access to the single QP:

```python
self._qp_lock = threading.Lock()
```

`post_read()` acquires the lock and holds it until `poll_completion()`
returns (or timeout). This means concurrent callers block — acceptable for
POC where the QP is the bottleneck anyway. Production multi-QP striping
(LMCache-9he) removes this serialization.

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

        # Build and post SendWR with wr_id, IBV_WR_RDMA_READ
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

**Issue 6 resolution:** QP is created with `sq_sig_all=True` so every
posted WR generates a CQE. This avoids the need for explicit
`IBV_SEND_SIGNALED` per WR and ensures poll_completion never hangs.

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
                    # Stale/unexpected CQE — skip it, keep polling for ours
                    logger.warning("CQE wr_id=%d != expected %d (skipped)",
                                   wc.wr_id, self._inflight_wr_id)
                    continue
                success = (wc.status == IBV_WC_SUCCESS)
                self._inflight_future.set_complete(success=success)
                self._inflight_future = None
                return success
        # Timeout — do NOT clear _inflight_future (WR still in flight)
        return False
    finally:
        # Release lock ONLY on success or error-completion.
        # On timeout, lock stays held — caller must drain_on_timeout()
        # which releases it.
        if self._inflight_future is None:
            self._qp_lock.release()
```

**Lock lifecycle:**
- `post_read()` acquires `_qp_lock`
- `poll_completion()` releases on success/error CQE
- On timeout: lock stays held → caller calls `drain_on_timeout()` →
  drain releases lock after flushing
- This prevents a second caller from posting while a timed-out WR is
  still in flight


### Timeout Safety — Buffer Quarantine

**Issue 7 resolution:** If `poll_completion()` times out, the RDMA Read may
still be in flight — the HCA could DMA into the buffer at any point until
the QP is drained. Freeing the buffer would cause memory corruption.

**`drain_on_timeout()` is part of the `RdmaTransport` protocol.** It is
added as an optional method with a no-op default so all implementations
satisfy the interface:

```python
# In RdmaTransport protocol:
def drain_on_timeout(self) -> bool:
    """Drain after a timeout. Returns True if all WRs confirmed flushed."""
    ...

# StubRdmaTransport (no-op, always safe):
def drain_on_timeout(self) -> bool:
    return True
```

**VerbsRdmaTransport implementation:**

```python
def drain_on_timeout(self) -> bool:
    """Move QP to ERROR, poll CQ until inflight WR's flush CQE arrives.

    MUST be called while _qp_lock is held (i.e., after a timeout from
    poll_completion). Releases _qp_lock before returning.
    """
    try:
        # 1. Move QP to ERROR — flushes all outstanding WRs
        attr = QPAttr(qp_state=IBV_QPS_ERR)
        self._qp.modify(attr, mask=IBV_QP_STATE)

        # 2. Poll CQ until we see our inflight wr_id's error CQE
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
            logger.error("drain_on_timeout: WR wr_id=%d not flushed",
                         self._inflight_wr_id)
        return flushed
    finally:
        self._qp_lock.release()
```

**Key correctness property:** `drain_on_timeout()` returns True only when
the specific timed-out WR's flush CQE has been observed. Only then is the
buffer provably safe to free (HCA will not DMA into it).

**Call site in RdmaWrapper.to_tensor():**

```python
if not transport.poll_completion(future, timeout_ms=5000):
    flushed = transport.drain_on_timeout()
    if flushed:
        transport.free_buffer(buf)
    else:
        # Buffer is quarantined — leaked intentionally to avoid corruption.
        # Process should restart.
        logger.error("RDMA buffer quarantined (leak): addr=0x%x len=%d",
                     buf.addr, buf.length)
    raise RuntimeError("RDMA Read timed out ...")
```

**POC simplification:** After drain, the transport is dead (QP in ERROR).
The process must restart to re-establish the connection. Acceptable for
POC; production reconnection is LMCache-6c9.


### MR Deregistration Lifecycle

**Issue 10 resolution:** `RdmaWrapper.wrap()` registers MRs via the
global `_REGISTERED_MRS` cache in `rdma_wrapper.py`. With the stub, leaked
MRs are harmless (just memory). With verbs, leaked MRs pin physical pages.

**The fix** captures a deregistration callback at registration time, so the
finalizer uses the transport's own `deregister_mr()` logic (including any
bookkeeping, metrics, or idempotency guards) without calling
`get_rdma_transport()` at GC time.

**MrInfo gains an optional `deregister` callback:**

```python
@dataclass(frozen=True)
class MrInfo:
    rkey: int
    addr: int
    length: int
    handle: object
    deregister: Callable[[], None] | None = None  # captured at registration
```

Each transport sets `deregister` at registration time:

```python
# In VerbsRdmaTransport.register_mr():
mr_obj = MR(self._pd, length, access, address=buffer_ptr)
mr_info = MrInfo(
    rkey=mr_obj.rkey, addr=buffer_ptr, length=length,
    handle=mr_obj,
    deregister=lambda: self._do_deregister(mr_obj),
)

# In StubRdmaTransport.register_mr():
mr_info = MrInfo(
    rkey=rkey, addr=buffer_ptr, length=length,
    handle=None,
    deregister=lambda: self._registered.pop(rkey, None),
)
```

**Finalizer uses the captured callback with data_ptr race protection:**

```python
def _deregister_on_gc(data_ptr: int, mr: MrInfo) -> None:
    """Weak-reference finalizer: deregister MR when tensor is collected.

    Must be safe against data_ptr recycling: only pop the registry entry
    if it still refers to THIS mr (not a newer registration at the same ptr).
    """
    with _MR_LOCK:
        entry = _REGISTERED_MRS.get(data_ptr)
        if entry is not None:
            _, registered_mr = entry
            if registered_mr is mr:
                _REGISTERED_MRS.pop(data_ptr, None)
            # else: ptr was recycled, new tensor registered — leave it alone
    if mr.deregister is not None:
        try:
            mr.deregister()
        except Exception:
            pass  # Best-effort during interpreter shutdown
```

In `_get_or_register_mr`:

```python
# Stale entry (tensor was GC'd, ptr recycled) — finalizer may not have
# fired yet. Safe to pop because we hold _MR_LOCK.
if entry is not None:
    ref, old_mr = entry
    if ref() is not tensor:
        _REGISTERED_MRS.pop(data_ptr, None)
        # Don't deregister here — finalizer will call old_mr.deregister()

# After registering new MR:
transport = get_rdma_transport()
mr = transport.register_mr(data_ptr, nbytes)
_REGISTERED_MRS[data_ptr] = (weakref.ref(tensor), mr)
weakref.finalize(tensor, _deregister_on_gc, data_ptr, mr)
```

**Properties:**
- Finalizer acquires `_MR_LOCK` and verifies `registered_mr is mr` before
  popping — safe against data_ptr recycling
- Finalizer never calls `get_rdma_transport()` — safe at any lifecycle stage
- Each transport controls its own deregistration semantics via callback
- `try/except` handles interpreter shutdown ordering
- `deregister_mr(mr)` on the transport remains the explicit API; the
  callback is the GC fallback path

**Also fix `_release_buffer` (existing code):** The current
`_release_buffer` in `rdma_wrapper.py` calls `get_rdma_transport()` from a
weakref finalizer — same unsafe pattern. Apply the captured-callback fix:

```python
# In RdmaWrapper.to_tensor(), after successful RDMA Read:
transport.release_buffer_tracking(buf)  # transport won't free on close()
free_fn = transport.free_buffer  # capture at allocation time
weakref.finalize(storage, lambda: free_fn(buf))
```

This replaces the current `_release_buffer` + `_BUFFER_KEEP_ALIVE` pattern:
1. `release_buffer_tracking(buf)` — removes from `_allocated_buffers` so
   `close()` won't unmap memory backing a live tensor
2. Captured `free_fn` — doesn't depend on the global transport at
   finalization time


### Import Fallback

```python
try:
    from pyverbs.device import Context as VerbsContext
    from pyverbs.pd import PD
    from pyverbs.cq import CQ
    from pyverbs.qp import QP, QPInitAttr, QPAttr, QPCap
    from pyverbs.mr import MR
    from pyverbs.wr import SendWR, SGE
    from pyverbs.enums import (
        IBV_QPT_RC, IBV_QPS_INIT, IBV_QPS_RTR, IBV_QPS_RTS, IBV_QPS_ERR,
        IBV_WR_RDMA_READ, IBV_WR_RDMA_WRITE, IBV_WC_SUCCESS,
        IBV_ACCESS_LOCAL_WRITE, IBV_ACCESS_REMOTE_READ, IBV_ACCESS_REMOTE_WRITE,
        IBV_MTU_4096,
    )
    from pyverbs.addr import AHAttr, GlobalRoute
    HAS_PYVERBS = True
except ImportError:
    HAS_PYVERBS = False
```

`__init__` raises `ImportError("pyverbs (rdma-core) required ...")` if
`HAS_PYVERBS` is False.


### Wiring into get_rdma_transport()

In `rdma_transport.py`, the `get_rdma_transport()` factory gains one branch:

```python
elif backend == "verbs":
    from lmcache.v1.platform.rdma.verbs_transport import VerbsRdmaTransport
    _global_transport = VerbsRdmaTransport.from_env()
```


### Cleanup

`close()` acquires `_qp_lock` to ensure no concurrent operation is in
flight, then tears down in reverse order:

```python
def close(self) -> None:
    self._qp_lock.acquire()
    try:
        if self._closed:
            return  # already torn down
        self._closed = True
        # Deregister any tracked MRs (those allocated via allocate_buffer
        # that haven't been freed yet — ownership not transferred to tensor)
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
    # Delete endpoint file if we created one (idempotent, ignores ENOENT)
    self._cleanup_endpoint_file()
```

The transport tracks buffers allocated via `allocate_buffer()` in a set
(`_allocated_buffers`). `free_buffer()` removes from this set. This allows
`close()` to deregister remaining MRs before PD teardown (avoiding EBUSY).

`post_read()` checks `self._closed` after acquiring the lock and raises
`RuntimeError("transport is closed")`.

Called by `__del__` as safety net. Tests should call `close()` explicitly.


## Testing Strategy

1. **Unit tests (no hardware):** Mock `pyverbs` at the import boundary.
   Verify QP state transitions, role dispatch, access flag selection,
   error paths (timeout, bad WC status, missing env vars). Test public
   API only: `future.wait()` not internal `_success` field (issue 12).

2. **SoftRoCE integration (CI-possible):** If `rxe` kernel module is
   available, create a loopback device and run real verbs. Optional —
   not required for POC merge.

3. **Hardware validation:** On IPU nodes, run with
   `LMCACHE_RDMA_TRANSPORT=verbs` using the benchmark scenarios from
   `docs/design/tools/ipu_traffic_benchmarks/`.


## Success Criteria

- `VerbsRdmaTransport` passes unit tests with mocked pyverbs
- `get_rdma_transport()` returns `VerbsRdmaTransport` when
  `LMCACHE_RDMA_TRANSPORT=verbs` and pyverbs is available
- `LMCACHE_RDMA_ROLE` unset → immediate ValueError (fail fast)
- Initiator role: `register_mr` succeeds with REMOTE_READ \| REMOTE_WRITE,
  `post_read`/`post_write` raise
- Target role: full `post_read` → `poll_completion` → data landed flow
- Timeout path: QP transitions to ERROR, buffers safe to free after drain
- MR deregistration: tensor GC triggers deregister_mr
- Graceful `ImportError` when pyverbs not installed
- Unblocks 10 downstream issues
