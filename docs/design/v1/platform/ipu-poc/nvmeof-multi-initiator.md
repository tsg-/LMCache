# Multi-Initiator LMCache-over-NVMe/Falcon — Dedup and Synchronization

## Scope

Companion to `nvmeof-initiator-only-alternative.md` and to the parent
plan `nvmeof-poc-plan.md`. That plan's §6.1 keeps multi-initiator
shared namespaces out of scope for the PoC, and §11 (Future Work)
explicitly parks "shared allocator, lease/ownership protocol,
mapping authority" as future distributed-systems work.

Shared L2 across initiators is a **product requirement**, not an
optional optimization: sole-tenant namespaces multiply capacity by
`N`, strand each initiator's L2 on crash, and turn cross-initiator
key overlap into duplicate prefill work. This note therefore does
three things:

1. Locks in **Option A (sole-tenant target per initiator)** as the
   **M0 baseline** on today's code — the only path that ships without
   new infrastructure.
2. Specifies **Option B1** (target-co-located metadata authority) as
   the follow-on shared-L2 target — an *A-plus* design where the
   target Xeon becomes authoritative for namespace metadata (lookup,
   conditional create, allocation, GC, replay) on the control path,
   while payload I/O remains standard NVMe-oF. Includes the concrete
   gate list a B1 delivery must clear.
3. Enumerates B2/B3/B4 as evaluated alternatives with their proof
   obligations, and scopes Option C as a read-side P2P hint that
   layers on A or B1.

Out of scope: N × M fabrics, cross-target replication, multi-tenant
isolation between competing models, and any B1 variant that puts
the metadata authority in a distributed control plane rather than
a single-node target-side daemon.

## What dedup is, and is not

Dedup in LMCache is a property of the key, not the storage layer. Two
initiators writing the same content produce the **same**
`CacheEngineKey` (`lmcache/utils.py:399`), which is
`model @ world_size @ worker_id @ chunk_hash @ dtype [@ layer_id]
 [@ tag%val...]`, with `chunk_hash` a prefix-chained hash over
`(prev_hash, token_ids, extra_keys)`. Given the same tokenizer,
model, world size, TP rank, and `extra_keys`, the key is
deterministic across instances.

Dedup benefit is bounded by the following invariants — all misses
that no synchronization layer repairs:

- **worker_id partitions the namespace.** TP ranks do not share;
  only MLA-style models with `save_only_first_rank=True` collapse
  `world_size → 1`.
- **Tokenizer or chat-template drift produces silent misses.**
- **`extra_keys` partitions the namespace** (LoRA id, session tag,
  multi-modal hashes).
- **`chunk_hash` is prefix-chained.** One differing chunk shifts
  every subsequent chunk key on that sequence.

Any cross-initiator sharing therefore assumes a **homogeneous
compatibility domain**: same model, same tokenizer, same TP layout,
same `extra_keys` policy. Where that assumption breaks, dedup is
zero regardless of the storage or directory design below.

## The three synchronization surfaces LMCache already exposes

Loose → tight, all in-tree today:

1. **Shared L2 backend.** Redis / Mooncake / Infinistore / NIXL /
   S3 / DAX / raw_block. Some backends (e.g., Redis) are designed
   for concurrent writers; some are not. `raw_block` is not — see
   the P0 constraint in §4.1 below.
2. **Cache Controller** (`lmcache/v1/cache_controller/`, ZMQ
   PULL + ROUTER). Fleet-wide directory. Instances push
   `BatchedKVOperationMsg` incrementally and reconcile via
   `FullSyncStart/Batch/End`. Eventually consistent. Populated
   **after** admit/evict — not on the critical path of a store.
3. **P2P backend** (`lmcache/v1/storage_backend/p2p_backend.py`).
   Uses the controller to find peers holding a chunk in L1 and
   pulls directly. Indexes lookups on `chunk_hash` only
   (`lmcache/v1/storage_backend/p2p_backend.py:291`), not the full
   `CacheEngineKey`.

