# IPU KV Cache Traffic Benchmark Configurations

Test configurations for characterizing KV cache traffic across all transport
scenarios relevant to IPU (Falcon offload) data plane validation.

## System Architecture

```
INITIATOR (GPU host)                          TARGET (Xeon storage server)
┌─────────────────────────────┐               ┌─────────────────────────────────┐
│  NVIDIA RTX 5090            │               │                                 │
│  ┌───────────────────────┐  │               │  ┌─────────┐     ┌───────────┐ │
│  │ vLLM                  │  │               │  │   IPU   │     │ Registered│ │
│  │  Tier 0: GPU HBM      │  │               │  │ Falcon  │◄────│ DRAM      │ │
│  │  Tier 1: Host DRAM    │  │               │  │ offload │     │ (LMCache) │ │
│  └──────────┬────────────┘  │               │  └────┬────┘     └─────┬─────┘ │
│             │ MISS at Tier 1│               │       │ TX             │       │
└─────────────┼───────────────┘               │       ▼               │       │
              │                               │    400G wire           │ miss  │
              │         400G network          │                       ▼       │
              ├───────────────────────────────►│               ┌─────────────┐ │
              │   RDMA / NVMe-TCP / NVMe-RDMA │               │ 8x NVMe SSD│ │
              │                               │               └─────────────┘ │
                                              └─────────────────────────────────┘
```

**LMCache** runs on the target Xeon as the memory management plane:
- Buffer allocation in registered DRAM (PagedTensorMemoryAllocator)
- Eviction policy (LRU/LFU) determines what stays hot
- SSD tiering for cold pages (SPDK/io_uring)
- IPU serves pages from DRAM to wire without CPU touching data bytes

**vLLM** runs on the initiator (NVIDIA RTX 5090 host):
- Manages Tier 0 (GPU HBM) and Tier 1 (host DRAM) natively
- On Tier 1 miss, issues read to target over network

## KV Page Sizing

The KV page is a **fixed size of 256 tokens** (project requirement). Network
traffic patterns depend on model characteristics. Packet size on the wire is
fixed per model.

A single per-layer KV chunk transferred on the wire:

```
page_bytes = kv_size × num_heads × head_size × dtype_bytes × tokens_per_chunk
```

| Model | Layers | KV Heads | Head Size | Dtype | Tokens/Chunk | Page Size |
|-------|--------|----------|-----------|-------|--------------|-----------|
| Llama-3.1 8B | 32 | 8 | 128 | FP8 | 256 | 512 KB |
| Llama-3.1 8B | 32 | 8 | 128 | BF16 | 256 | 1024 KB |
| Llama-3.1 70B | 80 | 8 | 128 | FP8 | 256 | 512 KB |
| Llama-3.1 70B | 80 | 8 | 128 | BF16 | 256 | 1024 KB |
| Llama-3.1 405B | 126 | 8 | 128 | FP8 | 256 | 512 KB |
| Mixtral 8x22B | 56 | 8 | 128 | FP8 | 256 | 512 KB |
| DeepSeek-V3 | 61 | n/a (MLA) | n/a (MLA) | FP8 | 256 | 144 KB |

Note: kv_size = 2 (key + value). Page sizes shown are per-layer transfers.
A full-prefix retrieval for a 70B model is 80 × 512KB = 40 MB (80 layer-chunks).

DeepSeek-V3 uses MLA (Multi-head Latent Attention), not GQA — it caches a
576-element compressed latent per token (shared across heads), not a
per-head KV pair, so "KV Heads"/"Head Size" don't apply and its page size
(144 KB) doesn't follow the `kv_size × num_heads × head_size` formula above.
See `models/deepseek_v3_fp8.yaml` for the derivation.

**IPU-optimized alternative (128 tokens/chunk → 256KB pages):** The 256KB page
size matches the IPU DMA sweet spot (128-256KB). Benchmarks should test both
256 and 128 token configurations to quantify the latency vs throughput tradeoff.
Model configs include `_ipu_alt` fields for the 128-token variant.

## Traffic Characteristics

From workload analysis (LMCache with DeepSeek as proxy reference):
- **TX:RX ratio**: >= 5:1 (target transmits far more than it receives)
- **Burst pattern**: One inference request triggers a burst of N layer-chunk reads
  (e.g., 80 reads for 80-layer model)
- **Read:write ratio**: >= 5:1 (most traffic is serving cached pages)
- **Transaction size**: 512 KB per layer-chunk at 256 tokens/chunk FP8
  (256 KB per layer-chunk at 128 tokens/chunk, IPU DMA-optimal)
- **Queue depth**: Concurrent in-flight reads per connection (IPU DMA slots)

