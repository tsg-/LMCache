# Initiator-Owned NVMe-oF L2 POC Plan

## 1. Executive Summary

This plan separates the software architecture decision from the hardware
rollout.

- **Architecture** is a software choice: **A** (initiator-owned NVMe-oF)
  vs **B** (storage-owned pull with a target-side LMCache agent).
- **Platform** names the hardware platforms evaluated separately under
  Architecture A: **CX7** (Mellanox baseline), **MEV** (Intel IPU /
  Falcon), and **MMG** (Intel IPU / MMG-400 / IPT). They are not
  sequential stages.
- **Delivery** is broken into **Stage 0 through Stage 5** (Appendix B).

Architecture A is validated on the CX7 platform first; MEV and MMG are
separate follow-on platform integrations, specified in Appendix D.

Architecture B is on a separate track and not covered here. Architecture
A is the customer-requested path to measure whether an IPU can reduce
transport CPU cost without degrading cache behavior. Success on CX7
means passing the functional and durability gates in Section 9.5 and
producing enough evidence to pick one of the three outcomes in
Section 3 (advance A, optimize A, or retain B).

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

Each requirement is categorized by source and has an owner and
acceptance evidence.

| ID | Requirement | Source | Owner | Acceptance evidence |
|---|---|---|---|---|
| R1 | LMCache runs only on the initiator (compute) host. | Customer requirement | Architecture | No LMCache process on target; verified at kickoff. |
| R2 | Storage node exposes NVMe namespaces over NVMe-oF/RDMA with no LMCache agent. | Customer requirement | Architecture | `nvmet-rdma` config listing + `ps` on target free of LMCache processes. |
| R3 | One initiator has exclusive ownership of one unused namespace. | Customer requirement | Lab operator | ACL restricts to single host NQN; namespace listed as unused pre-attach. |
| R4 | Lifecycle (attach/use/detach) is idempotent and leaks no state. | LMCache architectural decision | LMCache engineering | Lifecycle test T1: 3× repeat cycles + negative tests; final `nvme list-subsys` empty. |
| R5 | Data-plane fabric is isolated from management plane; management SSH is never at risk. | Lab/security constraint | Lab operator | Target refuses management-plane listen IP; management plane MTU/config unchanged across the POC. |
| R6 | Store ACK implies durability across crash/reconnect. | LMCache architectural decision | LMCache engineering | WAL commit + flush ordering (Appendix A); fault-matrix tests T4, T5. |
| R7 | Every load matches its committed checksum; no torn/stale value is lookup-visible. | Customer requirement | LMCache engineering | BLAKE3 verification on every read; recovery invariants (Appendix A). |
| R8 | Results are reproducible from a versioned manifest. | POC measurement objective | LMCache engineering | Run manifest schema (Section 8); every artifact tagged `initiator-owned-nvmeof` in the manifest. |
| R9 | POC evidence produces the CX7 baseline required to evaluate IPU offload in the later MEV / MMG platform plans. | POC measurement objective | LMCache engineering | Test T7 on CX7 captures host-CPU-per-GB and kernel `nvme_rdma` MR/QP churn tables. |

**Requirement → stage/test trace:** R1/R2 → kickoff and T1; R3 → kickoff
and T1; R4 → T1; R5 → guardrails and T1 negatives; R6 → T3, T4, T5;
R7 → T3, T4, T5; R8 → all tests via the manifest; R9 → T7 on CX7, then
MEV / MMG platform plans.

## 3. Architecture A vs B — Decision Table

One-page comparison intended for a decision review. Ends with three
possible outcomes.

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

**Three possible outcomes** after CX7 validation completes (and
optionally after MEV / MMG platform runs):

- **Advance A** as the primary path: correctness, durability, and the
  offload measurements from T7 all support it.
- **Optimize A** first: A passes correctness, but needs targeted work
  (WAL fast-path, allocator batching, or IPU offload delivery) before
  the customer can claim performance numbers.
- **Retain B** as primary: A's results and its offload evidence don't
  close the performance or scale gap against B.

