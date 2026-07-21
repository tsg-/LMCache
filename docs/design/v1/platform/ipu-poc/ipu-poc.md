# IPU RDMA KV Cache Transfer — POC Scope

Proof-of-concept for IPU-accelerated KV cache transfer between a GPU Node
and a KV Cache Node over MMG-400 RDMA at 400 Gb/s.

## Scope boundary

This document describes the **storage-owned pull architecture**: LMCache
runs on both the compute node and the storage node, and the storage-side
LMCache agent owns semantic admission, target-side BLAKE3 verification, MR
leases, and the two-phase L1/L2 eviction contract.

An alternative architecture in which LMCache runs **only on the compute
(initiator) side** and the storage node exports NVMe SSDs over NVMe-oF/RDMA
(no target-side LMCache agent) is under evaluation as a separate track. It
is a different design, not a variant of the storage-owned pull model. See
[nvmeof-initiator-only-alternative.md](nvmeof-initiator-only-alternative.md)
for the impact analysis. Do not conflate its numbers or design decisions
with those in this doc.

## Goals

1. **Wire-speed proof**: Demonstrate sustained ~400 Gb/s (50 GB/s) for
   KV page transfers between two nodes via Intel MMG-400 IPU RDMA.
2. **Full-stack integration**: LMCache control plane (admission, eviction,
   hash index) + IPU RDMA data plane working together under realistic
   DeepSeek-like prefix reuse workload patterns.


## Test Setup

```
    GPU Node (Compute)                      KV Cache Node (Storage)
    ┌──────────────────────────┐            ┌──────────────────────────┐
    │  Xeon GNR-SP             │            │  Xeon GNR-SP             │
    │  GPU / TPU               │            │  4-8x NVMe Gen5          │
    │  IPU (Intel MMG-400)     │            │  IPU (Intel MMG-400)     │
    │                          │            │                          │
    │  lmcache bench (synth)   │            │  LMCache server          │
    │  - DeepSeek trace replay │            │  - Registered DRAM (L1)  │
    │  - Store / retrieve ops  │            │  - SSD cold tier (L2)    │
    └────────────┬─────────────┘            └────────────┬─────────────┘
                 │                                       │
                 │         400 Gb/s RDMA (IPU-to-IPU)    │
                 └───────────────────────────────────────┘
```

**Hardware**: Two physical nodes, each with an MMG-400 IPU as its network
interface (symmetric deployment). No separate NIC — the IPU IS the network.


## In Scope

### Transport Layer

- Real `RdmaTransport` implementation backed by libibverbs on MMG-400
  (replaces `StubRdmaTransport`)
- MR pre-registration of the entire DRAM pool at startup
- RDMA Read (pull model) for writes; RDMA Write (push) for retrieves
- 256KB page size (128 tokens/chunk, IPU DMA sweet spot)

### LMCache Server (KV Cache Node)

- Full stack: PagedTensorMemoryAllocator with registered DRAM pool
- LRU eviction policy
- SSD cold tier via io_uring/SPDK (L2)
- Hash index for prefix deduplication

### Bench Tool (GPU Node)

- Synthetic workload generating DeepSeek-like traffic patterns:
  - Long prefixes (32K-128K tokens)
  - High prefix reuse rate (>80% hit ratio target)
  - Burst of N layer-chunks per request (61 layers for DeepSeek-V3)
- `lmcache bench server --mode ipu` with trace replay support

### Benchmark Scenarios

| # | Scenario | What it proves |
|---|----------|---------------|
| 1 | L1 DRAM hit → RDMA serve | Peak TX throughput (hot path) |
| 3 | L1 miss → SSD fetch → RDMA serve | Miss penalty, SSD→DRAM→wire pipeline |
| 5A | Write (RDMA pull model) | Admission + pull throughput, flow control |
| 7 | Mixed read/write (5:1 ratio) | Sustained BW under concurrent read + write |

### Metrics Collection

- Throughput: GB/s (TX and RX, per-scenario)
- Latency: p50, p95, p99 per operation
- CPU utilization during transfer (target: < 10%)
- Cache hit rate under trace replay
- IPU DMA utilization / queue depth

### Page Sizing

Single per-layer KV chunk on the wire:

```
page_bytes = kv_size × num_kv_heads × head_size × dtype_bytes × tokens_per_chunk
```

POC configuration (DeepSeek-V3 proxy):
- 128 tokens/chunk → 256KB page (IPU DMA optimal)
- 61 layers → 61 × 256KB = 15 MB burst per prefix retrieval
- FP8 dtype


## Success Criteria