`MP Coordinator` (FastAPI REST) manages membership only. The KV
directory lives in the ZMQ controller. Do not confuse the two.

## 4. Options

### 4.1 Option A — Sole-tenant target per initiator (M0 baseline only)

Each initiator owns its own NVMe namespace on the storage node.
Zero cross-initiator state.

- **Dedup across initiators:** none. Two initiators writing
  identical chunks each burn LBAs on their own namespace.
- **Consistency:** single-writer per namespace. The WAL/COW
  publication contract in `nvmeof-initiator-only-alternative.md`
  §3 holds unchanged, per initiator.
- **Storage backend:** matches how `RawBlockCore` is actually
  built. `lmcache/v1/storage_backend/raw_block/core.py` holds
  its index, allocator cursor, free-slot list, inflight table,
  and checkpoint state in a single `threading.Lock`-guarded
  process-local structure (`core.py:303`). It is engineered for
  one owning process per underlying block device. Sole-tenant is
  the configuration this code was written for.
- **Failure domain:** initiator crash affects only its namespace;
  local WAL replay recovers. Target crash makes L2 unavailable to
  all initiators; each degrades to L1-only.
- **Cost:** N namespaces × per-initiator L2 capacity. The SSD
  wastage is the price paid for keeping the initiator-owned
  design's *no distributed metadata service* property, which
  `nvmeof-poc-plan.md` §11 identifies as the reason the PoC scope
  is what it is.