## 4. Architecture A Topology and Component Roles

All cache semantics live on the initiator. Roles not listed do not
participate in Architecture A.

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
condition below is recorded in the run manifest. Each is a hard gate;
failure suspends the schedule.

| Gate | Required evidence | If unmet |
|---|---|---|
| Target kernel modules | `nvmet` and `nvmet_rdma` load cleanly on the target host. | Target owner rebuilds module or boots compatible kernel before Stage 1 starts. See Section 7. |
| Fabric MTU | `active_mtu=IBV_MTU_4096` on both HCAs; link MTU ≥ 4200 (lab: 9000); bidirectional `ping -M do -s 4000` passes. See Appendix C. | Functional stages proceed labeled `mtu:degraded`; performance stages stop and escalate. |
| Management-plane isolation | Target refuses management-plane listen IP; management MTU/config unchanged. | Provisioning refuses to proceed. |
| Dedicated media | A named `/dev/disk/by-id/...` namespace is unused, unmounted, and not a system/data device. | Target provisioning script fails closed. |
| Target isolation | Target binds only data-subnet address, unique configfs port, single-host-NQN ACL. | Target provisioning script fails closed. |
| Lifecycle safety | Setup, status, connect, discover, disconnect, teardown are idempotent; failed setup leaves no residue. | Lifecycle test T1 fails; Stage does not exit. |
| Observability | Initiator kernel/NVMe logs, target `nvmet` logs, `nvme list-subsys --json`, controller statistics, namespace identity captured per run. | Run manifest is incomplete; results not customer-reportable. |

## 6. Scope, Exclusions, and Workload Assumptions

### 6.1 Scope and exclusions

| In scope | Explicitly out of scope |
|---|---|
| One initiator with exclusive ownership of one unused namespace | Multiple initiators sharing a namespace |
| NVMe-oF/RDMA target lifecycle, attach, reconnect, teardown | Target-side LMCache, cache-level admission, or MR leases |
| Initiator-owned L0/L1/L2 tiering on the CX7 platform | IPU/Falcon offload software delivery on MEV and MMG platforms |
| Durable store, load, restart, and reconnect behavior | Comparison against Architecture B raw-verbs figures as if same experiment |
| WAL-based durable publication of data, checksum, and key→LBA mapping | Copy-on-write publication (deferred; see Future Work) |

### 6.2 Workload assumptions

Fixed for the CX7 platform unless the customer confirms alternates at
kickoff.

**Block-I/O baseline (T2a/T2b):**

| Attribute | Value |
|---|---|
| Model / profile | DeepSeek-V3 KV-cache page geometry (or any single model whose page size ≥ 4 KiB). |
| Page size (I/O request size) | 4 KiB for latency runs; 256 KiB for bandwidth runs. |
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
| Test duration | ≥ 15 min sustained per L1-hit-rate point after warmup. |
| Observed-counter assertions | For each target rate, the observed L1 hit ratio must fall within a customer-agreed tolerance of the target; L2 hit ratio is recorded separately. Deviations are recorded and analyzed rather than silently accepted. |

**Fault-injection method (T4/T5):** see Appendix E. In-flight faults are
delivered through a controlled harness that proves injection occurred
before the relevant NVMe completion and records the resulting NVMe
status. Uncontrolled `nvme disconnect` or `configfs` teardown is not
used as an in-flight injection because either may drain in-flight work
before the failure reaches the target, producing a clean lifecycle
result rather than a torn-I/O result.

## 7. Delivery Timeline, Owners, Dependencies, and Gates

Weeks measured from **environment-ready** (all guardrails in Section 5.2
met). No calendar dates — those are owned by the customer and lab
schedule. Stages 0 through 5 are the delivery milestones for this POC
on the CX7 platform. MEV and MMG platform work is separate and follows
Appendix D.

