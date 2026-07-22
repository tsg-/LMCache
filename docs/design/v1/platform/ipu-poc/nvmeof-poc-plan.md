# Initiator-Owned NVMe-oF L2 POC Plan

## 1. Executive Summary

This plan separates the software architecture decision from the hardware
rollout.

- **Architecture A** (initiator-owned NVMe-oF) is the starting point
  for this POC and the only architecture executed here. Architecture B
  (storage-owned pull with a target-side LMCache agent) is a separate
  track and is not decided by this plan.
- **Platform** names the hardware platforms evaluated under
  Architecture A:
  - **CX7** — Mellanox CX7 on Xeon hosts (Granite Rapids AP) with
    commodity NVMe. Used to shake out the initiator-owned software
    stack and produce the T7 kernel-path baseline.
  - **MEV** — Intel IPU with Falcon offload on the IPU, Gen4 NVMe
    pool. First IPU-offload measurement against the CX7 T7 baseline.
  - **MMG** — Xeon storage platform with Intel MMG-400 IPUs and Falcon
    offload. The IPU/SSD count and host topology are a pre-run decision
    in Appendix D.3. Follow-on integration begins when silicon and
    Falcon enabling are ready (anticipated early August 2026; see
    Appendix D.6).

  They are not sequential stages.
- **Delivery scope of this plan:** CX7 end-to-end (Deliverables 1
  and 2), plus the MEV integration contract (Appendix D.5) that a
  follow-on MEV plan will consume. MEV execution itself and MMG
  integration are follow-on plans; their environments are captured
  as contracts in Appendix D.5 / D.6, not as milestones on this
  plan's schedule.
- Under Architecture A, execution on any platform is split into
  **two deliverables**. Stages 0–5 (Appendix B) are the
  implementation milestones inside these deliverables:
  - **Deliverable 1 — Raw NVMe-oF Baseline.** Safe target lifecycle
    (attach, detach, reconnect exercised as lifecycle steps under T1
    — timing is not part of D1 exit), direct block read/write with
    integrity verification (T2a), latency and throughput (T2b).
    **Exit:** reproducible remote-NVMe latency/throughput baseline
    and a lifecycle that is idempotent. **Explicit non-claims:** no
    durable LMCache cache semantics, no crash recovery, no host-CPU
    baseline (T7, host-CPU-per-GB and MR/QP churn), no timed
    attach/reconnect metrics — those all belong to D2. (Stages 0–2.)
  - **Deliverable 2 — Durable Remote-L2.** LMCache integration,
    key→LBA metadata, WAL (Appendix A) or equivalent, idempotency;
    crash / reconnect recovery, overwrite generations, allocator
    safety; CX7 host-CPU baseline (T7) captured under the integrated
    workload (T6). **Exit:** an ACKed cache entry is recoverable and
    integrity-verified, and the T7 CX7 baseline is complete enough
    for the follow-on MEV / MMG plans to compare against. (Stages
    3–5.)
- This plan finishes at CX7 D2 exit. The follow-on MEV plan then
  runs both deliverables against the environment in Appendix D.5;
  MMG follows once its environment (Appendix D.6) is real.

**Claim-scope rule.** Deliverable 1 results may claim NVMe-oF
connectivity, lifecycle safety, and block-level performance /
integrity. They **must not** claim durable LMCache L2 or crash
safety. Those claims start only after Deliverable 2 exit.

Architecture A is the customer-requested path to measure whether an
IPU can reduce transport CPU cost without degrading cache behavior.
Success for this plan means passing the functional and durability
gates in Section 9.5 on CX7 and producing a T7 CX7 baseline the
follow-on MEV / MMG plans can compare against. The Architecture A
outcome (advance A, optimize A, or abandon A; Section 3.1) is not
finalized by this plan alone — it requires the MEV / MMG platform
evidence a subsequent plan will produce.

**Definitions used throughout:** L0 = GPU HBM; L1 = initiator host DRAM;
L2 = remote NVMe namespace via NVMe-oF/RDMA. Architecture A places all
cache semantics — key→LBA map, WAL, allocator, admission — on the
initiator, and this stays true across all three platforms (CX7, MEV,
MMG). What can differ between the IPU platforms (MEV, MMG) is whether
each platform retains or replaces the Linux NVMe-oF / block-I/O path
— a software-architecture decision, not just a transport swap.
Architecture doesn't dictate that choice; each IPU platform must make
it explicit (Appendix D).

## 2. Customer Requirements and Traceability

| ID | Requirement | Evidence |
|---|---|---|
| R1 | LMCache runs only on the initiator; the target exports NVMe-oF namespaces and runs no LMCache agent. | Target process and `nvmet-rdma` configuration at kickoff. |
| R2 | One initiator exclusively owns one unused namespace. | Single-host NQN ACL and pre-attach namespace check. |
| R3 | Every successful load matches its recorded BLAKE3 checksum. | Read-path integrity verification. |
| R4 | Results are reproducible from a versioned manifest. | Manifest records topology, software versions, commands, and artifacts. |
| R5 | CX7 results provide the baseline for later IPU platform comparisons. | T7 captures host CPU, `nvme_rdma` MR/QP churn, and CQ event rate. |

## 3. Architecture A vs B — Context

Architecture A is the starting point for this POC. The table below is
informational context on how A and B differ; it is **not** a decision
output of this plan. A and B measure different things (see Section 6.1
and the risk row on B comparisons in Section 10), and no shared
workload contract exists here to make them directly comparable. Any
A-vs-B decision would require a separate cross-architecture
experiment.

CX7 delivery gives a Xeon-based host with commodity NVMe to shake out
the software stack; MEV delivery adds Falcon RDMA offload on top of a
small Gen4 NVMe pool. MMG is a follow-on integration when silicon is
ready (Appendix D.6).

| Dimension | A: Initiator-owned NVMe-oF | B: Storage-owned RDMA + LMCache server |
|---|---|---|
| Ownership of cache semantics | Initiator only | Target-side LMCache agent |
| Storage node role | Passive `nvmet-rdma` | Smart cache (hash, admission, eviction) |
| Fault domain surface | Initiator process + fabric + target block layer | + target-side cache control plane |
| Operational complexity | Lower (no target agent, single-initiator scope) | Higher (agent lifecycle, admission tuning) |
| Durability authority | Initiator WAL + NVMe FUA/FLUSH on SSD | Target agent + NVMe |
| Latency path | Initiator LMCache → NVMe-oF/RDMA → SSD | Initiator MR → target LMCache admission decision → RDMA → SSD |
| Multi-initiator dedup | Out of scope (single-initiator exclusive namespace) | Yes (global hash index on server) |
| Cache-level admission / lease / BLAKE3-on-commit | Not present | Present |
| Distributed-systems risk | Low | Higher (control-plane, lease, quorum concerns) |
| IPU/Falcon offload opportunity | Initiator HCA (verbs/MR/QP) + target `nvmet-rdma` fabric termination | Target-side LMCache agent + admission control |
| Evidence to justify choosing | Simplicity, safety, offload of transport surface | Dedup benefit, admission benefit, multi-initiator scale |

### 3.1 Architecture A outcomes (framework for the full multi-plan decision)

CX7 (this plan), MEV (follow-on plan), and MMG (follow-on plan)
together answer whether Architecture A itself passes its functional,
durability, and offload-evidence gates. This plan is the CX7 input to
that framework; the outcome is finalized only after the follow-on
platform plans land. Three possible outcomes after all platform
evidence is in:

- **Advance A** as the primary path: correctness, durability, and the
  offload measurements from CX7 (T7) plus MEV / MMG all support it.
- **Optimize A** first: A passes correctness, but needs targeted work
  (WAL fast-path, allocator batching, or further IPU offload delivery)
  before the customer can claim performance numbers.
- **Abandon A**: A fails a functional/durability gate (Section 9.5) or
  neither the CX7 baseline nor the IPU-platform offload delta shows
  realistic headroom for MMG to build on. Any pivot to a different
  architecture is a follow-on decision, not an output of this plan.

## 4. Architecture A Topology and Component Roles

All cache semantics live on the initiator. Roles not listed do not
participate in Architecture A.

