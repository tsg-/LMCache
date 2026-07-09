# RDMA Transport Stack Rationale

**Audience:** Team members asking "why not just use NIXL/UCX directly?"
**Status:** Current as of ipu-poc branch (2026-07-09)

---

## Topology and Data-Flow Assumptions

All design decisions below assume this deployment topology:

```
G1  TPU/GPU compute node         G2  Xeon DRAM KV-cache offload node
┌──────────────────────┐         ┌──────────────────────────────────┐
│  HBM  (KV pages live │         │  Host DRAM  (L1 KV cache pool)   │
│  here during decode) │         │  NVMe SSDs  (G3 spill)           │
│                      │         │                                  │
│  IPU (CX7/RoCEv2)    │◄──────► │  IPU (CX7/RoCEv2)                │
└──────────────────────┘         └──────────────────────────────────┘
  RDMA source / target              RDMA initiator (pull model)
```

- **G1** exposes its HBM to RDMA: MR-registered, passive target.
- **G2** owns the IPU and drives all RDMA operations. It _pulls_ KV pages
  from G1 into its own DRAM (STORE), and _pushes_ them back to G1 HBM
  (RETRIEVE).
- **G2 has no GPU.** It is a Xeon-only node. GPU HBM is only on G1.
- **The IPU on each node is used as a dumb RoCEv2 NIC** — no ARM/Falcon
  core offload, no DPU processing. It provides hardware RDMA DMA and
  matches 400 Gb/s line rate; that is its entire role today.

---

## What Upstream `NixlChannel` Actually Provides

The upstream `dev` branch has `lmcache/v1/transfer_channel/nixl_channel.py`.
It is commonly referred to as "the NIXL connector." What it _actually_ does:

| Property | NixlChannel (upstream dev) |
|---|---|
| **Purpose** | P2P distributed L2 cache prefetch between two LMCache instances |
| **Buffer model** | One pre-allocated fixed arena per agent; registered once at startup |
| **Addressing** | Page-slot indices into the fixed arena (`get_local_mem_indices`) |
| **Operations** | `batched_write` / `batched_read` using `make_prepped_xfer` + prepped handles |
| **Async** | Yes — `async_batched_write/read` backed by asyncio |
| **STORE/RETRIEVE semantics** | Not implemented — `batched_send` and `batched_recv` raise `NotImplementedError` |
| **Per-tensor descriptors** | None — works with slot indices into a shared arena |
| **Agent metadata exchange** | Lazy per-peer handshake on first contact (`lazy_init_peer_connection`); arena registered at `__init__`, not per-tensor |
| **GPU tensor support** | Yes — VRAM path via UCX/GDR when `device="cuda:N"` |
| **Configurable backends** | Yes — UCX default; others possible via `backends` kwarg |

`NixlChannel` assumes two cooperating LMCache servers that have agreed on a
shared memory arena layout. It cannot be retargeted to arbitrary tensor
addresses at STORE/RETRIEVE time. Using it for vLLM KV-cache movement would
require rewriting its addressing model from scratch — at which point you are
writing what `NixlTransferModule` already is.

---

## Why We Need Each New Module

### `nixl_wrapper.py` — per-tensor NIXL descriptor

**What it adds:** Registers an arbitrary tensor (CPU or GPU, any address,
any shape) with a process-level NIXL agent and captures the full descriptor
needed for a one-shot cross-host transfer:

```python
(agent_name, agent_metadata, base_addr, length, device_id, mem_type, shape, dtype, stride, storage_offset)
```

`NixlChannel`'s `NixlAgentWrapper` registers one fixed arena at startup.
`NixlWrapper.wrap(tensor)` registers any tensor on demand. There is no
upstream equivalent that handles arbitrary per-request tensor addresses.

Sets `mem_type = "VRAM"` when `tensor.get_device() >= 0`, `"DRAM"` otherwise.
This is the only place GPU tensor awareness enters our NIXL path.

**Assumption:** The NIXL agent runs in the same process as vLLM. For
multi-process vLLM deployments the agent must be in the worker process that
owns the tensor memory.

---

### `nixl_transfer.py` (module) — server-side STORE/RETRIEVE over NIXL

**What it adds:** Handles STORE and RETRIEVE requests in the LMCache
multiprocess server using NIXL for the actual data movement:

- STORE: server calls `initialize_xfer("READ", ...)` — G2 pulls from G1
- RETRIEVE: server calls `initialize_xfer("WRITE", ...)` — G2 pushes to G1

Per-call `register_memory` + `initialize_xfer` + poll + deregister. This is
correct for the IPU path: transfers are non-recurring (each token window is a
different tensor), so `make_prepped_xfer` is not appropriate. The deeper
reason `make_prepped_xfer` is unsafe here: `prep_xfer_dlist` bakes UCX
endpoint state into the handle. If the worker's UCX endpoint is
garbage-collected or reconnected between requests, the cached handle
references stale state and the transfer stalls indefinitely in `PROC` status.
`initialize_xfer` recreates the handle from live descriptors each call,
avoiding this. `NixlChannel` uses `make_prepped_xfer` for its fixed arena
precisely because the addresses and endpoints never change.