| Week | Stage | Focus | Dependencies | Owner |
|---|---|---|---|---|
| Week 0 | Stage 0 | Kickoff. Freeze contract (namespace, NQNs, ownership boundary). Run all Section 5.2 gates. | Target host reachable; namespace identified; **`nvmet` / `nvmet_rdma` load succeeds** | Lab operator + LMCache engineering |
| Week 1 | Stage 1 | Lifecycle test T1: 3× repeat cycles + all negative tests. | Stage 0 exit | LMCache engineering |
| Weeks 2–3 | Stage 2 + Stage 3 | Baseline I/O tests T2a/T2b and WAL implementation + test T3 (Appendix A) in parallel. | Stage 1 exit; workload assumptions confirmed | LMCache engineering |
| Week 4 | Stage 4 | Fault + recovery matrix tests T4, T5. | Stage 3 exit | LMCache engineering |
| Week 5 | Stage 5 | Integrated workload T6 with T7 instrumentation captured in the same run (or a re-run if the harness cannot instrument in-line), results review, architecture decision. | T4/T5 exit | LMCache engineering + customer review |

**Schedule dependency:** if `nvmet` / `nvmet_rdma` does not load
cleanly on the target host at Stage 0, Stage 1 does not start. The
target owner rebuilds the module or boots a compatible kernel before
the timeline resumes.

## 8. Test Matrix and Evidence Artifacts

### 8.1 Test matrix

| # | Test | Inputs / variables | Pass criterion | Evidence | Owner |
|---|---|---|---|---|---|
| T1 | Lifecycle safety | Target NQN, host NQN, listen IP, namespace device; management-plane IP as negative input | 3× repeat setup→connect→I/O→disconnect→teardown all succeed; 5 negative cases fail closed; no residue | Script logs, `nvme list-subsys` before/after, configfs snapshot | LMCache engineering |
| T2a | Integrity-validation pass | Deterministic pattern writes across the QD sweep; **every** I/O verified via external SHA-256 write/read; measurement NOT timed | Zero mismatch; zero controller reset or unexpected error | Integrity log, controller stats, manifest | LMCache engineering |
| T2b | Timed performance pass | Page size ∈ {4K, 256K}; QD ∈ {1, 4, 16, 32, 64}; direction ∈ {read, write}; ≥ 60 s per point. Pre-run seed + post-run digest verify only — no per-I/O readback in the measurement window | Zero unexpected controller resets; pre/post digests match | fio/bench logs, controller stats, manifest | LMCache engineering |
| T3 | WAL durability (normal path) | 4 KiB and 256 KiB stores; QD 1 and 32; BLAKE3 verify on read | Every load hash-matches; PENDING never lookup-visible; ACK follows WAL commit flush (Appendix A step 4a) | Bench logs, WAL replay tool output, manifest | LMCache engineering |
| T4 | Fault matrix — crash | Controlled in-flight fault harness (Appendix E) at each of **6 WAL cutpoints** (Appendix A.3) — including c5, the committed-but-not-yet-visible boundary; cold restart | On replay: old or new value only; no torn key; no committed extent re-allocated; **every commit-record-durable case reconstructs the new value exactly once, no duplicate map entry** | Fault-matrix report (1 row per cutpoint) with recorded NVMe status per injection, replay tool output | LMCache engineering |
| T5 | Fault matrix — fabric-side fault | Controlled data-plane fault injection (Appendix E) that provably fires before the relevant NVMe completion; management plane never disturbed | Same invariants as T4; every injection records observed NVMe status; reconnect completes within timeout | Fault-matrix report with injection-timing evidence, kernel logs | LMCache + lab operator |
| T6 | Integrated LMCache workload | **KV trace targeting fixed L1 hit rates** (see workload assumptions §6.2): L1 hit rates ∈ {0%, 20%, 50%, 80%}, mixed R/W, ≥ 15 min sustained. Assertions on observed L1 hit counters vs target; L2 hit and eviction rate recorded separately | All functional scenarios pass with recovery guarantees; L1 hit ratio, L2 hit ratio, eviction rate, promotion count, bytes-written-per-reused-prefix, time-to-usable-KV-after-restart all measured and publishable | Bench report, hit-ratio and eviction histograms, controller metrics | LMCache engineering |
| T7 | CX7 measurements for later IPU comparisons | Instrumented re-run of T6 (or T6 with instrumentation enabled if the harness supports it in a single pass): capture host-CPU (kernel/user/interrupt), initiator-kernel `nvme_rdma` MR/QP churn (path a future IPU platform would replace), CQ event rate, lifecycle-latency breakdown | Baseline sufficient for MEV and MMG platform plans to compare against once each defines its endpoint contract (Appendix D) | T7 baseline report — host-CPU-per-GB and MR/QP churn tables | LMCache engineering |

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