![Architecture A — CX7 hardware topology](diagrams/architecture-a-cx7-hardware-topology.svg)

*Physical hardware view of the CX7 platform: two identical Xeon +
CX7 servers connected over a single RoCEv2 link on the 192.168.200
data plane. Media (GPU HBM on the initiator; NVMe SSD on the
target) is shown in blue, host CPU sockets in purple, DRAM in green,
and RoCEv2 network hardware in amber. See Section 4.1–4.3 for the
role each component plays.*

### 4.1 Initiator (compute) host

| Component | Role in Architecture A |
|---|---|
| Host CPU (Xeon) | Runs the LMCache engine, `StorageManager`, WAL/map authority, allocator, and admission logic. Owns the key→LBA table and integrity metadata. |
| GPU HBM (L0) | Consumes KV pages via DMA from initiator DRAM; not directly on the NVMe-oF path. |
| Initiator DRAM (L1) | Pinned buffers for RDMA MR registration and GPU DMA. |
| Initiator HCA (CX7: Mellanox CX7; MEV: Intel IPU via `irdma`; MMG: Intel IPU with IPT) | Terminates the NVMe-oF/RDMA transport. Registers MRs for I/O buffers and the WAL log. |
| Linux NVMe-oF initiator (`nvme-cli`, `nvme_rdma`) | Fabric attach, path discovery, reconnect. Kernel-owned on CX7; MEV and MMG each define whether they preserve or replace this path (Appendix D). |

### 4.2 Target (storage) host

| Component | Role in Architecture A |
|---|---|
| Host CPU | Runs `nvmet-rdma`, configfs orchestration, and the SSD block layer. **No LMCache agent.** No cache-level decisions. |
| Host DRAM | Payload staging for `nvmet-rdma` and kernel block layer only. **Not** an LMCache tier. |
| Target HCA (CX7: Mellanox CX7; MEV: Intel IPU; MMG: Intel IPU / MMG-400) | Terminates the NVMe-oF/RDMA target transport. |
| NVMe SSD(s) (L2 media) | Durable authority for stored block bytes. Wear-leveling, internal GC, and FUA/FLUSH semantics are the drive's responsibility. |

Wire direction is a transport concern, not a cache decision. On an
NVMe Write, `nvmet-rdma` issues an RDMA Read to pull the payload from
the initiator's registered buffer; this is the standard transport
implementation of the NVMe Write command. It is not target-side
admission, dedup, or a cache-semantic pull. The target performs no
LMCache admission, holds no target-side WAL, and makes no allocation
decision. Target-side cache semantics — the admission-then-pull model
— is Architecture B.

### 4.3 Fabric and non-participants

Data plane (CX7 platform): 192.168.200 RDMA, RoCEv2. Management plane
(CX7 platform): 192.168.100 — never touched. MEV and MMG platforms
have their own network planes; see Appendix D.

Non-participants: target-side LMCache agent (removed by design);
IPU/Falcon offload software (MEV and MMG platforms and beyond);
multi-initiator coordinator (out of scope).

Topology diagram: `diagrams/architecture-a-cx7-nvmeof-topology.mmd`.
Sequence: `diagrams/architecture-a-nvmeof-wal-sequence.mmd`.

## 5. Test Environment and Operational Guardrails (CX7 platform)

Every run manifest snapshots the version-sensitive rows below. This
section describes the CX7 platform lab only. See D.5 for MEV;
D.6 (placeholder pending MMG silicon) for MMG.

### 5.1 Hardware and software inventory

Hosts, HCAs, and network planes below are specific to the CX7-platform
lab. MEV and MMG runs use different hosts and network planes.

| Attribute | Value |
|---|---|
| Initiator host | Xeon (bmg0-class), NVMe-oF initiator, GPU present for L0 DMA |
| Target host | Xeon (bmg1-class), NVMe SSD(s), no LMCache software |
| Initiator HCA | Mellanox CX7 (`mlx5_1` on bmg0), RoCEv2, GID index 4 (192.168.200.3) |
| Target HCA | Mellanox CX7 (`rocep153s0f0` on bmg1 post-rename), RoCEv2, GID index 5 (192.168.200.4) |
| Ethtool ifaces | Initiator `ens1f1np1`; target `ens1f0np0` |
| Data-plane fabric | 192.168.200.0/24, direct-attach or dedicated switch, link MTU 9000, `active_mtu=IBV_MTU_4096` on both HCAs |
| Management plane | 192.168.100.0/24 — SSH only; never reconfigured |
| Kernel modules (target) | `nvmet`, `nvmet_rdma` — must load cleanly (hard no-go) |
| Kernel modules (initiator) | `nvme_core`, `nvme_rdma`, `mlx5_core`, `mlx5_ib` |
| Branch / repo | `ipu-poc-nvmeof-alt` |

### 5.2 Operational guardrails (preconditions and hard no-gos)

The POC does not begin destructive I/O or benchmarking until every
condition below is recorded in the run manifest. All gates are hard
gates (schedule stops until met) **except the Fabric MTU row**,
which is a performance gate — functional stages (T1, T3 correctness,
T4/T5) may proceed labeled `mtu:degraded`, but no timed number (T2b,
T3 latency budget, T6 throughput, T7 baseline) is customer-reportable
until the MTU gate is satisfied.

| Gate | Type | Required evidence | If unmet |
|---|---|---|---|
| Target kernel modules | Hard | `nvmet` and `nvmet_rdma` load cleanly on the target host. | Target owner rebuilds module or boots compatible kernel before Stage 1 starts. See Section 7. |
| Fabric MTU | Performance | `active_mtu=IBV_MTU_4096` on both HCAs; link MTU ≥ 4200 (lab: 9000); bidirectional `ping -M do -s 4000` passes. See Appendix C. | Functional stages proceed labeled `mtu:degraded`; timed / performance runs stop and escalate — no customer-reportable numbers until the gate is met. |
| Management-plane isolation | Hard | Target refuses management-plane listen IP; management MTU/config unchanged. | Provisioning refuses to proceed. |
| Dedicated media | Hard | A named `/dev/disk/by-id/...` namespace is unused, unmounted, and not a system/data device. | Target provisioning script fails closed. |
| Target isolation | Hard | Target binds only data-subnet address, unique configfs port, single-host-NQN ACL. | Target provisioning script fails closed. |
| Lifecycle safety | Hard | Setup, status, connect, discover, disconnect, teardown are idempotent; failed setup leaves no residue. | Lifecycle test T1 fails; Stage does not exit. |
| Benchmark harness | Hard | Direct-I/O runner, LMCache trace harness, and manifest collector complete a dry run and produce a readable artifact. | Stop at Stage 0; select and document an alternate harness before any Stage 1 work. |
| Observability | Hard | Initiator kernel/NVMe logs, target `nvmet` logs, `nvme list-subsys --json`, controller statistics, namespace identity captured per run. | Run manifest is incomplete; results not customer-reportable. |

## 6. Scope, Exclusions, and Workload Assumptions

### 6.1 Scope and exclusions

| In scope | Explicitly out of scope |
|---|---|
| One initiator with exclusive ownership of one unused namespace | Multiple initiators sharing a namespace |
| NVMe-oF/RDMA target lifecycle, attach, reconnect, teardown | Target-side LMCache, cache-level admission, or MR leases |
| Initiator-owned L0/L1/L2 tiering on the CX7 platform | IPU/Falcon offload software delivery on MEV and MMG platforms |
| Durable store, load, restart, and reconnect behavior | Comparison against Architecture B raw-verbs figures as if same experiment |
| WAL-based durable publication of data, checksum, and key→LBA mapping | Copy-on-write publication (deferred; see Future Work) |
| Standard NVMe behavior across qualified SSDs | OEM selection, procurement, and vendor-specific SSD features such as CMB |

**Transport and offload boundary.** CX7 measurements use NVMe-oF/RDMA.
TSO/GSO is a TCP-path offload concern, not an RDMA payload-path
criterion; any TCP fallback must state its packetization and offload
evidence in a separate experiment. The IPU platform contract in
Appendix D defines the corresponding data path.

