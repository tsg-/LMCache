# Initiator-Owned NVMe-oF L2 POC Plan

## Purpose

Prove that a single LMCache initiator can use an exclusively assigned remote
NVMe-oF/RDMA namespace as durable L2 while keeping L1, cache metadata,
admission, integrity checking, and recovery on the initiator. This is a
separate POC from the storage-owned RDMA pull architecture and does not
reuse its performance targets or headline results.

The architecture and sequence flow are documented in
[nvmeof-initiator-only-alternative.md](nvmeof-initiator-only-alternative.md),
[nvmeof-poc-test-setup.mmd](nvmeof-poc-test-setup.mmd), and
[lmcache-nvmeof-jbof-flow.mmd](lmcache-nvmeof-jbof-flow.mmd).

## Scope and Boundaries

| In scope | Explicitly out of scope |
|---|---|
| One initiator with exclusive ownership of one unused namespace | Multiple initiators sharing a namespace |
| NVMe-oF/RDMA target lifecycle, attach, reconnect, and teardown | Target-side LMCache, cache-level admission, or MR leases |
| Initiator-owned L1 plus remote NVMe L2 | Falcon or IPU control-plane offload |
| Durable store, load, restart, and reconnect behavior | Comparing NVMe-oF figures with M1 raw-verbs figures |
| WAL or copy-on-write publication of data, checksum, and key-to-LBA mapping | vLLM production qualification |

The target node exports a namespace through `nvmet-rdma`; it does not run an
LMCache agent and its DRAM is not an LMCache tier. The initiator alone owns
the key-to-LBA map, allocation policy, integrity metadata, and visibility
decision.

## Preconditions

The POC does not begin destructive I/O or benchmarking until every condition
below is recorded in the run manifest.

| Gate | Required evidence |
|---|---|
| Isolated fabric | The data subnet is reachable at the target fabric MTU (currently 4096; see MTU discussion below) in both directions. Management connectivity is on a separate interface and is not reconfigured during the POC. |
| Dedicated media | A named `/dev/disk/by-id/...` namespace is unused, unmounted, and not a system or mounted data device. The target script refuses any other device. |
| Target isolation | The target binds only the data-subnet address, uses a unique configfs port, and permits only the initiator host NQN through its ACL. |
| Lifecycle safety | Setup, status, connect, path discovery, disconnect, and teardown are idempotent. A failed setup leaves no mounted device, controller, subsystem, or configfs port behind. |
| Observability | Initiator kernel/NVMe logs, target `nvmet` logs, `nvme list-subsys --json`, controller statistics, and namespace identity are captured with each run. |

### Fabric MTU Requirement

The data-plane fabric (currently 192.168.200) must be configured for and
sustain **MTU 4096** end-to-end. This is a hard precondition, not a tuning
knob. The management plane (192.168.100) is never touched — its MTU stays
at the switch default and is out of scope for this POC.

**Verification (both hosts, before every run):**

- Link MTU: `ip -j link show <fabric-iface> | jq '.[0].mtu'` reports `4096`
  on both initiator and target fabric interfaces.
- RoCEv2 active MTU: `ibv_devinfo -d <hca> -v | grep active_mtu` reports
  `IBV_MTU_4096` on both HCAs.
- Bidirectional data-path check: `ping -M do -s 4000 -c 3 <peer-fabric-ip>`
  succeeds in both directions (initiator → target and target → initiator).

**MTU discussion — why 4096 is the hard requirement:**

- **RC-QP path MTU is the operative value, not the Ethernet link MTU.**
  RoCEv2 negotiates the QP path MTU from the link MTU and enumerates only
  `IBV_MTU_{256, 512, 1024, 2048, 4096}` — there is no `IBV_MTU_9000`.
  At link MTU 1500 the QP falls back to `IBV_MTU_1024`; a 4 KiB KV page
  fragments into four PSN-numbered packets and shifts RC-QP
  retransmit-window behavior. The raw-verbs performance baselines
  (`bench_verbs.py`, storage-owned M1 READ/WRITE runs) are defined
  against `IBV_MTU_4096` and are not comparable to a 1500-MTU run.