### 9.2 Performance targets (TBD after T2b)

T2b captures the direct block-I/O baseline on the CX7 platform:

- L2 read p50/p99 latency at each QD sweep point.
- L2 read aggregate bandwidth at each QD sweep point.
- L2 write p50/p99 latency (block-I/O lower bound; no WAL, no
  checksum, no map publication).
- Attach-to-first-I/O time.
- Reconnect time after transient link loss.

T2b numbers are the **bare block-I/O baseline**, not the T3/T6 targets.
T3 (WAL normal path) and T6 (integrated LMCache workload) add WAL
intent + commit records, BLAKE3 checksum writes, and map-publication
work on top of the block-I/O path; each has its own overhead budget
against T2b, agreed with the customer at kickoff. Absent a customer
override, the default overhead budget is:

- **T3 vs T2b write path:** ≤ 30% additional p99 latency at 4 KiB QD=1
  (WAL + checksum + map publish); ≤ 15% additional at 256 KiB QD=32
  (bulk regime absorbs metadata overhead).
- **T3 read path:** parity with T2b read within measurement noise —
  the read path adds BLAKE3 verify but no WAL work.
- **T6:** T3 targets apply per-request; steady-state throughput target
  is a function of the trace mix (recorded per run, not fixed here).

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

### 9.5 Final decision criteria (go/no-go and A vs B outcome)

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

At the end of the CX7 delivery, pick one: **advance A**, **optimize A**,
or **retain B** (see Section 3 for the criteria behind each).

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
| Results compared to Architecture B raw verbs | Report NVMe-oF as a separate host-I/O path; A and B are compared via the decision table (Section 3), not customer performance claims. |
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

1. **WAL intent.** Append `{key, LBA_range, digest}` to the WAL and
   flush. The intent alone does not authorize a lookup.
2. **Payload write.** Write the payload with FUA (or a verified flush
   barrier). The block is durable but not yet referenced.
3. **Checksum write.** Write the checksum record with FUA. The
   integrity metadata is durable but not yet referenced.
4. **Durable commit + atomic publish.**
   - 4a. Append a WAL commit record `{key, LBA_range, digest,
     COMMITTED}` and issue a flush that returns before proceeding. Only
     after this flush completes is the store considered durable.
   - 4b. Publish the key→LBA map entry.
   - 4c. Flip the L1 entry from `PENDING` to `VISIBLE` in the same
     critical section as 4b.
   - 4d. Return the terminal ACK to the caller.

A periodic metadata checkpoint is not a substitute for the WAL commit
record. FUA/FLUSH on the payload alone is not durability of the store —
without the commit record, replay cannot distinguish an interrupted
write from a completed one.

**Critical visibility boundary.** Once step 4a's flush returns, the
store is durable — a crash after 4a but before 4b/4c must reconstruct
the new value from the WAL on restart. This "committed-but-not-yet-
visible" window is a distinct fault-matrix cutpoint from
"committed-and-visible" (see A.3 c5).

### A.2 Allocator and GC invariants

Allocator state is a derived view of the WAL: at any point the set of
live extents is the union of `LBA_range`s in WAL records with a matching
`COMMITTED` entry, minus extents released by a `RELEASED` record.

On replay the allocator is reconstructed by streaming the WAL. Extents
belonging to WAL intents lacking a commit record are returned to the
free list. GC scans only extents that no `COMMITTED` record references;
a committed extent cannot be reclaimed until an explicit `RELEASED`
record is written. This prevents post-replay extent reuse from colliding
with a prior committed value.

