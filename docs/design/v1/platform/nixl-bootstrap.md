# NIXL Connection Bootstrap

**Module:** `lmcache/v1/platform/rdma/nixl_wrapper.py`,
`lmcache/v1/multiprocess/modules/nixl_transfer.py`

**Related:** [verbs-transport.md](verbs-transport.md),
[ipu/transport-stack-rationale-pyverbs_nixl.md](ipu/transport-stack-rationale-pyverbs_nixl.md)

---

## Overview

NIXL uses UCX as its network transport. UCX requires each communicating
process to know the other's endpoint metadata before data can be moved.
`VerbsRdmaTransport` solves this with an explicit two-phase QP exchange
(write endpoint file → poll peer file → modify QP to RTS). NIXL removes
that explicit exchange: the initiator piggybacks its `agent_metadata`
onto every STORE/RETRIEVE control message, and the server calls
`add_remote_agent` lazily on first use.

---

## Bootstrap Sequence

```
Initiator (worker / thin client)          Server (NixlTransferModule)
─────────────────────────────────         ────────────────────────────
get_nixl_agent()                          __init__:
  nixl_agent("lmcache_worker_<pid>",        nixl_agent("lmcache_server_<pid>_<id>",
    nixl_agent_config(backends=["UCX"]))      nixl_agent_config(backends=["UCX"],
                                               sync_mode=NIXL_THREAD_SYNC_STRICT))

NixlWrapper.wrap(tensor):
  _ensure_registered(tensor)             [server is idle]
  get_new_notifs()  ← flush UCX
  agent.get_agent_metadata() → bytes

ZMQ STORE/RETRIEVE payload:
  [key, pid, [], pickle(NixlWrapper)]
──────────────────────────────────────────────────────────────────────>
                                          store() / retrieve():
                                            wrapper = Deserialize(payload)
                                            add_remote_agent(wrapper.agent_metadata)
                                              ↑ idempotent; registers UCX endpoint
                                              once per (agent_name, process)

                                            initialize_xfer(op, local_dlist,
                                              remote_dlist, wrapper.agent_name)
                                            transfer(xfer_handle)
                                            poll check_xfer_state → DONE

ZMQ response: (b"", ok)
<──────────────────────────────────────────────────────────────────────
```

### Key design choices

| Decision | Reason |
|---|---|
| `agent_metadata` embedded in every request | Stateless server restarts without re-handshake; initiator process restarts are handled automatically |
| `add_remote_agent` is idempotent | Server calls it unconditionally; NIXL deduplicates by agent name |
| `initialize_xfer` (one-shot) not `prep_xfer_dlist` + `make_prepped_xfer` | The prep-based path caches a prepared dlist handle; on the second cross-process request the remote UCX endpoint state may have changed, causing a stall |
| Server uses `NIXL_THREAD_SYNC_STRICT` | `NixlTransferModule` runs on an `AFFINITY` thread pool; strict sync prevents concurrent NIXL internal state races |
| Initiator uses no `enable_listen_thread` flag | The initiator (worker or thin client) is purely a target of server-initiated transfers. `get_nixl_agent()` creates the agent with default config; no listen thread is started because the worker process does not initiate NIXL connections to anyone |

---

## Initiator Agent Lifecycle

`get_nixl_agent()` in `nixl_wrapper.py` creates exactly one `nixl_agent`
per process. The singleton is protected by `_AGENT_LOCK`. The agent name
is `lmcache_worker_<pid>` — unique per process even under multiprocessing
with `spawn`, ensuring no name collisions on the server's remote-agent
registry.

Memory registrations are cached in `_REG_PTRS` (keyed by `tensor.data_ptr()`).
A `weakref.finalize` callback deregisters the MR when the tensor is
garbage-collected. Stale registrations (same address, different tensor)
are removed synchronously before the new registration to avoid
double-registration at the same address.

---

## Server Agent Lifecycle

`NixlTransferModule.__init__` creates one `nixl_agent` per module
instance (i.e., per `MPCacheServer` process). Remote agents are never
explicitly removed — NIXL manages endpoint state internally.

Per-chunk, the server:

1. Calls `agent.get_reg_descs([(local_ptr, chunk_len, 0, "")], mem_type)`
   and `agent.register_memory(...)` to pin the local buffer.
2. Builds a 1-entry local xfer dlist and a 1-entry remote xfer dlist from
   `wrapper.base_addr + i * chunk_length`.
3. Calls `initialize_xfer(operation, local_dlist, remote_dlist, agent_name)`.
4. Polls `check_xfer_state` at 100 µs intervals until `"DONE"` or timeout
   (15 s).
5. Releases the xfer handle and deregisters the local MR in a `finally`
   block regardless of success.

---

## Contrast with VerbsRdmaTransport Bootstrap

| | VerbsRdmaTransport | NixlTransferModule |
|---|---|---|
| Out-of-band exchange | QP endpoint file read/write | `agent_metadata` in ZMQ payload |
| Timing | Must complete before first transfer | Lazy, on first STORE/RETRIEVE |
| Connection state | RC QP in RTS state | UCX manages internally |
| Restart recovery | New QP + file exchange required | New `agent_metadata` in next request |
| Memory registration | Per-transfer MR (or pool) | Per-chunk server-side; initiator uses lazy cache |

---

## Testing

- `tests/v1/multiprocess/test_nixl_thin_client.py` — e2e STORE/RETRIEVE
  against a real `MPCacheServer` subprocess (`supported_transfer_mode="nixl"`).
  Includes `TestRdmaThinClientWithNixl` which validates the full path through
  `RdmaThinClient(wrapper_cls=NixlWrapper)`.
- `tests/v1/platform/test_nixl_wrapper.py` — unit tests for `NixlWrapper.wrap`,
  MR caching, and GC finalizer.