- **Link MTU ≥ 4200 satisfies the precondition.** The link needs enough
  headroom for a 4096-byte RDMA payload plus RoCEv2/UDP/IP headers
  (~150 bytes). Any Ethernet MTU from 4200 up to 9000 works, provided
  `active_mtu` reports `IBV_MTU_4096` on both HCAs. A fabric already
  provisioned for 9000-byte jumbo frames (shared with NVMe-oF or
  TCP-storage workloads) satisfies this POC because RoCEv2 still
  negotiates `IBV_MTU_4096` on top of a larger link MTU. The extra
  link-layer headroom is unused by RDMA payloads — measured wire-packet
  count per KV page is identical at link MTU 4200 and at link MTU 9000.
- **256 KiB NVMe-oF block efficiency.** The durable-commit flow issues
  256 KiB O_DIRECT/io_uring writes. `IBV_MTU_4096` delivers ~64 wire
  packets per block; `IBV_MTU_1024` (link 1500) delivers ~250+ with
  higher per-packet header overhead and worse tail latency, invalidating
  comparison with M1 or any prior fabric numbers.
- **This POC picks link MTU 4096.** The 192.168.200 CX7 lab fabric is
  provisioned for it; raising the link MTU to 9000 adds no RDMA benefit
  and introduces a switch-config change outside the POC's scope. If a
  customer environment is fixed at 9000, run there — the POC does not
  fail on that configuration as long as `active_mtu=IBV_MTU_4096` is
  observed on both ends.
- **Cross-track evidence integrity.** Phase 2 baselines and Phase 5
  workload evidence must be reproducible from the manifest. A silent
  sub-4096 `active_mtu` run produces numbers that look plausible but
  are not comparable to any other track's results.

**If the fabric refuses `IBV_MTU_4096`** (link MTU below 4200, `ping -M
do -s 4000` drops, or `active_mtu` reports lower than `IBV_MTU_4096`),
the impact is scoped by phase — not every phase requires 4096:

- **Phases 1, 3, 4 (functional correctness) may proceed** at the
  negotiated `active_mtu` (typically `IBV_MTU_1024` on a 1500-byte link
  or `IBV_MTU_2048` on a 2200+ link). Lifecycle safety, the durable
  WAL/COW commit protocol, and the fault/recovery matrix are all
  MTU-independent — their correctness invariants do not depend on
  packet size. The run manifest must record the observed `active_mtu`
  and label the run `mtu:degraded`.
- **Phases 2 and 5 (performance evidence) must stop** and escalate per
  the "HCA/fabric-instability" row in Risks. Any numbers produced at
  sub-4096 `active_mtu` are not comparable to raw-verbs baselines or to
  cross-track results; they may be captured for characterization but
  must be labeled `mtu:degraded, non-baseline` and cannot be presented
  as POC performance evidence.

Restoring `IBV_MTU_4096` and re-running Phase 2/5 is required before
any customer-facing performance claim.

## Work Plan

### Phase 0: Freeze the Contract

Document the selected namespace, host NQN, target NQN, data-plane IPs,
device-by-id path, page size, queue depth, and exclusive-ownership
assumption. Confirm that the target does not expose an LMCache service.

**Exit gate:** the topology diagram, command inventory, ownership boundary,
and rollback owner are reviewed by the customer and lab operator.

### Phase 1: Prove Safe NVMe-oF Lifecycle

Implement and exercise the target provisioning and initiator attachment
scripts. The target must reject non-data-plane addresses, require the host NQN
ACL, validate the namespace device, and avoid hard-coded or shared configfs
ports. Initiator discovery must handle both the v1 and v2 schemas returned by
`nvme list-subsys --json` across kernel versions.

Run the sequence below at least three times: `setup -> connect -> 4 KiB
write/read/sha256 -> disconnect -> teardown`. Run a negative test for each
safety check: management-plane listen IP, wrong subnet, missing ACL, mounted
device, and pre-existing configfs port.

**Exit gate:** all positive cycles succeed; all negative cases fail closed;
the final status shows no controller or target residue.

### Phase 2: Establish the Remote-L2 I/O Baseline

Attach the namespace using its stable device path and run direct block-I/O
reads and writes at the intended page size and queue-depth sweep. Record
attach-to-first-I/O time, p50/p99 latency, aggregate read/write bandwidth,
I/O error count, queue depth, and target controller/SSD metrics.

These measurements are an NVMe-oF host-I/O baseline. They are not raw-verbs
or IPU headline results.

**Exit gate:** every completed I/O has an external SHA-256 (or equivalent)
write/read verification pair; no unexpected timeout, controller reset, or
kernel error occurs; the manifest identifies the target namespace and
software versions. BLAKE3 and the durable-commit protocol are introduced in
Phase 3, not here.