PTP is optional and is used only for approximate cross-host trace
alignment. It is not the source of latency or packet-pacing timestamps.

### 6.2 Workload assumptions

Fixed for the CX7 platform unless the customer confirms alternates at
kickoff.

**Block-I/O baseline (T2a/T2b):**

| Attribute | Value |
|---|---|
| Model / profile | DeepSeek-V3 KV-cache page geometry (or any single model whose page size ≥ 4 KiB). |
| Page size (I/O request size) | 4 KiB for latency runs; 128 KiB and 256 KiB for bandwidth runs. |
| Namespace capacity | ≥ 100 GiB usable; ≥ 2× the intended working set. |
| Queue-depth sweep | QD ∈ {1, 4, 16, 32, 64} for both read and write. |
| Test duration | ≥ 60 s per QD point for latency; ≥ 5 min per point for bandwidth. |

**LMCache workload (T6) — synthetic KV trace with fixed reuse:**

"Reuse rate" in this section means the **L1 hit rate** — the fraction
of accesses that a warm L1 satisfies from local DRAM without touching
L2. Because every T6 run pre-populates the full key population into L2
and starts L1 empty, every non-L1-hit is by construction an L1-miss
that hits L2 (not a cold-L2 miss). Repeated-prefix rate in the trace is
tuned to drive the target L1 hit rate; observed L1 hit counters are
what the pass criterion checks.

| Attribute | Value |
|---|---|
| Access sequence | Zipf-distributed key selection over a fixed key population sized ≥ 4× L1; repeated-prefix rate parameterized by trace to drive the target L1 hit rate. |
| L1 hit rates measured | 0%, 20%, 50%, 80% (four separate runs). 0% = every access is an L1 miss that hits L2 (traffic-generator sanity point); 80% = high L1 hit rate with occasional L2 hits. |
| Warmup | Pre-populate L2 with the full key population, then run for a warmup window equal to 2× the L1 fill time before starting measurement. |
| Cache-clear / reset between runs | Restart the LMCache process AND detach/reattach the NVMe-oF namespace between L1-hit-rate points so L1 starts empty and L2 starts with the pre-populated set. Every T6 run manifest records the pre-run L1/L2 hit counters as zero. |
| Promotion policy | L2-hit → promote to L1 with LRU eviction; recorded in the run manifest. |
| Read/write mix | Reads:writes ≥ 5:1 in a named steady-state run; record observed TX:RX bytes and the achieved operation mix. |
| Test duration | ≥ 15 min sustained per L1-hit-rate point after warmup. |
| Observed-counter assertions | For each target rate, the observed L1 hit ratio must fall within a customer-agreed tolerance of the target; L2 hit ratio is recorded separately. Deviations are recorded and analyzed rather than silently accepted. |

**Fault-injection method (T4/T5):** see Appendix E. In-flight faults are
delivered through a controlled harness that proves injection occurred
before the relevant NVMe completion and records the resulting NVMe
status. Uncontrolled `nvme disconnect` or `configfs` teardown is not
used as an in-flight injection because either may drain in-flight work
before the failure reaches the target, producing a clean lifecycle
result rather than a torn-I/O result.

## 7. Delivery Sequence, Owners, Dependencies, and Gates

Ordered by dependency, not calendar. Stages 0 through 5 are the
implementation milestones for this POC on the CX7 platform, grouped
into the two deliverables in Section 1. MEV and MMG platform work is
separate and follows Appendix D.

| Deliverable | Stage | Focus | Dependencies | Owner |
|---|---|---|---|---|
| D1 — Raw NVMe-oF Baseline | Stage 0 | Kickoff. Freeze contract (namespace, NQNs, ownership boundary). Run all Section 5.2 gates. | Target host reachable; namespace identified; **`nvmet` / `nvmet_rdma` load succeeds** | Lab operator + LMCache engineering |
| D1 | Stage 1 | Lifecycle test T1: 3× repeat cycles + all negative tests. | Stage 0 exit | LMCache engineering |
| D1 | Stage 2 | Baseline block I/O tests T2a / T2b (direct fio/dd against the attached namespace — no LMCache in the path). Establishes remote-NVMe latency/bandwidth baseline. **D1 exit.** | Stage 1 exit; workload assumptions confirmed | LMCache engineering |
| D2 — Durable Remote-L2 | Stage 3 | LMCache remote-L2 integration + WAL implementation + test T3 (Appendix A). First stage at which durable `store()` ACK is claimable. | Stage 2 exit (D1); WAL design frozen | LMCache engineering |
| D2 | Stage 4 | Fault + recovery matrix tests T4, T5 across all c1–c6 cutpoints (Appendix A.3); exercise first-write and overwrite generation semantics (A.1/A.4). | Stage 3 exit | LMCache engineering |
| D2 | Stage 5 | Integrated workload T6 with T7 instrumentation captured in the same run (or a re-run if the harness cannot instrument in-line), results review, and CX7 evidence packaged as inputs to the Architecture A outcome (Section 3.1; the outcome itself is finalized after MEV / MMG follow-on plans land, not by this plan alone). **D2 exit.** | T4/T5 exit | LMCache engineering + customer review |

**Claim-scope gate.** Results emitted before Stage 3 exit are labeled
`deliverable:D1` in the run manifest and may not appear in
customer-facing durability or crash-safety narratives. Only Stages
3–5 artifacts (D2) may substantiate those claims.

**Schedule dependency:** if `nvmet` / `nvmet_rdma` does not load
cleanly on the target host at Stage 0, Stage 1 does not start. The
target owner rebuilds the module or boots a compatible kernel before
Stage 1 resumes.

## 8. Test Matrix and Evidence Artifacts

### 8.1 Test matrix

| # | Deliv. | Test | Inputs / variables | Pass criterion | Evidence | Owner |
|---|---|---|---|---|---|---|
| T1 | D1 | Lifecycle safety | Target NQN, host NQN, listen IP, namespace device; management-plane IP as negative input | 3× repeat setup→connect→I/O→disconnect→teardown all succeed; 5 negative cases fail closed; no residue | Script logs, `nvme list-subsys` before/after, configfs snapshot | LMCache engineering |
| T2a | D1 | Integrity-validation pass | Deterministic pattern writes across the QD sweep; **every** I/O verified via external SHA-256 write/read; measurement NOT timed | Zero mismatch; zero controller reset or unexpected error | Integrity log, controller stats, manifest | LMCache engineering |
| T2b | D1 | Timed performance pass | Page size ∈ {4K, 128K, 256K}; QD ∈ {1, 4, 16, 32, 64}; direction ∈ {read, write}; ≥ 60 s per point. Pre-run seed + post-run digest verify only — no per-I/O readback in the measurement window | Zero unexpected controller resets; pre/post digests match | fio/bench logs, controller stats, manifest | LMCache engineering |
| T3 | D2 | WAL durability (normal path) | 4 KiB, 128 KiB, and 256 KiB stores; QD 1 and 32; BLAKE3 verify on read | Every load hash-matches; PENDING never lookup-visible; ACK follows WAL commit flush (Appendix A step 4a) | Bench logs, WAL replay tool output, manifest | LMCache engineering |
| T4 | D2 | Fault matrix — crash | Controlled in-flight fault harness (Appendix E) at each of **6 WAL cutpoints** (Appendix A.3) — including c5, the committed-but-not-yet-visible boundary; cold restart | On replay: old or new value only; no torn key; no committed extent re-allocated; **every commit-record-durable case reconstructs the new value exactly once, no duplicate map entry** | Fault-matrix report (1 row per cutpoint) with recorded NVMe status per injection, replay tool output | LMCache engineering |
| T5 | D2 | Fault matrix — fabric-side fault | Controlled data-plane fault injection (Appendix E) that provably fires before the relevant NVMe completion; management plane never disturbed | Same invariants as T4; every injection records observed NVMe status; in-flight I/O fails visibly before reconnect, and no degraded-rate operation is accepted | Fault-matrix report with injection-timing evidence, kernel logs | LMCache + lab operator |
| T6 | D2 | Integrated LMCache workload | **KV trace targeting fixed L1 hit rates** (see workload assumptions §6.2): L1 hit rates ∈ {0%, 20%, 50%, 80%}, mixed R/W, ≥ 15 min sustained. Assertions on observed L1 hit counters vs target; L2 hit and eviction rate recorded separately | All functional scenarios pass with recovery guarantees; L1 hit ratio, L2 hit ratio, eviction rate, promotion count, bytes-written-per-reused-prefix, time-to-usable-KV-after-restart all measured and publishable | Bench report, hit-ratio and eviction histograms, controller metrics | LMCache engineering |
| T7 | D2 | CX7 measurements for later IPU comparisons | Instrumented re-run of T6 (or T6 with instrumentation enabled if the harness supports it in a single pass): capture host-CPU (kernel/user/interrupt), initiator-kernel `nvme_rdma` MR/QP churn (path a future IPU platform would replace), CQ event rate, lifecycle-latency breakdown | Baseline sufficient for MEV and MMG platform plans to compare against once each defines its endpoint contract (Appendix D) | T7 baseline report — host-CPU-per-GB and MR/QP churn tables | LMCache engineering |

