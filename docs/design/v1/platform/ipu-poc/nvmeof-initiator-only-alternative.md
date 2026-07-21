# NVMe-oF Initiator-Only Alternative — Impact on the Pull Model

## Alternative under consideration

Run LMCache only on the initiator (compute) side. Export the storage node's
NVMe SSDs over NVMe-oF/RDMA. No LMCache agent runs on the storage node —
the storage side presents only NVMe namespaces via an NVMe-oF target
(kernel `nvmet-rdma` or an equivalent).

This is an **initiator-owned LMCache + remote NVMe L2** architecture, not a
variant of the current storage-owned pull design.

## Summary

The current *pull* model does not survive this change. It is a
storage-owned cache design: the target-side LMCache owns semantic
admission, BLAKE3 verification on commit, MR leases, and the two-phase
L1/L2 eviction/flush contract. Removing the target agent eliminates those
cache-level semantics.

What remains is a perfectly workable — but architecturally different —
initiator-owned tiered cache in which LMCache manages DRAM/HBM as L1 and a
remote NVMe namespace as L2. Several storage-owned properties reappear as
new initiator-side responsibilities: allocation, key→LBA mapping,
integrity metadata, durability ordering, and crash recovery.

**Do not conflate this with M1.** M1 is a DRAM-to-DRAM raw RC verbs
baseline. NVMe-oF traverses the NVMe controller, SQ/CQ queues, PCIe, and
SSD media; raw verbs cannot address an NVMe namespace directly. The two
must be measured and reasoned about separately.

## Impact by subsystem

### 1. Semantic admission moves to the initiator

Today the target owns `ALREADY_PRESENT`, `REJECT`, `QUEUE`, `ACCEPT`, and
the digest-validated commit. With no target-side LMCache, these cache
decisions migrate to the initiator side:

- **Single-initiator, exclusive namespace ownership** — the initiator holds
  a local key→LBA index and allocator; admission is a local decision.
- **Multi-initiator shared namespace** — needs a shared allocator, an
  ownership/lease protocol, a mapping authority, GC, and recovery. This
  is where a coordinator becomes necessary; conceptually it re-introduces
  the storage-side control plane on a different host.

The **cache metadata authority** moves off the target in both cases; only
the single-initiator case avoids a new distributed protocol. The NVMe
target remains the durable authority for stored block bytes — what changes
is that the mapping from cache keys to those blocks, and the decisions
about what to admit or evict, no longer live on the storage side.

### 2. Transport direction is not what changes — cache semantics are

The current pull model has the *target-side LMCache* post RDMA Reads
against the initiator's registered source buffer, gated by target
admission. That behavior is gone: there is no target agent to gate or
issue those reads at the cache layer.

At the NVMe-oF transport layer, direction is not fixed. For NVMe writes,
the target commonly performs RDMA Reads from the initiator's data
buffers; for NVMe reads, the target performs RDMA Writes into initiator
buffers. So the wire-level "who pulls" question depends on the NVMe
command, not on whether the semantic pull model survives. What
disappears is the *cache-level* lease/admission pull, not RDMA direction
in general.

### 3. Integrity gate moves off the wire

The current design recomputes BLAKE3 on the target after the RDMA Read
lands and NACKs on mismatch. Without a target-side agent, this specific
gate is gone. Reasonable initiator-side replacements exist:

- Hash the page on the initiator, write data + checksum metadata
  (per-block or in a metadata namespace), issue an NVMe `FLUSH` or use
  FUA on the write, and only then publish the key→LBA mapping.
- Recovery scans the checksum metadata; unpublished writes are treated
  as never committed.

FUA/FLUSH by themselves do **not** make the data write, the checksum
record, and the key→LBA map update atomic as a group — they are
independent I/Os to independent regions. A production design needs one
of: a durable intent log / WAL that records the tuple before any of the
three lands, or a copy-on-write generation scheme in which a new
generation only becomes visible when a single pointer flips. Recovery
rules must then handle torn or stale map and checksum records
(unreferenced blocks → GC; map entries pointing at absent or bad
checksums → discard).

The property that is lost is an *independent, target-side* verifier — if
the initiator computes and stores the hash itself, host-side memory
corruption between hash and write is undetectable. The document does not
prescribe a scheme; it flags that the design must specify write-ack,
durability, ordering between data and map publication, and crash
recovery explicitly.

### 4. L1/L2 do not collapse — eviction moves to the initiator

LMCache still runs on the initiator, so its DRAM/HBM tier remains **L1**
and the exported NVMe namespace is **remote L2**. The two-phase contract
(reserve L1 slot; if reservation requires eviction, durably flush LRU to
L2 before completing the write) is preserved conceptually. What changes:

- Eviction and durable flush are executed by the *initiator-side*
  LMCache, not by a target-side agent.
- Space accounting on L2 is now the initiator's job (or the coordinator's
  in the multi-initiator case), since the target only sees LBAs.
- Wear-leveling and SSD-internal GC remain the drive's / target's
  concern and are invisible to LMCache, as they are today.

### 5. Cache-level lease / QUEUE / CANCEL semantics are gone

Transport-level flow control does not disappear: NVMe-oF SQ/CQ
backpressure, RDMA credits, and MR lifetime for I/O buffers continue to
function under the initiator's control. What disappears is the
*LMCache-level* contract in which the target holds an initiator's source
MR pinned under a lease until L1 capacity frees, and can `CANCEL`
misbehaving initiators. There is no target-side actor to hold that
lease or fence a peer.

Distinguish **transport fencing** from **semantic fencing**. An NVMe-oF
target can still enforce host NQN ACLs, per-host queue limits,
namespace-masking, and abrupt host disconnects — those transport-level
controls survive. What it cannot enforce is LMCache-level
key/admission/lease semantics: it has no view of cache keys, no notion
of a page being "queued pending capacity," and no way to distinguish a
well-behaved cache client from a bad one at the semantic layer.

### 6. Key→LBA mapping is a new initiator-side responsibility

Object semantics (`store(key)`) → block semantics (`write LBA range`)
requires a mapping table. Placement depends on the deployment boundary:

- **Single initiator, exclusive namespace** — local table on the
  initiator. No coordination.
- **Multiple initiators sharing a namespace** — needs a shared allocator
  and mapping authority (leases over LBA extents, GC, recovery). This is
  the case that reproduces target-side control-plane complexity.

### 7. What survives

- The IPU-as-NIC thesis — unaffected.
- Raw M1 verbs baseline as a **separate** measurement. It remains the
  best DRAM-to-DRAM number for the fabric and is not comparable to
  NVMe-oF throughput.
- Initiator-owned tiered caching (L1 DRAM/HBM, L2 remote NVMe) as a
  workable architecture with different (not degenerate) semantics.

## Recommendation

Frame this as an **alternative architecture**, not a tweak to M1:

- Benchmark it *alongside* M1 as a distinct experiment: an NVMe-oF
  round-trip (initiator LMCache → `nvmet-rdma` → SSD) with its own
  latency/throughput profile, not the M1 verbs number.
- Introduce M3-like requirements up front: persistence, durability
  ordering (FUA/FLUSH), key→LBA allocator, crash recovery, and the
  single- vs multi-initiator deployment split.
- Do not present it as preserving the storage-owned pull model. The
  cache-level admission, target-side BLAKE3, and target-held MR-lease
  contracts do not survive. Everything else — including L1/L2 tiering
  — remains, but moves to the initiator side and needs explicit design.

## See also

- `ipu-poc.md` — top-level POC design
- `ipu-poc-opens.md` — open questions deck
- `lmcache-nvmeof-jbof-flow.mmd` — JBOF flow diagram