### Phase 3: Implement Durable Initiator-Owned Publication

Add the remote-L2 connector. The POC commits to **WAL** as the durable-commit
protocol, matching `lmcache-nvmeof-jbof-flow.mmd`. Copy-on-write remains a
viable alternative for future work but is not in scope for this POC — the
WAL vs COW decision is closed at the start of Phase 3.

A store must execute the following ordered cutpoints:

1. Append intent `{key, LBA_range, digest}` to the WAL and flush.
2. Write the payload with FUA (or a verified flush barrier).
3. Write the checksum record with FUA.
4. Atomically publish the key-to-LBA map and flip the L1 entry from
   `PENDING` to `VISIBLE`, then return the terminal ACK.

A periodic metadata checkpoint is not a substitute for this protocol.

**Exit gate:** a normal store/load cycle verifies BLAKE3 on read and exposes
the key only after durable publication.

### Phase 4: Fault and Recovery Matrix

Inject failures after each of the four durable boundaries defined in
Phase 3: (1) WAL intent, (2) payload+FUA, (3) checksum+FUA, (4) atomic map
publish. Repeat the matrix for process restart and NVMe disconnect/reconnect.
On recovery, discard uncommitted or checksum-invalid mappings, drop all
`PENDING` L1 entries, and identify unreferenced payloads as GC candidates.

**Exit gate:** each case has either the old committed value or the fully
published new value. No torn key, stale checksum, or unacknowledged store may
be lookup-visible.

### Phase 5: LMCache Integration and Workload Evidence

Run the initiator-only LMCache path with local DRAM/HBM L1 and remote NVMe L2.
Exercise L1 hit, L2 hit, L2 miss, eviction-to-L2, store, restart, and
reconnect. Publish separate latency distributions for L1 and L2, durable
eviction time, integrity failures, recovery time, and target controller
metrics.

**Exit gate:** all functional scenarios pass with the recovery guarantees
from Phase 4.

## Required Deliverables

Every artifact below is labeled `track:initiator-nvmeof` to keep this POC's
evidence separate from the storage-owned M1 raw-verbs track.

- Lifecycle scripts and focused unit tests.
- A versioned run manifest containing topology, NQNs, namespace identity,
  package/kernel versions, commands, metrics, and log paths.
- Scenario definitions for normal write, crash replay, and reconnect.
- Fault-matrix report with one row per cutpoint and the observed recovered
  state.
- Benchmark report that separates L1, remote-L2, and lifecycle metrics.
- Demo runbook with setup, test, teardown, and rollback commands.

## Customer Acceptance Criteria

| Area | Acceptance criterion |
|---|---|
| Isolation | One initiator accesses only its ACL-approved, dedicated namespace. |
| Lifecycle | Repeated attach/use/detach leaves no leaked target or initiator state. |
| Integrity | Every successful load matches its committed BLAKE3 checksum. |
| Durability | ACK means the data, checksum, and map are recoverable after restart. |
| Recovery | All crash and reconnect cutpoints recover without exposing torn or stale keys. |
| Evidence | Every result is reproducible from the manifest and is labeled as the initiator-owned NVMe-oF track. |

## Risks and Decisions

| Risk | Mitigation or decision |
|---|---|
| Wrong namespace damages data | Require unused device-by-id path and fail closed on mount, partition, or holder detection. |
| Fabric or RDMA instability obscures storage behavior | Gate on bidirectional MTU/connectivity checks before lifecycle or benchmark work. |
| FUA/FLUSH is mistaken for an atomic transaction | Require WAL/COW publication and fault-cutpoint evidence before claiming durability. |
| Results are compared to raw verbs | Report NVMe-oF as a separate host-I/O path with its own latency and bandwidth. |
| A later multi-initiator request expands the design | Treat it as a separate coordinator/lease/allocator project, not a POC extension. |
| Initiator HCA/driver refuses MR or MKEY creation | Phase 1 blocks on transport, not lifecycle. Hand off to the fabric team with BDF-scoped diagnostics before iterating on lifecycle scripts. |

## Go / No-Go Decision

Proceed to performance characterization only after Phases 1 through 4 pass.
A lifecycle-safety failure, any torn-key exposure, or missing manifest is a
no-go for customer performance claims. After Phase 5, retain or advance the
architecture only if the measured remote-L2 benefit justifies its durability
and operational complexity relative to the storage-owned track.