## The Pull Model (Critical Design Choice)

The write path uses a **pull** model: the target always controls when and where
data arrives. The initiator never pushes data — it announces intent, and the
target pulls at its own pace via RDMA Read.

```
Push (rejected):  Initiator fires 256KB at target → target scrambles for buffer
Pull (chosen):    Initiator says "I have 256KB at addr X" → target pulls when ready
```

Why this matters at 400G:
- 50 GB/s inbound = 195K × 256KB writes/sec arriving unpredictably (push)
- 32KB IPU cache cannot absorb bursts
- Pull model: target's drain rate limited only by its own allocation speed + RDMA Read BW
- Write intent queue grows/shrinks safely — never causes data loss

LMCache IS the admission controller. The write path and eviction policy are the
same decision loop: receive intent → evict if needed → allocate → post RDMA Read → index.

**Scope note (2026-07-21):** The pull model described above requires an
LMCache agent on the target/storage node. It cannot be layered on top of a
plain NVMe-oF namespace, because an NVMe-oF target has no cache-level
admission, MR leases, or per-key semantics — it exposes block namespaces
only. Scenarios 1–9 assume the storage-owned pull architecture.

The alternative architecture (initiator-owned LMCache + remote NVMe-oF L2,
no target-side agent) is measured separately in scenario 10+ and documented
at [../../v1/platform/ipu-poc/nvmeof-initiator-only-alternative.md](../../v1/platform/ipu-poc/nvmeof-initiator-only-alternative.md).
Its numbers are not interchangeable with scenarios 1–9.

## Scenarios

| # | Scenario | Transport | Benchmark Tool | Key Metric |
|---|----------|-----------|----------------|------------|
| 1 | L1 DRAM hit, RDMA serve | RDMA (NIXL/UCX) | transfer_channel_benchmark | TX throughput (GB/s) |
| 2 | L1 DRAM hit, NVMe/TCP serve | NVMe/TCP | storage_backend_io | TX throughput + TSO efficiency |
| 3 | L1 miss → SSD fetch → DRAM → RDMA serve | RDMA | storage_backend_io + transfer_channel | Miss penalty latency |
| 4 | L1 miss → SSD fetch → DRAM → NVMe/TCP serve | NVMe/TCP | storage_backend_io | End-to-end miss latency |
| 5 | Write path (pull model, both variants) | RDMA raw + NVMe-oF | transfer_channel + storage_backend_io | Allocation latency, pull throughput |
| 6 | Write path NVMe/TCP (R2T pull) | NVMe/TCP | storage_backend_io | R2T overhead vs raw RDMA |
| 7 | Mixed read/write (5:1 ratio) | RDMA | transfer_channel_benchmark | Sustained bandwidth under churn |
| 8 | Eviction pipeline under full DRAM | LMCache allocator | controller + storage_backend_io + transfer_channel | Pipeline latency (evict→alloc→pull) |
| 9 | Multi-initiator write flood | RDMA | transfer_channel_benchmark (multi-peer) | Admission control, backlog bound |

## Diagrams

See [diagrams.md](diagrams.md) for Mermaid sequence diagrams of each scenario
showing exact data flow between initiator, IPU, DRAM, and SSD.

## File Layout

```
docs/design/tools/ipu_traffic_benchmarks/
├── README.md                          (this file)
├── scenarios/
│   ├── 01_l1_hit_rdma.yaml           Read: hot path, RDMA serve
│   ├── 02_l1_hit_nvme_tcp.yaml       Read: hot path, NVMe/TCP + TSO
│   ├── 03_l1_miss_rdma.yaml          Read: cold path, SSD→DRAM→RDMA
│   ├── 04_l1_miss_nvme_tcp.yaml      Read: cold path, SSD→DRAM→NVMe/TCP
│   ├── 05_write_rdma.yaml            Write: pull model (raw RDMA + NVMe-oF variants)
│   ├── 06_write_nvme_tcp.yaml        Write: NVMe/TCP R2T pull
│   ├── 07_mixed_rdma.yaml            Steady-state: 5:1 read:write mix
│   ├── 08_eviction_pressure.yaml     Pipeline: evict→alloc→pull under full DRAM
│   └── 09_write_flood_admission.yaml Multi-initiator: admission control validation
├── diagrams.md                        Mermaid sequence diagrams for all scenarios
├── models/
│   ├── llama3_8b_fp8.yaml
│   ├── llama3_70b_fp8.yaml
│   ├── llama3_70b_bf16.yaml
│   ├── llama3_405b_fp8.yaml
│   ├── mixtral_8x22b_fp8.yaml
│   └── deepseek_v3_fp8.yaml
├── metrics.md
├── monitoring.md
└── analysis.md
```