### A.3 Fault-matrix cutpoints (6 boundaries)

Each cutpoint below is exercised in tests T4 and T5. The mapping to
Section A.1 steps is explicit.

| ID | Cutpoint | On-media state | Required recovery outcome |
|---|---|---|---|
| c1 | Before WAL intent flushes | No intent record durable | Key must not appear on restart; no LBAs reserved |
| c2 | After WAL intent flush, before payload FUA | Intent durable; payload not durable | Key must not appear; intent is a GC candidate |
| c3 | After payload FUA, before checksum FUA | Payload durable; no checksum record | Key must not appear; payload orphan is a GC candidate |
| c4 | After checksum FUA, before WAL commit flush (A.1 step 4a) | Payload and checksum durable; no `COMMITTED` record | Key must not appear; matched payload+checksum is a GC candidate |
| **c5** | **After WAL commit flush (A.1 step 4a), before in-memory map/L1 publication (A.1 steps 4b/4c)** | **`COMMITTED` record durable on media; in-memory map/L1 does NOT yet reflect it** | **Key MUST appear after restart with digest match on read — recovery reconstructs the new value from the WAL exactly once** |
| c6 | After map publication (A.1 step 4c) and ACK returned | Fully durable and visible | Key MUST appear with digest match |

Cutpoint c5 is the most important boundary because durability is fully
established on media but not yet visible in the running process's data
structures. Recovery must reconstruct exactly the value that would have
been visible had the process not crashed, and must never reconstruct
that value more than once (no duplicate map entry, no double-allocate).

### A.4 Recovery rules

On restart or reconnect:

- Discard mappings for WAL intents without a commit record; their
  extents return to the free list.
- Discard commit records whose checksum record is missing or invalid;
  their extents return to the free list on the next GC pass.
- Drop all `PENDING` L1 entries — they were never lookup-visible.
- Reconstruct the allocator's live-extent set from the WAL. Do not
  reclaim any extent referenced by a `COMMITTED` record.

### A.5 ACK-loss and client retry semantics

Steps 4b/4c publish the key and 4d returns the terminal ACK to the
caller. The terminal ACK itself can be lost between LMCache and the
client — a socket close, a client-side timeout, or a caller-process
crash after commit but before the ACK is observed. This is a
client-side contract question, not a WAL cutpoint (c5/c6 already cover
the durability/visibility boundary on the LMCache side).

The rule is: a client retry of a store MUST be idempotent, keyed on the
`{key, digest}` pair. LMCache maintains an in-progress map keyed on
`{key, digest}` alongside the visible map so retries can be routed
against pending state, not just committed state.

- **Absent key** (not in the visible map, not in the in-progress
  map): the retry starts a fresh store on the standard path.
- **`{key, digest}` matches an in-progress store** (WAL intent
  written, commit record not yet durable — the `PENDING` window
  covering steps 1–4a): the retry does NOT start a second store. It
  attaches to the in-progress operation and either blocks until the
  terminal ACK is emitted, or returns a retryable status that
  instructs the client to retry after a bounded delay. It MUST NOT
  allocate a second extent, write a second payload/checksum, or
  append a second WAL intent for the same `{key, digest}`.
- **`{key, digest}` matches an in-progress store with a DIFFERENT
  digest** for the same key: the retry is rejected with the same
  contract-violation error as the visible-mismatch case below. A
  single key must not have two concurrent stores with different
  digests.
- **`{key, digest}` is `VISIBLE`** and digest matches the committed
  digest: the retry is a no-op and returns success immediately. No
  new WAL record is written.
- **`{key, digest}` is `VISIBLE`** and digest **differs** from the
  committed digest: client-side contract violation. LMCache returns
  an error to the caller; it does NOT silently overwrite. The event
  is recorded in the run manifest for postmortem.

The in-progress map entry is torn down atomically with step 4c (the
map/L1 flip). A crash between 4a and 4c is covered by cutpoint c5:
recovery reconstructs the committed value from the WAL, publishes it
into the visible map, and the in-progress map is empty on restart —
so a post-restart retry of the same `{key, digest}` sees the visible
match case above.

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
runbooks; see Appendix D.