This is the **M0 baseline only**. It matches the parent plan's §6.1
scope row ("one initiator with exclusive ownership of two unused
namespaces (one per SSD)") and does not require any component the
plan defers. It is not a shipping shared-L2 design — capacity scales
`N×`, an initiator crash strands its L2, and cross-initiator key
overlap turns into duplicate prefill work. The follow-on target is
Option B1 below.

### 4.2 Shared-L2 design space (post-M0)

Shared L2 is a spectrum, not a binary choice against Option A.
The variants below differ in *where the namespace-metadata
authority lives* and *what it costs to keep it consistent*. All
of them are ruled out on today's `raw_block` for the same reason
(single-writer index / allocator / checkpoint under one
`threading.Lock` at `core.py:303`, `core.py:614`); the design
choice is which authority replaces it, not whether one is
needed.

Four variants, ordered by the recommendation below:

- **B1 — Target-co-located metadata authority** *(recommended
  follow-on; "A-plus")*
- **B2 — Content-addressed LBA with on-disk collision protocol**
- **B3 — Sharded key ownership across initiators**
- **B4 — Read-shared, write-private**

#### 4.2.1 B1 — Target-co-located metadata authority (A-plus)

A single-node metadata daemon runs on the target Xeon,
co-located with `nvmet-rdma`. It is authoritative for namespace
metadata: `lookup`, `conditional-create`, extent allocation,
generation numbers, checksums, deletion, and GC. **Payload I/O
remains standard NVMe-oF** — initiators issue NVMe-oF READ/WRITE
against target namespaces exactly as in Option A, with the flows
in `architecture-a-nvmeof-falcon-{store,retrieve}-flow.puml`
unchanged for the data path.

**Framing.** This is not "Option A with a daemon attached." The
target is on the control path for every lookup and create. Call
it what it is: **A-plus**, where the target owns cache metadata
decisions but stays passive for data movement and payload
interpretation. Availability boundary matches the target itself
— which the parent plan already treats as a hard failure
boundary — so no new failure domain is introduced.

**Placement of the metadata WAL.** A **dedicated logical
namespace on the same target failure domain**, not necessarily a
separate physical device. Rationale: isolation and operational
boundary (independent LBA space, independent GC, independent
retention) without adding a durability domain the target does
not already own. The daemon writes *intent* and *commit* records
to this metadata namespace; payload and checksum land in data
namespaces first with their required durability barrier; **only
then** may the metadata commit become durable and visible.

There is no cross-namespace atomic write. Recovery must
therefore be WAL-driven and **fail closed**: a committed
metadata record whose data or checksum cannot be verified on
replay is **discarded, never served**. The metadata namespace is
an isolation and operational boundary, not a substitute for
write ordering or read-time validation.

**Identity on every RPC.** Initiator↔daemon protocol uses the
**full canonical key** on every `lookup` and `conditional-create`
— specifically the `ObjectKey` wire form
(`lmcache/v1/distributed/api.py:57`: `chunk_hash`, `model_name`,
`kv_rank`, `object_group_id`, `cache_salt`). Legacy
`CacheEngineKey` callers (`lmcache/utils.py:402`) normalize to
`ObjectKey` before the wire. Bare `chunk_hash` — what
`BatchedP2PLookupMsg` carries today
(`lmcache/v1/storage_backend/p2p_backend.py:284`) — is **not
sufficient** as the correctness identity for B1.

Every request also carries a **compatibility-domain ID**: a
versioned tuple covering model revision, tokenizer/template, KV
layout, dtype, key-hash scheme, and serialization version. Two
requests with different domain IDs must not resolve to the same
extent even if their `ObjectKey` byte-form collides. A compact
fingerprint may become an internal session-bound handle later,
but never the sole correctness identity.

**Gate list — B1 delivery must clear all four:**

1. **Atomic conditional create.** A single daemon RPC that
   either publishes the (canonical-key, extent, checksum,
   generation) tuple or returns an existing one; no split state
   under concurrent creates from N initiators.
2. **Full `ObjectKey` identity plus compatibility-domain ID on
   every lookup and create.** Bare `chunk_hash` is a P2P hint,
   not an L2 correctness identity.
3. **WAL/replay across daemon-and-initiator crashes.** Explicit
   ordering: payload durable → checksum durable → metadata
   intent → metadata commit. Any tuple whose data or checksum
   cannot be verified on replay is discarded, never served.
   Behavior specified for at least: daemon crash mid-commit;
   initiator crash after data-write ack but before metadata
   commit; simultaneous crash of daemon and one initiator.
4. **Measured overlap gates.** Two dedup-benefit runs: 0% key
   overlap (worst case for shared L2 — pure metadata overhead
   with no dedup win) and 100% overlap (best case — one physical
   copy for N initiators). Report capacity and prefill-work
   deltas versus Option A at the same initiator count.

Costs to accept: the target Xeon is now on the control path for
every store and every lookup; daemon latency budget adds to L2
store/retrieve; the metadata namespace consumes durable
capacity and its own GC cycles; upgrade and rollback of the
daemon are now operational concerns.

#### 4.2.2 B2 — Content-addressed LBA with on-disk collision protocol

`LBA = f(canonical-key)` with linear probing on collision. Two
initiators writing the same key hit the same LBA; last-writer-
wins is safe **only** because the bytes are identical under the
same compatibility-domain ID.

Not allocator-free. Real collision resolution requires an atomic
claim/probe record, full key identity in each slot (so probe
lookups verify the occupant is *this* key), generation handling,
deletion/GC, and recovery. That is a metadata authority
**embedded on disk** rather than in a daemon. Ordering and
validation obligations are the same as B1; only the location of
the authority differs.

Attractive when the wire-protocol cost of a daemon RPC is
prohibitive. Costs: load-factor sensitivity forces
over-provisioning; on-disk collision directory competes with
payload for LBA space; recovery must reconstruct the claim log,
which is effectively re-implementing B1's WAL on the data
namespace.

#### 4.2.3 B3 — Sharded key ownership

`hash(canonical-key) → owner-initiator`. Writes route to owner
(one-hop penalty on stores); reads either route to owner or use
Option C as a hint. Consistent hashing keeps rebalancing bounded.

Not simpler than B1. Owner routing creates membership,
owner-failure detection, reassignment, and durable handoff
requirements. Every remote write becomes a protocol operation —
including its own conditional-create, replay, and idempotency
semantics — because the owner initiator is now the mini
metadata authority for its shard. Failure boundary is worse
than B1: an owner initiator's crash strands its shard's
metadata until reassignment completes.

#### 4.2.4 B4 — Read-shared, write-private

Each initiator writes its own namespace (Option A on the write
path); all initiators read all namespaces on miss. Duplicate
writes remain; capacity is still `N×`. Read hit rate improves
fleet-wide.

Not zero-infrastructure. A reader still needs to know which
private namespace contains a key and its extent. That requires
either (a) a directory service (which is B1's daemon minus the
create path, so most of the cost with less of the benefit), or
(b) a deterministic per-namespace placement contract plus
recovery semantics for when a namespace goes away. Recovery
matters because a reader's *own* recovery is no longer
sufficient — it must know whether a peer's namespace is
consistent.

Lower risk than B1 in the write path (no shared-writer
metadata), higher risk than it looks on the read path. Useful
mainly as a stepping-stone if B1's daemon lands first as a
read-only directory before it gets the conditional-create
path.

#### 4.2.5 Summary

| Variant | Metadata authority | Dedup writes | Capacity | Buildable on today's code | Recommendation |
|---|---|---|---|---|---|
| A       | none (per-initiator) | no  | `N×` | yes                | M0 baseline |
| B1      | target-side daemon   | yes | `1×` | no — new daemon    | **Follow-on target** |
| B2      | on-disk protocol     | yes | `1×` | no — new backend   | Alternative if daemon RPC is too costly |
| B3      | per-shard initiators | yes | `1×` | no — new routing   | Alternative; worse failure boundary |
| B4      | directory or contract| no  | `N×` | no — directory req'd | Stepping-stone only |

### 4.3 Option C — Read-side P2P hint (layerable on Option A)

The Cache Controller directory plus `P2PBackend` can be enabled
on top of Option A as a **best-effort read optimization**, with
the correctness and admit path unchanged. This is the only piece
of the multi-initiator picture that composes with today's code
without new infrastructure.

Precise scope:

- **Read side only.** On a local L1 miss, an initiator may
  consult the controller to find a peer holding the chunk in L1
  and pull via P2P, rather than fetching from its own L2.
- **Not on the write critical path.** The controller is
  populated by `push_msg` calls issued **after** an admit or
  evict completes (see
  `lmcache/v1/cache_controller/controllers/kv_controller.py`
  around the `BatchedKVOperationMsg` handling path). It cannot
  atomically claim a key before an L2 write, and does not
  prevent duplicate NVMe stores. Any "avoid a duplicate write"
  claim overstates the mechanism.
- **Lookup indexes `chunk_hash`, not the full key.**
  `BatchedP2PLookupMsg` carries only chunk hashes
  (`lmcache/v1/storage_backend/p2p_backend.py:291`). Full-key
  discrimination (model, world_size, worker_id, dtype, tags —
  see `lmcache/utils.py` around `CacheEngineKey` at line 423)
  must be enforced by the deployment: run only within a
  homogeneous compatibility domain (§What dedup is, and is not).
  Crossing domains under Option C risks returning a peer's
  chunk that shares `chunk_hash` but belongs to a different
  model or TP rank.
- **Consistency:** eventually-consistent hint. Stale directory
  entries degrade to a local L2 fetch, never to wrong data —
  provided the compatibility-domain constraint above is
  enforced.
- **Failure domain:** controller crash disables cross-initiator
  discovery; each initiator falls back to L1 + local L2. No
  data loss. Full-sync repopulates on restart.

Option C is worthwhile if measurement shows it is worthwhile.
See §5 for the gating protocol; it is not a scheduled milestone.

## 5. Rollout protocol

**PoC (in scope, `nvmeof-poc-plan.md` §6.1):** Option A only. One
initiator per namespace, one namespace per SSD. This is what the
parent plan already scopes; this note does not add work.

**Post-PoC exploration of Option C:** enable only after measuring
a meaningful cross-initiator L1 warm-set overlap in a
homogeneous-domain deployment. Concretely:

- Run the sole-tenant baseline (2 and 4 initiators × own
  namespace, identical workload, identical compatibility
  domain).
- Compute the *achievable* peer-warm hit rate: for each L1 miss
  on initiator X, would a peer's L1 have satisfied it? This is
  a post-processing pass over the run's access logs, not a new
  fabric configuration.
- Gate C rollout on that measured rate exceeding a threshold
  set by the operator. Do not commit to a fleet-size threshold
  in advance; the answer depends on workload overlap and L1
  sizing, not on initiator count.

**Option B1 (follow-on target):** design and deliver the
target-side metadata daemon against the four-item gate list in
§4.2.1. Not on the M0 milestone; is the M1 milestone for
shared L2. B2/B3/B4 are evaluated in the same doc when B1 is
specified, not before — evaluating alternatives against a
strawman is cheaper than against nothing.

## 6. MkP functional proving ground for the 4x400 target

The future 4x400 GbE platform needs aggregate traffic from multiple
initiator nodes and, potentially, multiple LMCache instances per node.
That hardware is not available. The mkp1↔mkp2 100 GbE Falcon-backed
NVMe-oF environment is therefore a **functional proving ground**, not
a scaled bandwidth model: it can validate process topology, isolation,
and control-plane behavior before 4x400 hardware arrives, but cannot
establish 1.6 Tb/s throughput, storage sizing, or cross-node fan-in.

There are two deliberately separate MkP tests:

1. **Traffic generation.** Prepopulate a unique `fs_native` key prefix,
   then run one or more `lmcache bench l2 --only load` processes against
   that read-only keyspace. No `lmcache server`, coordinator, or P2P is
   required. This establishes whether several initiator *processes* can
   generate aggregate filesystem/NVMe-oF traffic. A writer always has a
   distinct prefix; concurrent writers never share one prefix.
2. **Serving topology.** Run one `lmcache server` per logical initiator
   instance, with distinct server/HTTP ports, instance IDs, L1 buffers,
   and exclusive L2 namespace or directory ownership. The MP server is
   required here because vLLM connects to its local StorageManager, not
   because NVMe-oF needs a coordinator. Use the MP L2 metrics alongside
   RDMA counters to validate each instance's attribution.

The MP coordinator remains off for the isolated-namespace baseline.
Enable it only for a separate P2P/read-sharing experiment: it supplies
membership and peer discovery, not shared-L2 locking, allocation, or
conditional create. It cannot make multiple `fs_native` writers on the
same ext4-mounted namespace correct.

The MkP host layout can exercise multiple instances on mkp1, but does
not by itself prove multiple physical initiator nodes. The 4x400
qualification must repeat the same matrix with multiple initiator hosts
and report aggregate application bytes, per-initiator bytes, and
per-port RDMA counters.

### Current MkP controller queue configuration

The existing mkp1 controllers are constrained to their already-live
connections: `nvme2` and `nvme3` each report `queue_count=17`, meaning
one admin queue plus 16 I/O queues per controller. The Falcon NIC on
each host consequently has 34 live `nvme_rdma` RC QPs for the two
controllers, plus its normal GSI QP.

This is the negotiated controller configuration, not a demonstrated
hardware QP limit. New RC-QP creation currently fails, so do not change
the queue count or attach another controller for the MkP process test.
The 2- and 4-process rows below intentionally share these existing I/O
queues.

## 7. 4x400 GbE namespace layout

The 4x400 target should start with four initiator hosts, one 400 GbE
port per host. A single initiator using four paths is useful for a
later host-throughput experiment, but does not exercise independent
initiator failure domains or fan-in.

The first scaling baseline assigns each initiator group exclusive
write ownership of one target L2 pool:

| Initiator group | Falcon endpoint | Target L2 pool |
|---|---|---|
| `init-0` | port 0 | `kv-pool-0` |
| `init-1` | port 1 | `kv-pool-1` |
| `init-2` | port 2 | `kv-pool-2` |
| `init-3` | port 3 | `kv-pool-3` |

Each pool is one or more NVMe-oF namespaces backed by a disjoint SSD
set. Every logical LMCache initiator has its own L1, MP server ports,
and instance ID. This is Option A at scale: it is safe without a
distributed metadata service, and gives an unambiguous per-port
baseline. A prepopulated keyspace may be read concurrently for
traffic-generation experiments, but no filesystem is mounted
read-write by more than one initiator.

Pool width is a measured capacity decision, not "four SSDs per port"
by default. A 400 GbE port needs about 50 GB/s payload; provision at
least 60 GB/s in the exact target-side RAID/filesystem/NVMe-oF
configuration:

```text
pool_width = ceil(60 GB/s / measured sustained read GB/s per SSD)
```

If a four-SSD pool cannot meet that target, sixteen SSDs cannot
support four independent line-rate pools. Add drives, accept a lower
per-port storage ceiling, or move to the shared sharded design below.
Linux NVMe multipath is not assumed to stripe I/O across four 400 GbE
paths; it is an availability feature unless a measured configuration
proves balanced aggregate bandwidth.

The production shared-L2 design removes the static
initiator-to-SSD mapping. A target-side B1 metadata authority maps the
full `ObjectKey` to a shard, extent, and generation; every initiator
may then access the assigned data extent over NVMe-oF. A reasonable
initial layout is eight logical data shards striped over two SSDs each,
plus one dedicated metadata/WAL namespace, but the final shard width
must follow fio and hot-shard measurements. This remains blocked on
the B1 conditional-create, WAL/replay, checksum, and GC gates in
§4.2.1. MP servers and the MP coordinator do not supply this storage
authority.

## 8. Multi-initiator matrix

The first table is the current MkP proving matrix. It is scoped to
`fs_native` over the 100 GbE Falcon-backed kernel NVMe-oF path.

| Logical initiators | L2 ownership | Runtime | Coordinator | Purpose |
|---|---|---|---|---|
| 1 | one prepopulated read-only prefix | `bench l2` | off | Sustained-load reference |
| 2, 4 | same read-only prefix on the existing controllers | independent `bench l2 --only load` processes | off | Next MkP functional case: process-concurrency and read-only isolation check; not a shared-write, fresh-QP, 64-QP, or physical multi-initiator test |
| 2, 4 | exclusive namespace or directory per instance | one MP server per logical initiator | off | Option A serving topology, separate L1 and L2 ownership |
| 2, 4 | exclusive namespace or directory per instance | one MP server per logical initiator | on, P2P only | Option A plus read-side peer-sharing functional test |

There is no shared-writable-namespace row in the MkP matrix. Adding
one requires the B1 gate list in §4.2.1 to be cleared. The 4x400
matrix will re-run the isolated rows across multiple physical
initiators; the shared-L2 matrix is deferred until B1 can report
capacity and prefill-work deltas at 0% and 100% key overlap.

**Blocked R2 topology.** The planned 64-QP case is blocked by the
current fresh-RC-QP failure (`LMCache-cfb`). Do not relabel the MkP
multi-process row as that case: it uses the existing kernel NVMe-oF
controllers and cannot establish a 64-QP or physical multi-initiator
topology. Its result is functional evidence for process isolation and
aggregate request generation only.

## 9. Consistency model (single statement)

Cached-byte correctness rests on the per-initiator block-layer
contract from `nvmeof-initiator-only-alternative.md` §3:
WAL/COW-published key→LBA map, checksum verified on read, no
reader sees an unpublished write. Under Option A, each initiator
enforces this on its own namespace with no cross-initiator
coordination.

Option C's Cache Controller directory and P2P transfers are
**eventually-consistent hints** on top of that contract. Stale
directory entries degrade to a local L2 fetch, provided the
homogeneous-compatibility-domain constraint (§4.3) is enforced.
P2P transfers must validate the pulled bytes against the same
checksum used for L2 fetches — same integrity gate, different
source.

No distributed lock is introduced. Pin/unpin remains advisory
per-instance; eviction remains uncoordinated.

## 10. Controller placement (only when Option C is enabled)

Ranked, most preferred first:

1. **Dedicated IPU host (not a storage node).** Fate-shares with
   the fabric, not a target SSD; cheap.
2. **Storage-node CPU.** Convenient but couples the directory's
   fate to the target. Acceptable if the deployment already
   treats the storage node as a hard failure boundary.
3. **Co-located on one initiator.** Convenient for lab bring-up;
   entangles a peer's crash with fleet-wide lookup availability.
   Rejected for any deliverable configuration.

The controller is not on the data path. Latency to it matters
only during store/evict directory pushes and full-sync.

## 11. Failure domains

Enumerated once. Do not re-derive elsewhere.

- **Initiator crash (Option A).** Local namespace only; peer
  initiators unaffected. WAL replay on restart discards
  unpublished writes.
- **Initiator crash (Option A + C).** Same as above; controller
  marks the instance dead and its L1 entries drop out of P2P
  lookup. Full-sync on restart repopulates.
- **Target crash.** L2 unavailable to all initiators; each
  degrades to L1-only. Target-side WAL/COW recovery is
  out of scope for this note.
- **Controller crash (Option C only).** No data loss. P2P
  lookups fail; each initiator falls back to L1 + local L2
  (Option A behavior). Rehydrate on controller restart via
  `FullSync*`.
- **Fabric partition.** Isolated initiator degrades to L1-only
  plus retry queue against its own target namespace.
- **Split-brain.** None at the cache layer under Option A —
  each namespace has one writer. B1 forecloses split-brain by
  making the target daemon the sole metadata authority; a
  daemon-partition scenario reduces to *daemon unavailable*
  (fail closed), not two daemons diverging. B2/B3 have real
  split-brain shapes that their respective sections must
  address.

## 12. What this note deliberately does not solve

- **B1 daemon internals.** RPC wire format, WAL record layout,
  extent allocator strategy, GC cadence, and upgrade/rollback
  procedures are all deferred to the B1 design doc. This note
  fixes only the framing (A-plus, target-side authority) and
  the gate list.
- **B2/B3/B4 detailed specs.** Evaluated against B1 as
  strawman; not specified in isolation.
- **Cross-domain sharing under Option C.** Requires a full-key
  P2P lookup, which is not what the current controller messages
  carry. B1's canonical-key protocol is the correct place to
  fix this if Option C is retained after B1 ships.
- **Non-idempotent writes.** Same as before: a real lock would
  be needed on a critical path. Flag any future change to
  `CacheEngineKey` that adds non-key-determined bytes.
- **Cross-target replication.** Not needed for a 1-target PoC.

## 13. Follow-on beads

- `LMCache-dnt` — bench work to exercise the Option A rows in
  §6. Overlap-percent knob is now a **workload-similarity**
  knob, not a shared-namespace parameter: it drives how much
  key overlap exists between initiators writing to their own
  namespaces (bounding what Option C could later recover) and
  is measured post-hoc from access logs.
- New (not yet filed) — measurement pass that computes
  achievable peer-warm hit rate from Option A logs, gating the
  Option C rollout decision (§5).
- New (not yet filed) — **B1 design doc**: target-side metadata
  daemon spec covering the four gate items in §4.2.1 (atomic
  conditional create, `ObjectKey` + compatibility-domain ID
  identity, WAL/replay behavior across daemon/initiator
  crashes, 0%/100% overlap measurement plan). Includes B2/B3/B4
  as evaluated alternatives against the B1 strawman. Tracked
  against `nvmeof-poc-plan.md` §11 as the M1 milestone for
  shared L2.

## See also

- `nvmeof-initiator-only-alternative.md` — parent architecture
  (this note is a §1/§6 continuation)
- `nvmeof-poc-plan.md` §6.1, §11 — PoC scope boundary and the
  Future Work items this note respects
- `lmcache/v1/cache_controller/` — Cache Controller
- `lmcache/v1/storage_backend/p2p_backend.py:284` — P2P batched
  lookup (chunk_hash only)
- `lmcache/v1/storage_backend/raw_block/core.py:303` — single
  process-local lock; §4.2 blocker
- `lmcache/utils.py:399` — `CacheEngineKey` definition