### 8.2 Evidence artifacts

Every artifact is tagged `initiator-owned-nvmeof` in the run manifest,
keeping this POC's evidence separate from the storage-owned track.

- Lifecycle scripts and focused unit tests.
- A versioned run manifest containing topology, NQNs, namespace
  identity, package/kernel versions, commands, metrics, and log paths.
- Scenario definitions for normal write, crash replay, and reconnect.
- Fault-matrix report with one row per cutpoint and the observed
  recovered state.
- Benchmark report separating L1, remote-L2, and lifecycle metrics.
- T7 baseline report — host-CPU-per-GB and MR/QP churn tables. Feeds
  the MEV and MMG platform plans as their comparison baseline.
- Demo runbook with setup, test, teardown, and rollback commands.

## 9. Success Metrics and Final Decision Criteria

### 9.1 Functional pass/fail (fixed at kickoff)

| Metric | Target |
|---|---|
| Lifecycle cycles at T1 exit | 100% pass, all negative cases fail closed |
| Store/load integrity mismatches | Zero over T3 and T6 runs |
| Fault-matrix cases exposing torn or stale keys | Zero |
| Post-restart allocator collisions with committed extents | Zero |

### 9.2 Performance targets (anchored to the T2b baseline)

T2b captures the direct block-I/O baseline on the CX7 platform:

- L2 read p50/p99 latency at each QD sweep point.
- L2 read aggregate bandwidth at each QD sweep point.
- L2 write p50/p99 latency (block-I/O lower bound; no WAL, no
  checksum, no map publication).

Timed attach-to-first-I/O and reconnect-after-link-loss numbers are
**not** part of T2b (D1). T1 exercises attach and reconnect as
lifecycle steps for correctness (idempotency, no residue); timed
metrics for those steps are captured in the T7 lifecycle-latency
breakdown (D2, Section 8.1) so they land against the integrated
workload rather than a raw-block point.

T2b numbers are the **bare block-I/O baseline**, not the T3/T6
targets. T3 (WAL normal path) and T6 (integrated LMCache workload)
add WAL intent + commit records, BLAKE3 checksum writes, and
map-publication work on top of the block-I/O path; each has its own
overhead budget against T2b.

**Measurement protocol** (applies to every T2b/T3/T6 datapoint):

- ≥ 5 independent runs per (page size, QD, direction) point after a
  discard-first-run warmup.
- Report median with a 95% bootstrap confidence interval; also
  report the min/max observed over the 5 runs.
- A budget is deemed *met* only if the upper CI bound satisfies the
  threshold, not the point median alone.
- Every run is stamped with the manifest so the reader can trace
  which host/kernel/module version produced the number.

**Overhead budgets vs T2b** (fixed at kickoff; overrideable by the
customer only in writing in the run manifest):

- **T3 vs T2b write path:** median p99 latency ≤ T2b p99 + 30% at
  4 KiB QD=1 (WAL + checksum + map publish); ≤ T2b p99 + 15% at
  256 KiB QD=32 (bulk regime absorbs metadata overhead).