`NixlTransferModule` initializes its agent with `NIXL_THREAD_SYNC_STRICT`
to prevent internal state races when concurrent AFFINITY-pool threads call
`_run_xfer` simultaneously. This serializes NIXL's internal state operations;
throughput under high concurrency is bounded by this lock.

**No upstream equivalent.** `NixlChannel` has no STORE/RETRIEVE handler.

---

### `nixl_transfer.py` (transfer context) — worker-side vLLM adapter

**What it adds:** The vLLM-side `TransferContext` that serializes a
`NixlWrapper` descriptor and submits it to the MessageQueue for the server
to consume. This is the glue between vLLM's engine and the NIXL server
module.

`NixlChannel` has no `TransferContext` because it is not used from vLLM's
engine path — it is used for cache-to-cache P2P, not engine-to-cache.

**No upstream equivalent.**

---

### `rdma_transport.py` + `rdma_wrapper.py` + `verbs_transport.py` — pyverbs path

**Why keep this when the IPU is just a NIC?**

This is the most common objection. The answer has three parts.

**1. NIXL/UCX is not universally available.**  
UCX requires specific driver versions, NIXL is installed via `pip install nixl`
(the package exposes `nixl._api`, `nixl_cu12._api`, and `nixl_cu13._api` —
the `cu12`/`cu13`-suffixed imports are legacy aliases in the same wheel), and
both require a working UCX transport plugin for the NIC vendor in question. The pyverbs path requires only `libibverbs` + the RDMA kernel
modules, which are available on any RDMA-capable host. Having a fallback that
works without NIXL installed is the difference between "PoC only runs on
bmg0/bmg1" and "PoC can be validated on any RDMA node."

**2. Benchmarking requires an independent baseline.**  
Our measurements (recorded in `test_report.md` and `rdma_bench_runner.py`)
show VerbsRdmaTransport at **2.59 ms p50** vs NIXL/UCX at **3.63 ms p50**
for equivalent warm-path transfers. Without a pyverbs reference we cannot
tell whether NIXL overhead is acceptable or whether a UCX configuration
problem is hiding latency. Two independent paths with a documented cost
difference are an engineering artifact, not a redundancy.

**3. IPU ARM core offload will require the pyverbs path as a fallback.**  
Covered in the ARM offload section below.

---

### `gid_resolver.py` — automatic GID from IP address

Removes the need to pre-set `LMCACHE_RDMA_REMOTE_GID` in every test script
by scanning `/sys/class/infiniband/<dev>/ports/<port>/gids/` for the
RoCEv2 GID that corresponds to a given IP. Operationally the same as
`ibv_query_gid` + IP match, but without spawning a subprocess.

**Deferrable:** Setting `LMCACHE_RDMA_REMOTE_GID` manually bypasses this
entirely. The module is tested standalone (`test_gid_resolver.py`) with no
RDMA hardware required.

---

### `gpudirect.py` — NIC-to-GPU HBM direct write (pyverbs path only)

**What GPUDirect is in this context:**  
When the *local* node has a GPU, `gpudirect.py` allocates a CUDA device buffer
and registers it as an ibverbs MR. An RDMA operation posted with that buffer
as `local_buf` causes the NIC to DMA directly to GPU HBM via PCIe peer-to-peer
— no CPU DRAM hop. This is GPUDirect RDMA (GDR).

**Does this apply to G2 (cache offload node)?**  
No. G2 is Xeon-only. `is_gpudirect_available()` returns False because
`torch.cuda.is_available()` is False on G2. The module is inert in the current
deployment.

**Does this apply to G1 (TPU/GPU node)?**  
G1 does not run the LMCache server process — it is the initiator/client. It
only needs to register its HBM as an MR and announce the rkey/address to G2.
That registration happens through the RDMA transport on G1's side; `gpudirect.py`
is not involved.

`gpudirect.py` would apply if G2 were also a GPU node (e.g., a GPU-accelerated
cache tier). It is a latent capability, not a current requirement. Off by default
(`LMCACHE_RDMA_GPUDIRECT` unset).

**Does the NIXL path need an equivalent?**  
No. When `NixlWrapper.wrap(tensor)` wraps a GPU tensor, it sets
`mem_type = "VRAM"` and NIXL/UCX handles GPUDirect registration transparently
through its built-in GDR support. The NIXL path is GPU-aware out of the box on
both ends of the transfer — no `gpudirect.py` analogue is needed.

---

### `server.py` + `config.py` wiring — routing "rdma" and "nixl" modes

Eight lines in `server.py`, four in `config.py`. These expose
`--transfer-mode rdma` and `--transfer-mode nixl` as first-class CLI choices,
which is how all existing transfer modes (lmcache_driven, engine_driven) are
wired. No abstraction, no new class hierarchy — just a routing table.

---

## Performance Features: Which Apply Where