| Metric | Target | Rationale |
|--------|--------|-----------|
| Scenario 1 TX throughput | ≥ 45 GB/s sustained | 90% of 400 Gb/s line rate |
| Scenario 5A pull throughput | ≥ 30 GB/s | Pull overhead (intent + RDMA Read RTT) |
| CPU utilization (data path) | < 10% | Proves zero-CPU-data-touch via IPU DMA |
| Cache hit rate (DeepSeek trace) | > 80% | Validates prefix reuse patterns |
| Scenario 3 miss penalty | < 100 µs | SSD fetch + stage + RDMA serve |
| Scenario 7 read degradation | < 5% vs scenario 1 | Reads don't degrade under writes |


## Milestones (4-6 weeks)

### Week 1-2: RDMA Bringup

- libibverbs `RdmaTransport` backend on MMG-400
- MR registration, QP setup, basic connectivity
- Single 256KB page round-trip (wrap → RDMA Read → to_tensor)
- Validate: data integrity, no CPU data copies

### Week 3: Bench Integration

- `lmcache bench server --mode ipu` with real RDMA transport
- Scenario 1 (L1 hit) at target throughput
- Basic metrics pipeline (throughput, latency histogram)

### Week 4-5: Full Suite + Trace Replay

- Scenarios 3, 5A, 7 operational
- DeepSeek prefix reuse trace generation and replay
- SSD tiering under sustained load (eviction pipeline)
- CPU utilization monitoring (perf stat, IPU counters)

### Week 6: Results + Demo

- Performance report with all metrics vs success criteria
- Bottleneck analysis (if targets not met)
- Demo script: single command runs the full benchmark suite
- Documentation of hardware setup, configuration, reproduction steps


## Architecture (POC)

See [ipu.md](../rdma/ipu.md) for the full architecture (layer map, wrapper, transport
protocol) and [lmcache-ipu-pull-model-flow.mmd](../rdma/diagrams/lmcache-ipu-pull-model-flow.mmd)
for the detailed combined store + retrieve sequence diagram. For the
decision-oriented overview, see
[lmcache-ipu-pull-model-flow-hl.mmd](../rdma/diagrams/lmcache-ipu-pull-model-flow-hl.mmd).

Hardware topology: [ipu-poc-test-setup.mmd](ipu-poc-test-setup.mmd)


## Dependencies

| Dependency | Owner | Status |
|------------|-------|--------|
| MMG-400 IPU driver + RDMA support | Hardware team | Available |
| libibverbs headers / libraries | System | Standard OFED |
| Two-node testbed with IPU connectivity | Lab ops | Needs setup confirmation |
| DeepSeek workload trace (synthetic) | This POC | To be generated |
| `lmcache bench server --mode ipu` | This POC (LMCache-bnw) | Blocked on transport |


## Further Optimizations (Post-POC)

| Item | Rationale for deferral |
|------|----------------------|
| **Anjali's IPT Transport** | Depends on IPT SDK / Falcon toolchain availability from Anjali's team |
| **Direct-to-accelerator (GPUDirect RDMA)** | Requires PCIe BAR validation on target GPU platform; optimization over working two-hop path |
| **Multi-initiator write flood** | Single node-pair proves the data plane; scaling validation is a separate effort (scenarios 8, 9) |
| **Real model inference (vLLM)** | POC proves transport layer; vLLM integration is plumbing once the data plane is validated |
| **NVMe/TCP paths** | RDMA is the primary performance path; TCP fallback is a deployment convenience, not a performance target (scenarios 2, 4, 6) |
| **Multi-QP striping** | Start with single QP + high queue depth; stripe only if throughput is bottlenecked |
| **On-chip hash table (Falcon)** | Speculative optimization from IPT design; requires Falcon programmability |


## Related Documents

- [IPU RDMA Platform Backend](../rdma/ipu.md) — full design doc (architecture, wrapper, transport protocol)
- [VerbsRdmaTransport Spec](../rdma/verbs-transport.md) — libibverbs implementation spec (QP state machine, lock protocol, buffer quarantine)
- [High-Level Store+Retrieve Flow](../rdma/diagrams/lmcache-ipu-pull-model-flow-hl.mmd) — admission, retrieval, and commit decisions
- [Combined Store+Retrieve Flow](../rdma/diagrams/lmcache-ipu-pull-model-flow.mmd) — end-to-end sequence diagram
- [Open Questions / Architecture Decisions](ipu-poc-opens.md) — stakeholder alignment deck (NVMe-oF vs RDMA, phasing)
- [Hardware Test Topology](ipu-poc-test-setup.mmd) — two-node lab setup diagram
- [IPU Traffic Benchmark Configs](../../tools/ipu_traffic_benchmarks/README.md) — all 9 scenarios, model configs, monitoring
- [Benchmark Scenario Diagrams](../../tools/ipu_traffic_benchmarks/diagrams.md) — Mermaid sequence diagrams