- **T3 read path:** median p99 latency ≤ T2b read p99 + 5%. This is
  the BLAKE3-verify budget — the read path adds no WAL work, only
  a hash over the returned buffer. (Replaces the earlier "within
  measurement noise" phrasing, which has no statistical definition.)
- **T6:** T3 per-request targets apply. Steady-state throughput is a
  function of the trace mix and is recorded per run rather than
  fixed here; the run manifest records the target trace, observed
  L1 hit rate, and achieved bytes/sec so the customer can compare
  across runs.

**On when the numbers are decided.** The overhead *budgets* above are
fixed at kickoff. The T2b *baseline* against which they are evaluated
is measured in Stage 2 (Appendix B.3). It is a category error to say
either "targets are TBD after T2b" or "targets are agreed at kickoff"
— both are true of different quantities: budgets at kickoff, baseline
at T2b.

No absolute performance number is quoted in advance. Prior fabric
measurements were on different topology/HCA generations and are not
customer commitments for this POC.

### 9.3 CX7 measurements for later IPU comparisons (T7)

Measurements that make the IPU offload case evaluable once the MEV or
MMG platform plans are defined:

- Initiator host-CPU utilization per GB transferred (kernel/user/interrupt breakdown).
- Target host-CPU utilization per GB transferred.
- Per-block RDMA CQ event count and MR churn rate on the kernel
  `nvme_rdma` / `nvmet_rdma` path.
- Reconnect and lifecycle-event latency breakdown.

These are properties of the CX7 kernel path. They become the baseline
against which the MEV and MMG platform plans measure their offload
effect.

### 9.4 Customer acceptance criteria

| Area | Acceptance criterion |
|---|---|
| Isolation | One initiator accesses only its ACL-approved, dedicated namespace. |
| Lifecycle | Repeated attach/use/detach leaves no leaked target or initiator state. |
| Integrity | Every successful load matches its committed BLAKE3 checksum. |
| Durability | ACK means the WAL commit record, payload, checksum, and map are recoverable after restart. |
| Recovery | All crash and reconnect cutpoints recover without exposing torn or stale keys and without post-replay extent collisions. |
| Evidence | Every result reproducible from the manifest and tagged `initiator-owned-nvmeof`. |
| CX7 comparison baseline | Test T7 delivers the CX7 measurements the later MEV / MMG platform plans compare against. |

### 9.5 Final decision criteria (go/no-go and Architecture A outcome)

T2 (block-I/O baseline) and T3 (WAL normal path) run per the Section 7
timeline once T1 exits. Publishable **customer performance claims** —
including the integrated T6 workload numbers and any comparison against
customer performance targets — require T4 and T5 to first show no
torn-key exposure and no post-replay allocator collisions. Runs of T6
performed before T4/T5 pass are internal characterization only and are
not customer-reportable.

A lifecycle-safety failure, any torn-key exposure, any post-replay
allocator collision, or a missing manifest is a no-go for customer
performance claims.

At CX7 D2 exit, record the CX7 evidence needed to inform the
Architecture A outcome — **advance A**, **optimize A**, or **abandon
A** (see Section 3.1 for the criteria behind each). The outcome
itself is finalized only after the follow-on MEV / MMG platform
evidence lands; this plan is not the sole input to that decision.

## 10. Risks

| Risk | Mitigation or decision |
|---|---|
| Wrong namespace damages data | Require unused device-by-id path and fail closed on mount, partition, or holder detection. |
| Fabric or RDMA instability obscures storage behavior | Gate on bidirectional MTU/connectivity checks before lifecycle or benchmark work. |
| Target-side kernel modules fail to load (BTF / module-version mismatch) | Section 7 hard Stage-0 dependency. Target owner rebuilds `nvmet` / `nvmet_rdma` against the running kernel or boots a compatible kernel before Stage 1 resumes. |
| Initiator HCA/driver refuses MR or MKEY creation | T1 blocks on transport, not lifecycle. Hand off to the fabric team with BDF-scoped diagnostics before iterating on lifecycle scripts. |
| FUA/FLUSH mistaken for an atomic transaction | WAL commit record + flush is the durable barrier. Fault-matrix evidence required before claiming durability. |
| Post-replay extent collision | Allocator is a derived view of committed WAL records; GC touches only unreferenced extents; explicit `RELEASED` records gate reclamation (Appendix A). |
| Overclaiming an IPU demonstration | The CX7 delivery does not put an IPU on the data path. MEV and MMG are separate platform plans. Reports label offload data with the specific platform name. |
| Results compared to Architecture B raw verbs | A and B measure different things (Section 6.1); this plan does not decide A vs B. Report NVMe-oF as a separate host-I/O path. The context table in Section 3 is informational, not a customer performance claim. |
| Later multi-initiator request expands the design | Treat as a separate coordinator/lease/allocator project, not a POC extension. |

## 11. Future Work

- **MEV platform integration.** Re-host Architecture A on the Intel
  IPU MEV release (available today; see Appendix D.5). Measure host-CPU
  reduction, per-block CQ/MR churn reduction, and performance parity
  or regression against the CX7 baseline.
- **MMG platform integration.** Same Architecture A software on
  MMG-400 silicon with IPT running on Falcon cores. Anticipated
  availability early August 2026; Falcon enabling WIP. Same
  enablement measurements as MEV, plus the IPT-vs-Falcon-reliable-
  transport comparison.
- **Copy-on-write publication.** Alternative to WAL that flips a
  single generation pointer. Preserves the same durability invariants
  with different GC and space-overhead characteristics.
- **Multi-initiator shared namespace.** Shared allocator,
  lease/ownership protocol, mapping authority. Reintroduces
  distributed-systems complexity Architecture A removed by design.

## Appendix A: WAL Durable-Commit Protocol (CX7 baseline)

Applies to the CX7 delivery. MEV and MMG platform plans must state
whether they preserve or redefine this protocol given whatever they
change in the initiator-side software path.

The POC commits to **WAL** as the durable-commit protocol, matching
`diagrams/architecture-a-nvmeof-wal-sequence.mmd`. COW is deferred
(see Future Work).

### A.1 Store cutpoints

A store executes the following ordered cutpoints. Steps 1–3 make the
payload and integrity metadata durable on the SSD; step 4 is the
durable commit barrier that atomically transitions the entry to
lookup-visible.

**WAL record schema.** Every WAL record carries
`{key, gen, LBA_range, digest, state}` where:

- `gen` is a monotonically increasing per-key generation counter,
  assigned by the initiator when the store enters step 1. First-write
  starts at `gen=1`; each overwrite of a previously committed key
  uses `gen = (prior committed gen) + 1`.
- `state ∈ {INTENT, COMMITTED, RELEASED}`.

The `(key, gen)` pair uniquely identifies a store attempt across
retries and replays.

1. **WAL intent.** Append `{key, gen, LBA_range, digest, INTENT}` to
   the WAL and flush. The intent alone does not authorize a lookup.
2. **Payload write.** Write the payload with FUA (or a verified flush
   barrier). The block is durable but not yet referenced.
3. **Checksum write.** Write the checksum record with FUA. The
   integrity metadata is durable but not yet referenced.
4. **Durable commit + atomic publish.**
   - 4a. Append a WAL commit record `{key, gen, LBA_range, digest,
     COMMITTED}` and issue a flush that returns before proceeding. Only
     after this flush completes is the store considered durable.
   - 4b. Publish the key→LBA map entry (`gen` now the current
     generation for `key`).
   - 4c. Flip the L1 entry from `PENDING` to `VISIBLE` in the same
     critical section as 4b. **After this flip, no new readers can
     resolve `key` to the prior generation.**
   - 4d. Quiesce readers of the prior generation (overwrite only).
     Wait for any in-flight reads that resolved the prior map entry
     before step 4b to complete. Implementations may use an epoch or
     RCU-style reader counter; the invariant is that no thread holds
     a reference to the prior `LBA_range` after 4d returns.
   - 4e. **Supersede prior generation (overwrite only).** If a prior
     `COMMITTED` record exists for the same `key` at `gen' < gen`,
     append a `{key, gen', LBA_range', digest', RELEASED}` record and
     flush. Only now is the prior extent eligible for GC (see A.2).
     First-write skips 4d/4e.
   - 4f. Return the terminal ACK to the caller.

A periodic metadata checkpoint is not a substitute for the WAL commit
record. FUA/FLUSH on the payload alone is not durability of the store —
without the commit record, replay cannot distinguish an interrupted
write from a completed one.

**Critical visibility boundary.** Once step 4a's flush returns, the
store is durable — a crash after 4a but before 4b/4c must reconstruct
the new value from the WAL on restart (see A.3 c5). For an overwrite,
a crash between 4a and 4e leaves the prior committed extent without
its `RELEASED` record; replay must recognize the new `gen` as
authoritative and retire the stale extent (see A.4).

**Why publish precedes RELEASED (4b–c before 4d–e).** Publishing the
new map entry first means no new reader can resolve `key` to the
prior generation. The 4d quiesce then drains any pre-publish readers
of the prior extent. Only after that quiesce does 4e write RELEASED,
so the allocator (A.2) will never see the prior extent become
GC-eligible while a reader still holds a reference to it. Reversing
this order would race allocator reuse against in-flight reads.

### A.2 Allocator, key→LBA, and GC invariants

Both the allocator and the key→LBA map are derived views of the WAL,
indexed by `(key, gen)`:

- **Live extent set.** An extent `(key, gen, LBA_range)` is live iff a
  `COMMITTED` record exists for `(key, gen)` and no `RELEASED` record
  exists for the same `(key, gen)`.
- **Current value per key.** The current committed value for `key` is
  the live extent with the maximum `gen`. Older live extents for the
  same key (missing their `RELEASED` because of a crash between 4a
  and 4e) are stale-but-recoverable and are retired by post-replay GC
  (see A.4).

On replay the allocator is reconstructed by streaming the WAL. Extents
belonging to WAL intents lacking a commit record are returned to the
free list. GC scans only extents that either (a) have no `COMMITTED`
record, or (b) have a matching `RELEASED` record, or (c) are
non-max-gen live extents for a key with a higher committed generation
(see A.4). A committed max-gen extent cannot be reclaimed until an
explicit `RELEASED` record is written for it. This prevents
post-replay extent reuse from colliding with the current committed
value.

### A.3 Fault-matrix cutpoints (6 boundaries)

Each cutpoint below is exercised in tests T4 and T5. The mapping to
Section A.1 steps is explicit.

| ID | Cutpoint | On-media state | Required recovery outcome |
|---|---|---|---|
| c1 | Before WAL intent flushes | No intent record durable | Key must not appear on restart; no LBAs reserved |
| c2 | After WAL intent flush, before payload FUA | Intent durable; payload not durable | Key must not appear; intent is a GC candidate |
| c3 | After payload FUA, before checksum FUA | Payload durable; no checksum record | Key must not appear; payload orphan is a GC candidate |
| c4 | After checksum FUA, before WAL commit flush (A.1 step 4a) | Payload and checksum durable; no `COMMITTED` record | Key at new `gen` must not appear; matched payload+checksum is a GC candidate. Overwrite: prior committed generation remains authoritative |
| **c5** | **Any crash point after WAL commit flush (A.1 step 4a) but before the terminal ACK returns (A.1 step 4f). Covers three in-memory sub-states — pre-publish (before 4b), post-publish/pre-quiesce (during 4c/4d), and (overwrite only) post-quiesce/pre-RELEASED. On the first-write path there is no 4d/4e, so the c5 window ends when 4c completes and the map is publish-visible; on the overwrite path the window ends when the 4e `RELEASED` flush returns. In both cases the on-media state after restart is the same for a given crash point within the window.** | **`COMMITTED` record for new `gen` durable on media; no `RELEASED` for prior `gen` (overwrite only); in-memory map/L1 is wiped by restart** | **Key MUST appear after restart with digest match on read at the new `gen` — recovery reconstructs the new value from the WAL exactly once. On the overwrite path, replay additionally emits a synthetic `RELEASED` for the prior `gen` and returns its extent to GC (see A.4). First-write has no prior extent to release.** |
| c6 | Post-final-publication and terminal ACK returned (A.1 step 4f). On the overwrite path this additionally requires the `RELEASED` flush (A.1 step 4e) to have returned before 4f; first-write skips 4d/4e entirely and reaches c6 directly from 4c. | Fully durable and visible; prior extent (overwrite only) retired | Key MUST appear with digest match at the new `gen`; prior extent (overwrite only) GC-eligible. First-write has no prior extent to reclaim. |

Cutpoint c5 is the most important boundary because durability is fully
established on media but not yet visible in the running process's data
structures. Recovery must reconstruct exactly the value that would have
been visible had the process not crashed, and must never reconstruct
that value more than once (no duplicate map entry, no double-allocate).

### A.4 Recovery rules

On restart or reconnect, replay the WAL in-order and apply:

1. **Bucket records by `(key, gen)`.** For each bucket, note the
   presence of `INTENT`, `COMMITTED`, and `RELEASED` records.
2. **Discard incomplete generations.** For any `(key, gen)` with an
   `INTENT` but no `COMMITTED`, drop the mapping and return its
   `LBA_range` to the free list. This is the c1–c4 outcome.
3. **Discard invalid commits.** For any `(key, gen)` with a
   `COMMITTED` record whose corresponding payload or checksum record
   is missing or fails verification, treat as incomplete: drop the
   mapping and free the extent on the next GC pass.
4. **Pick current generation per key.** For each `key`, let
   `gen_current = max{gen | (key, gen) is COMMITTED and valid and not
   RELEASED}`. Publish the map entry
   `key → (gen_current, LBA_range, digest)`.
5. **Retire superseded generations.** For every valid `COMMITTED
   (key, gen)` with `gen < gen_current`, treat as retired regardless
   of whether an explicit `RELEASED` record is present (a crash
   anywhere in the c5 window — between 4a and 4e — may have
   prevented the `RELEASED` write). Emit a synthetic `RELEASED` for
   `(key, gen)` at the end of replay and return its extent to GC.
   This is the overwrite-c5 outcome.
6. **Drop `PENDING` L1 entries** — they were never lookup-visible.
7. **Reconstruct the allocator's live-extent set** from the surviving
   `COMMITTED` records (step 4). Do not reclaim any extent referenced
   by a live max-gen `COMMITTED` record.

### A.5 ACK-loss and client retry semantics

Steps 4b/4c publish the key, 4d quiesces prior-generation readers,
4e writes `RELEASED` (overwrite only), and 4f returns the terminal
ACK to the caller. The terminal ACK itself can be lost between
LMCache and the client — a socket close, a client-side timeout, or
a caller-process crash after commit but before the ACK is observed.
This is a client-side contract question, not a WAL cutpoint (c5/c6
already cover the durability/visibility boundary on the LMCache
side).

The rule is: a client retry of a store MUST be idempotent, keyed on the
`{key, digest}` pair. LMCache maintains an in-progress map keyed on
`{key, digest}` alongside the visible map so retries can be routed
against pending state, not just committed state.

- **Absent key** (not in the visible map, not in the in-progress
  map): the retry starts a fresh store on the standard path at
  `gen=1`.
- **`{key, digest}` matches an in-progress store** (WAL intent
  written, commit record not yet durable — the `PENDING` window
  covering steps 1–4a): the retry does NOT start a second store. It
  attaches to the in-progress operation and either blocks until the
  terminal ACK is emitted, or returns a retryable status that
  instructs the client to retry after a bounded delay. It MUST NOT
  allocate a second extent, write a second payload/checksum, or
  append a second WAL intent for the same `(key, gen)`.
- **`{key, digest}` matches an in-progress store with a DIFFERENT
  digest** for the same key: the retry is rejected with a busy /
  retryable status. Two concurrent stores at the same `gen` for one
  key are forbidden; the client must wait until the in-progress
  store either commits or aborts, at which point a subsequent
  differing-digest store is admitted as an overwrite at `gen+1`.
- **`{key, digest}` is `VISIBLE`** and digest matches the committed
  digest: the retry is a no-op and returns success immediately. No
  new WAL record is written.
- **`{key, digest}` is `VISIBLE`** and digest **differs** from the
  committed digest: this is a legitimate overwrite. LMCache admits it
  as a new store at `gen = current_gen + 1`, following the full A.1
  sequence including the 4d reader quiesce and step 4e (`RELEASED`
  for the prior generation).

The in-progress map entry is torn down atomically with step 4c (the
map/L1 flip). A crash anywhere in the c5 window (4a through 4e) is
covered by cutpoint c5: recovery reconstructs the committed value
from the WAL at the new `gen`, publishes it into the visible map,
emits a synthetic `RELEASED` for any prior generation, and the
in-progress map is empty on restart — so a post-restart retry of
the same
`{key, digest}` sees the visible match case above.

Tests T3 and T4 include two retry cases each:

- **Post-ACK retry** (existing case): client-side retry after a
  simulated ACK loss on an already-committed store. Verifies no
  duplicate map entry, no second allocation, no WAL commit-record
  duplication.
- **Pre-commit retry** (new case): client-side retry while the
  original store is still `PENDING` (before commit-record flush).
  Verifies the retry joins the in-progress store (or receives a
  retryable status) rather than allocating twice, and that the
  post-recovery state has exactly one committed extent and one map
  entry.

## Appendix B: Stage-by-Stage Runbook (CX7 baseline)

Applies to the CX7 delivery. MEV and MMG platform plans use their own
runbooks; see Appendix D. Stages are grouped into the two deliverables
introduced in Section 1: Stages 0–2 belong to **Deliverable 1 (Raw
NVMe-oF Baseline)**; Stages 3–5 belong to **Deliverable 2 (Durable
Remote-L2)**.

This appendix is the operator-level detail; Section 7 (delivery
sequence) and Section 8 (test matrix) are the customer-facing view.

### B.1 Stage 0 — Lab Setup & Environment Snapshot [D1]

Bring up the lab and record an environment snapshot: selected
namespace, host NQN, target NQN, data-plane IPs, device-by-id path,
page size, queue depth, and exclusive-ownership assumption. Confirm
the target does not expose an LMCache service. Run all Section 5.2
gates. Exit when the topology diagram, command inventory, ownership
boundary, and rollback owner are reviewed by the customer and lab
operator.

### B.2 Stage 1 — Prove safe NVMe-oF lifecycle [D1]

Exercise the target provisioning and initiator attachment scripts.
The target must reject non-data-plane addresses, require the host NQN
ACL, validate the namespace device, and avoid hard-coded or shared
configfs ports. Initiator discovery must handle both v1 and v2
schemas returned by `nvme list-subsys --json`. Run test T1. Hard
no-go: `nvmet` / `nvmet_rdma` must load; this is enforced at Stage 0.

### B.3 Stage 2 — Remote-L2 I/O baseline [D1 exit]

Attach the namespace and run tests T2a and T2b directly against the
block device (no LMCache in the path). These measurements are an
NVMe-oF host-I/O baseline; they are not raw-verbs or IPU customer
performance results. Performance targets used from Stage 3 forward
are established here. **Deliverable 1 exits after Stage 2:** the
reproducible remote-NVMe baseline is captured, and no durable-cache
claims are attached to D1 artifacts.

### B.4 Stage 3 — Durable initiator-owned publication [D2]

Implement the remote-L2 connector with the WAL protocol in
Appendix A. Run test T3. Exit when a normal store/load cycle
verifies BLAKE3 on read and exposes the key only after the durable
WAL commit and map publish. This is the first stage at which
`store()` ACK may be claimed to mean durable, recoverable cache
state.

### B.5 Stage 4 — Fault and recovery matrix [D2]

Run tests T4 and T5 against the six WAL cutpoints defined in
Appendix A.3 (c1: before WAL intent; c2: after intent before
payload; c3: after payload before checksum; c4: after checksum
before WAL commit flush; **c5: after WAL commit flush, anywhere
before the terminal ACK returns** — sampled at the sub-boundaries
in Appendix E.1 (first-write skips the quiesce/`RELEASED`
sub-boundaries); c6: post-final-publication and ACK, which on the
overwrite path additionally requires the `RELEASED` flush to have
returned). Exit conditions, matching A.3:

- **First-write case** (key had no prior committed value): c1–c4
  yield the key absent after recovery; c5 and c6 yield the fully
  published new value with digest match.
- **Overwrite case** (key had a prior committed value): c1–c4 yield
  the prior committed value; c5 and c6 yield the fully published new
  value with digest match.
- In both cases: no committed extent is observed being re-allocated
  by the post-restart allocator, and c5 reconstructs the new value
  exactly once.

### B.6 Stage 5 — Integration and workload evidence [D2 exit]

Run tests T6 and T7. Exit when all functional scenarios pass with the
recovery guarantees from Stage 4, and the T7 baseline artifact is
complete enough that the MEV and MMG platform plans can compare
against it. **Deliverable 2 exits after Stage 5:** an ACKed cache
entry is recoverable and integrity-verified across all c1–c6
cutpoints, including first-write and overwrite generations.

## Appendix C: Fabric MTU Rationale (CX7 baseline)

Applies to the CX7 RoCEv2 fabric. MEV and MMG platforms have their own
transport-layer MTU behavior; see Appendix D.

The operative value is `active_mtu=IBV_MTU_4096` on both HCAs — this is
the RC-QP path MTU used by every NVMe-oF/RDMA transfer. The Ethernet
link MTU only needs enough headroom for a 4096-byte RDMA payload plus
RoCEv2/UDP/IP headers (~150 bytes), so link MTU ≥ 4200 works. The
current lab fabric runs at 9000; this satisfies the precondition.

**Why `IBV_MTU_4096` and not lower.** RoCEv2 negotiates the QP path MTU
from the link MTU and enumerates only `IBV_MTU_{256, 512, 1024, 2048,
4096}` — there is no `IBV_MTU_9000`. At link MTU 1500 the QP falls
back to `IBV_MTU_1024`; a 4 KiB KV page fragments into four
PSN-numbered packets and shifts RC-QP retransmit-window behavior.

**Why link MTU 9000 is not a functional improvement.** Link headroom
above 4200 is unused by RDMA payloads because RoCEv2 caps the path MTU
at 4096. Measured wire-packet count per KV page is identical at link
MTU 4200 and at link MTU 9000. Run at whichever the fabric is already
provisioned for; do not change switch config for this POC.

**Behavior if `IBV_MTU_4096` cannot be negotiated.** Tests T1, T3, T4,
T5 (functional correctness) may proceed at the observed `active_mtu`
with the manifest labeling the run `mtu:degraded`. Tests T2, T6, T7
(performance and characterization) stop and escalate; sub-4096 numbers
may be captured for characterization but cannot be presented as POC
performance evidence.

**Bidirectional verification** on a link MTU ≥ 9000 fabric: run both
`ping -M do -s 4000 -c 3` and `ping -M do -s 8000 -c 3` between fabric
peers to confirm jumbo-frame headroom is real and not clamped by an
intermediate hop.

## Appendix D: Follow-on Platform Integration Contracts

MEV and MMG are separate integration experiments from the CX7 delivery.
Both keep the Architecture A cache semantics on the initiator (LMCache
engine, key→LBA map, WAL, allocator); each must define whether it
preserves or replaces the Linux NVMe-oF / block-I/O implementation
underneath. This is a software-architecture decision for each platform,
not just a transport swap.

Before either platform starts, the team must approve the endpoint, API,
ownership boundary, and success criteria in D.2 and D.3.

### D.1 Platform overview

| Platform | Silicon | Wire transport | Availability | Purpose |
|---|---|---|---|---|
| MEV | Intel IPU, MEV release | Falcon reliable transport (RoCE-style verbs on top) | Available today (see D.5) | First IPU offload measurement against the CX7 baseline |
| MMG | Intel IPU, MMG-400 | IPT (Intel patented transport, running on Falcon cores) | Anticipated early August 2026; Falcon enabling WIP | Second IPU offload measurement; adds an IPT-vs-Falcon-reliable-transport comparison on top of the offload delta |

### D.2 Endpoint(s) offloaded — must choose (per platform)

| Option | What the IPU replaces | Software boundary |
|---|---|---|
| D-init | Initiator kernel `nvme_rdma` path | Initiator LMCache emits NVMe-oF commands through an IPU-hosted transport (specific API TBD) instead of the Linux block layer |
| D-tgt | Target kernel `nvmet_rdma` path | Target host CPU is bypassed for the fabric-facing side; IPU on the target terminates RDMA and drives the SSD (via NVMe-oF passthrough or a target-side driver) |
| D-both | Both endpoints | Both boundaries above; comparison baseline is the same CX7 run |

MEV and MMG may pick different endpoint options, but each platform's
choice is fixed for the duration of its runs.

### D.3 Contract items each platform must define

- **Software / API boundary** the IPU implements (e.g. an SPDK-style
  NVMe-oF target on the IPU, or a userspace NVMe-oF initiator library
  the LMCache process links against). May differ between MEV and MMG.
- **What remains on each host CPU** after offload (LMCache engine,
  allocator, WAL, map — all of which are initiator-CPU-side
  responsibilities in the CX7 baseline).
- **Preservation vs replacement of Linux NVMe-oF / block-I/O
  implementation.** Explicit statement per platform.
- **Hardware topology.** Record IPUs and SSDs per socket/host, host
  count, link rate, and whether the IPU data path spans hosts. Resolve
  the current `2 IPUs + 8 SSDs per host/socket` and "no multi-host
  IPU" statement against the older `4 IPUs + 16 SSDs` MMG assumption
  before the platform run begins.
- **Data-touch contract.** "Zero CPU data touch" means that, after
  buffer registration and descriptor setup, host CPUs do not load,
  store, or memcpy KV payload bytes. It does **not** mean DRAM bypass:
  registered host DRAM remains the DMA staging area. The IPU must
  stream payloads rather than treat its approximately 32 KiB cache as
  a KV-page store. Evidence includes host CPU profiles and IPU/NIC DMA
  counters for the measured run.
- **Transport and packetization.** State the payload sweep (128 KiB
  and 256 KiB) and the selected transport. For a TCP path, record
  TSO/GSO state and segmentation evidence; for an RDMA path, do not
  use TCP offload counters as data-path evidence.
- **Link-failure policy.** A contracted link failure is fail-fast:
  surface the in-flight I/O error, do not operate at a reduced rate,
  and reconnect only after the link returns at its contracted
  parameters. The fault harness records this ordering.
- **Time correlation.** PTP may align traces approximately across
  hosts, but it is not a latency clock or packet-pacing mechanism.
- **Baseline vs. IPU comparison** — CX7 T2b/T6/T7 numbers are the fixed
  comparison target for both platforms.
- **Success thresholds** the customer sets before each platform runs:
  - Minimum host-CPU-per-GB reduction (e.g. ≥ 40% initiator kernel
    cycles removed).
  - Maximum acceptable throughput regression (e.g. ≤ 10% at QD=32,
    256 KiB).
  - Maximum acceptable latency regression (e.g. ≤ 20% at p99, 4 KiB,
    QD=1).
  - Functional-parity constraint: all CX7 T3/T4/T5 invariants must
    hold on the IPU path.

### D.4 CX7 comparison baseline for IPU platforms

Test T7 in the CX7 delivery captures the full kernel-path baseline:
per-GB host-CPU utilization (kernel/user/interrupt) on both hosts,
initiator kernel `nvme_rdma` MR/QP churn and CQ event rate, target
kernel `nvmet_rdma` counters, and lifecycle-latency breakdown. These
are properties of the CX7 kernel path — not measurements of any IPU
itself.

Each IPU platform compares against **the subset of T7 metrics that
correspond to its selected endpoint(s)** in D.2:

- **D-init** (initiator-side offload): compare against T7's
  initiator-host CPU-per-GB and initiator `nvme_rdma` MR/QP/CQ
  metrics. Target-side T7 numbers are unchanged reference.
- **D-tgt** (target-side offload): compare against T7's target-host
  CPU-per-GB and `nvmet_rdma` counters. Initiator-side T7 numbers are
  unchanged reference.
- **D-both**: compare against both halves of T7.

A platform that preserves the Linux NVMe-oF/block-I/O path on a given
side has no CPU-offload delta to demonstrate on that side. T7 remains
the comparison baseline on the preserved side for **transport-only**
effects (wire transport, MTU, congestion behavior) that traverse the
kernel path unchanged; it just cannot substantiate a CPU-offload claim
where no offload was applied.

### D.5 MEV platform test environment (Intel IPU / MEV)

This is a different lab from the CX7 setup.

| Attribute | Value |
|---|---|
| Hosts | Inspur NF5280M7 (I-P00599 and I-P00600), Xeon Gold 6430, 64C/128T each |
| IPU | Intel IPU, MEV-TS release `IPU IMC MEV-HW-C1-ci-ts.release.2.1.0.11517` |
| RDMA device | `rocep69s0f0` (vendor `0x8086`, part `5202`), driven by `irdma`; control plane via `idpf` (host↔IPU) and Falcon MKP; ACC-side orchestration via `feature_pack.py` / `rtcmd` |
| Wire transport | **Falcon reliable transport** on the wire — not RoCEv2/UDP. Layered as RoCE-style verbs on top of Falcon. |
| Data plane | 100 GbE direct-attach, `200.0.0.0/24`; `active_mtu=IBV_MTU_4096` on both ends |
| Management plane | `10.166.87.x` (I-P00599) / `10.166.86.x` (I-P00600); not disturbed during MEV runs |
| Pre-run sanity baselines (perftest RC, 64 KiB) | `ib_send_bw` ≈ 96.4 Gb/s; `ib_write_bw` ≈ 95.9 Gb/s; `ib_read_bw` ≈ 92.8 Gb/s. Gate MEV runs on reproducing these numbers before running NVMe-oF workloads. |
| Feature pack | FP 0.8 Drop 3 (`feature_pack_release_0_8_drop3.tar.gz`); config.yaml pre-loaded for both hosts to load P4, IDPF, irdma drivers and start Falcon rtcmd with correct PF MAC addresses |
| Perftest | Built from `https://github.com/linux-rdma/perftest.git` per the setup runbook |

### D.6 MMG platform test environment (Intel IPU / MMG-400 + IPT)

Placeholder — silicon and Falcon enabling are WIP; anticipated
availability early August 2026. When the environment is fixed, populate
this section with the same schema as D.5 (hosts, silicon revision, RDMA
device, wire transport, data plane, management plane, pre-run sanity
baselines, driver/feature-pack pointer). The comparison target remains
the same CX7 T7 baseline.

### D.7 Platform-to-platform comparison caveat

Every platform-vs-CX7 delta combines at least two effects, scoped to
the selected endpoint(s) in D.2: (a) offload of the corresponding
kernel path (`nvme_rdma` for D-init, `nvmet_rdma` for D-tgt, both for
D-both), and (b) wire-transport difference between CX7 (RoCEv2/UDP)
and the target platform (Falcon reliable transport on MEV; IPT on
MMG). For a platform that preserves the Linux path on a given side,
there is no offload term for that side — only the transport term
applies to any comparison that crosses that side.

MMG vs CX7 additionally reflects the IPT-vs-RoCEv2 gap. MMG vs MEV
reflects the IPT-vs-Falcon-reliable-transport gap plus any
endpoint-option difference between the two platform runs.

A delta can only be attributed to a single cause when everything
else — endpoint option, hardware, software revision, workload, wire
transport — is held constant across the runs being compared.
Otherwise the delta is a **combined platform delta**, not an
attributed component delta. Reports must say which of the two
applies and, for combined deltas, which endpoint(s) were offloaded.

## Appendix E: Controlled In-Flight Fault Injection Harness (CX7 baseline)

Applies to the CX7 delivery, which uses the 192.168.100 management
plane / 192.168.200 data plane split described in Section 5. MEV and
MMG platform plans have different network planes (Appendix D.5, D.6)
and must adapt this harness accordingly.

Fault injection for T4 and T5 must satisfy three properties. Any harness
that cannot demonstrate all three is not acceptable evidence.

1. **Ordering proof.** The harness must record that the injection fires
   before the NVMe completion that would have made the operation
   visible. A post-hoc "we killed the process near this line" is not
   ordering proof. The fault driver instruments the LMCache code path
   with named checkpoints matching Appendix A.3 cutpoints (c1–c6) and
   fires the injection between checkpoint entry and the next
   corresponding NVMe/WAL syscall.
2. **Recorded NVMe status.** Each injection records the resulting NVMe
   command status (as reported by the kernel or the harness) so that
   the recovery outcome can be correlated with what the fabric actually
   observed. A "no completion recorded" outcome is a valid entry — but
   it must be recorded, not silently missing.
3. **Management-plane isolation.** The injection method must never
   disturb the CX7 platform's 192.168.100 management plane (or the
   equivalent management plane defined per platform in Appendix D), and
   must never rely on host-wide operations (host reboot, module unload)
   as the sole trigger — those either serialize or drain in-flight work
   and yield a clean-lifecycle result rather than an in-flight-fault
   result.