| Feature | pyverbs path | NIXL path |
|---|---|---|
| Multi-QP striping (9he) | Yes — `LMCACHE_RDMA_QP_COUNT` *(feature branch only)* | N/A — NIXL manages transport concurrency internally |
| Event-driven CQ (kyc) | Yes — `LMCACHE_RDMA_CQ_MODE=event` *(feature branch only)* | N/A — poll in `check_xfer_state()` |
| Busy-poll CQ | Yes — `LMCACHE_RDMA_CQ_MODE=poll` (default, on `ipu-poc`) | N/A |
| Pre-registered MR buffer pool (br4) | Yes — `_MrBufferPool`, 256 MB arena (on `ipu-poc`) | Not yet — per-call reg/dereg; LMCache-o25 planned |
| GPUDirect RDMA | Yes — `gpudirect.py`, off by default *(feature branch only)* | Transparent via `mem_type="VRAM"` |
| Zero-copy CPU path | Yes — MR over host DRAM, no memcpy | Yes — UCX handles DMA |
| VRAM source/target | Requires explicit MR + GPUDirect | Automatic via `mem_type="VRAM"` |

**Default configuration for PoC:** busy-poll CQ, single QP, no GPUDirect
(all controlled by env vars). Multi-QP (`QP_COUNT >= 2`) is the first knob to
turn for throughput on a 400 Gb/s link since a single RC QP saturates at
roughly 100 Gb/s under small-message workloads.

---

## How the Current Work Enables IPU ARM Core Offload

Today the IPU is a dumb RoCEv2 NIC. The Falcon/ARM cores on the IPU are
unused. If they are brought online as a DPU (offload processing engine), the
change required in our stack is:

**Minimal change — NIXL path (hypothetical):**  
`NixlTransferModule._run_xfer` would call `add_remote_agent(arm_agent_name, ...)`
(not yet implemented — the current interface takes `agent_name` from the
`NixlWrapper` descriptor).
The ARM-side would run a NIXL daemon that accepts `initialize_xfer` requests
and executes DMA through its onboard network engine. Our G2 CPU is no longer
in the data-plane loop. The `NixlWrapper` descriptor format, the ZMQ protocol,
and the `NixlTransferContext` on G1 are all unchanged.

**Minimal change — pyverbs path:**  
The ARM cores would be exposed as a separate ibverbs device or a privileged
QP alias. `VerbsRdmaTransport` would target that device name instead of
`mlx5_0`. Alternatively `post_read`/`post_write` could be offloaded via the
IPU vendor SDK while keeping the same `RdmaTransport` protocol. Either way,
the protocol boundary (`register_mr`, `post_read`, `post_write`,
`poll_completion`) is unchanged — the ARM offload is behind it.

**The critical enabling factor is the protocol abstraction.**  
`RdmaTransport` is a `@runtime_checkable` Protocol with nine methods
(`register_mr`, `deregister_mr`, `post_read`, `post_write`,
`poll_completion`, `allocate_buffer`, `free_buffer`,
`release_buffer_tracking`, `drain_on_timeout`). `VerbsRdmaTransport` is one
implementation. An ARM-offload implementation — call it `FalconRdmaTransport`
— would be another, with no changes anywhere else in the stack. This
separation was the rationale for the Protocol boundary, not over-engineering
for its own sake.

**What would be genuinely new work for ARM offload:**
- G1-side registration of HBM with the IPU's ARM-accessible MR table (may
  require vendor SDK, not standard ibverbs)
- Synchronization between the ARM engine and the host CPU for completion
  notification
- A NIXL backend plugin for Falcon cores, if the NIXL path is used for
  offload (NIXL supports custom backends via its plugin interface)

None of these affect the modules built so far.

---

## Answering "Why Not Just Use NIXL?"

The short answer: we are. The NIXL path is fully implemented and is the
recommended mode for production (`--transfer-mode nixl`). The pyverbs path
exists for three concrete reasons:

1. **Availability.** pyverbs + libibverbs is present on any RDMA node.
   NIXL requires UCX, vendor plugins, and `nixl_cu12`. The fallback matters
   for bring-up on hardware we do not control.

2. **Reference baseline.** VerbsRdmaTransport is a ~740-line, single-purpose
   ibverbs implementation with no external UCX dependency. It provides a
   latency floor measurement independent of NIXL/UCX internals. The 28%
   overhead measured on bmg0 is data, not waste.

3. **ARM offload path.** The pyverbs `RdmaTransport` Protocol is the interface
   an ARM-offload implementation would satisfy. Deleting pyverbs would delete
   the abstraction boundary that makes Falcon offload a drop-in replacement
   rather than a fork.

The NIXL path handles GPU tensors transparently, requires no `gpudirect.py`
equivalent, and is simpler to operate at scale. The pyverbs path handles
environments where NIXL is unavailable and provides an independent performance
reference. The server-side module for each path is ~500 lines (`NixlTransferModule`: 529;
`RdmaTransferModule`: 475). The combined addition across all new source files
on `ipu-poc` is roughly 3,000 lines, including transport abstractions and
worker-side transfer contexts. Neither path is redundant given the deployment
and bring-up requirements above.