The Section 7 timeline maps onto delivery stages as follows. This
appendix is the operator-level detail; the main body's timeline and
test matrix (Sections 7–8) are the customer-facing view.

### B.1 Stage 0 — Freeze the contract (Week 0)

Document the selected namespace, host NQN, target NQN, data-plane IPs,
device-by-id path, page size, queue depth, and exclusive-ownership
assumption. Confirm the target does not expose an LMCache service.
Exit when the topology diagram, command inventory, ownership boundary,
and rollback owner are reviewed by the customer and lab operator.

### B.2 Stage 1 — Prove safe NVMe-oF lifecycle (Week 1)

Exercise the target provisioning and initiator attachment scripts.
The target must reject non-data-plane addresses, require the host NQN
ACL, validate the namespace device, and avoid hard-coded or shared
configfs ports. Initiator discovery must handle both v1 and v2
schemas returned by `nvme list-subsys --json`. Run test T1. Hard
no-go: `nvmet` / `nvmet_rdma` must load; this is enforced at Stage 0.

### B.3 Stage 2 — Remote-L2 I/O baseline (Weeks 2–3)

Attach the namespace and run tests T2a and T2b. These measurements
are an NVMe-oF host-I/O baseline; they are not raw-verbs or IPU
customer performance results. Performance targets used from Stage 3
forward are established here.

### B.4 Stage 3 — Durable initiator-owned publication (Weeks 2–3)

Implement the remote-L2 connector with the WAL protocol in
Appendix A. Run test T3. Exit when a normal store/load cycle
verifies BLAKE3 on read and exposes the key only after the durable
WAL commit and map publish.

### B.5 Stage 4 — Fault and recovery matrix (Week 4)

Run tests T4 and T5 against the six WAL cutpoints defined in
Appendix A.3 (c1: before WAL intent; c2: after intent before
payload; c3: after payload before checksum; c4: after checksum
before WAL commit flush; **c5: after WAL commit flush before
in-memory map/L1 publication** — the committed-but-not-yet-visible
boundary; c6: after ACK). Exit conditions, matching A.3:

- **First-write case** (key had no prior committed value): c1–c4
  yield the key absent after recovery; c5 and c6 yield the fully
  published new value with digest match.
- **Overwrite case** (key had a prior committed value): c1–c4 yield
  the prior committed value; c5 and c6 yield the fully published new
  value with digest match.
- In both cases: no committed extent is observed being re-allocated
  by the post-restart allocator, and c5 reconstructs the new value
  exactly once.

### B.6 Stage 5 — Integration and workload evidence (Week 5)

Run tests T6 and T7. Exit when all functional scenarios pass with
the recovery guarantees from Stage 4, and the T7 baseline artifact
is complete enough that the MEV and MMG platform plans can compare
against it.

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
| **c5 (after WAL commit flush, before in-memory publish)** | **Kill AFTER commit-record fsync returns, BEFORE the map/L1 flip is observable — this is the critical boundary** |
| c6 (after ACK) | Kill after ACK enqueue to caller |

### E.2 Fabric-side faults (T5, CX7 baseline)

For fabric-side faults on the CX7 platform, the preferred method is a
data-plane-only fault that does not use `nvme disconnect` or
`configfs` teardown as the primary trigger:

- Drop the target-side RDMA CM listener via a controlled `iptables`
  DROP on the 192.168.200 port for the duration of a single in-flight
  I/O, then release — reproduces a target-side stall without draining
  the initiator queue.
- Selectively drop NVMe capsules or RDMA READ responses on the wire via
  `tc` netem loss on the fabric interface, timed to fire between an
  Appendix A.3 checkpoint entry and the next completion.
- Only after a controlled fault has been proven in-flight is
  `nvme disconnect` valid as a **secondary** injection to test the
  reconnect path itself (not as an in-flight torn-I/O generator).

The CX7 platform's 192.168.100 management plane is never a fault
injection target. MEV and MMG platform plans must call out the
equivalent constraint for their own management planes.