### E.1 Recommended in-flight injection methods

| Cutpoint (Appendix A.3) | Preferred injection |
|---|---|
| c1 (before WAL intent flush) | Kill LMCache process after `write(intent)` returns but before `fsync` returns; harness holds SIGKILL until the syscall boundary |
| c2 (after intent, before payload) | Kill after fsync returns, before payload `io_uring_submit` returns |
| c3 (after payload FUA, before checksum FUA) | Kill after payload CQE handler observes success, before checksum submit |
| c4 (after checksum FUA, before WAL commit flush) | Kill after checksum CQE observed, before commit-record fsync returns |
| **c5 (after WAL commit flush, before terminal ACK)** | **Sample all c5 sub-boundaries in separate runs: (i) kill AFTER commit-record fsync returns, BEFORE the map/L1 flip (4b); (ii) kill AFTER 4b/4c publish, BEFORE the 4d reader-quiesce completes (overwrite only); (iii) kill AFTER quiesce, BEFORE `RELEASED` fsync returns (overwrite only). First-write exercises only (i) — the quiesce/RELEASED sub-boundaries do not exist on that path. All sampled sub-boundaries have the same on-media state after restart for their respective path but exercise different in-memory races** |
| c6 (post-final-publication and ACK; overwrite also requires `RELEASED` flush) | Kill after ACK enqueue to caller |

### E.2 Fabric-side faults (T5, CX7 baseline)

For fabric-side faults on the CX7 platform, the preferred method is a
data-plane-only fault that faults **established** RC-QP traffic
mid-transfer, not just new connection establishment:

- **Primary — `tc netem` on the fabric interface.** Selectively drop
  NVMe capsules or RDMA READ responses on the wire via `tc netem`
  loss (or a scheduled drop of a single PSN range) on the data-plane
  interface (`ens1f1np1` on initiator, `ens1f0np0` on target), timed
  to fire between an Appendix A.3 checkpoint entry and the next
  completion. This is the only method here that interrupts an
  already-established QP mid-transfer, which is what T5 is designed
  to exercise.
- **Secondary — `nvme disconnect` for reconnect-path coverage
  only.** Once a controlled in-flight fault via `tc netem` has been
  demonstrated, `nvme disconnect` is valid as an additional injection
  to test the reconnect path itself. It is not an in-flight torn-I/O
  generator because it drains the initiator queue before the failure
  reaches the target.
- **Not acceptable as a T5 primary — `iptables` DROP on the
  target-side RDMA CM listener port.** The RDMA CM listener handles
  connection establishment only; once a QP is established the data
  path bypasses the listener. Dropping the listener port therefore
  prevents new connections but does not interrupt in-flight I/O on
  existing QPs, and cannot substantiate the T5 in-flight-fault
  claim. Retain only as a connection-attempt-time negative test if
  desired, clearly labeled as such in the run manifest.

The CX7 platform's 192.168.100 management plane is never a fault
injection target. MEV and MMG platform plans must call out the
equivalent constraint for their own management planes.
