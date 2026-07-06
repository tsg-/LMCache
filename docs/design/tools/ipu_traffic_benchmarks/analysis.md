# Analysis Guide

How to interpret benchmark results for IPU data plane validation.

## Quick Assessment

After running a scenario, answer these three questions:

1. **Did we hit line rate?** → `throughput_gbps / 50.0` (400Gb/s = 50 GB/s)
2. **Was CPU uninvolved?** → `cpu_utilization < 10%`
3. **Was tail latency bounded?** → `p99 / median < 2.0`

If all three pass, the data path is working as designed.

## Scenario-by-Scenario Analysis

### Scenario 1 & 2: L1 Hit (Hot Path)

**Question answered:** Can we serve at line rate from registered DRAM?

```
IF throughput >= 40 GB/s AND cpu_util < 10%:
    → Zero-copy DMA path is working
    → IPU is reading from DRAM and pushing to wire without CPU

IF throughput < 40 GB/s AND cpu_util < 10%:
    → DMA or PCIe bottleneck (check PCIe lane count, NUMA placement)

IF throughput < 40 GB/s AND cpu_util > 10%:
    → Software is in the data path (TSO not working, or memcpy happening)
    → Check: ethtool -k <iface> | grep tso (must be "on")
    → Check: packet sizes in NIC stats (should be >> 1500B)
```

**RDMA vs NVMe/TCP comparison:**
- RDMA (scenario 1) sets the ceiling — this is minimum-overhead serving
- NVMe/TCP (scenario 2) should be within 15-20% of RDMA
- If gap > 30%, NVMe/TCP framing is consuming too much CPU or bandwidth

### Scenario 3 & 4: L1 Miss (Cold Path)

**Question answered:** What's the cost of a cache miss?

```
miss_penalty = scenario_03_latency - scenario_01_latency

Expected breakdown:
  SSD read (256KB, io_uring):    30-80 us
  DRAM staging:                   1-5 us
  DMA setup + wire:               5-10 us
  Total:                         36-95 us

IF miss_penalty > 200 us:
    → SSD is bottlenecked (check iostat queue depth, device utilization)
    → Or io_uring not using NVMe passthrough (check io_uring_cmd support)

IF miss_penalty < 30 us:
    → Data was in kernel page cache, not actually reading from SSD
    → Run with O_DIRECT or drop caches first
```

**Capacity planning implication:**
```
miss_rate = 1 - (dram_capacity / working_set_size)
effective_latency = hit_latency * (1 - miss_rate) + miss_latency * miss_rate

# Example: 64GB DRAM, 200GB working set, 70B FP8:
# miss_rate = 1 - 64/200 = 0.68
# effective_latency = 5us * 0.32 + 80us * 0.68 = 56us
# → Need more DRAM, or accept reduced throughput
```

### Scenario 5, 6, 9: Write Path (Pull Model)

**Question answered:** Does the pull model maintain flow control under pressure?

The write path is NOT bandwidth-limited — it's latency-sensitive:
```
# New KV generated = active_users × decode_rate × kv_bytes_per_token
# Example: 100 users, 50 tokens/sec decode, Llama-70B FP8:
#   kv_per_token_all_layers = 2 × 8 × 128 × 1 × 80 = 160KB
#   total_write_rate = 100 × 50 × 160KB = 800 MB/s (1.6% of 400Gb/s!)
#
# Writes are cheap in bandwidth. The bottleneck is the allocation pipeline.
```

**Pull model validation (scenario 5 variant comparison):**
```
variant_a_latency (raw RDMA):   control msg → alloc → RDMA Read → done
variant_b_latency (NVMe-oF):    NVMe cmd → alloc → RDMA Read → CQE → done
overhead = variant_b - variant_a

IF overhead > 15 us:
    → NVMe command parsing cost is significant
    → Consider raw RDMA path (skip NVMe layer entirely)
    → This answers the open question for Pat's team

IF overhead < 5 us:
    → NVMe-oF adds negligible cost
    → Use NVMe-oF for operational benefits (standard storage semantics)
```

**Admission control under flood (scenario 9):**
```
IF max_backlog_depth grows unbounded:
    → Drain rate < arrival rate (pipeline bottleneck)
    → Check scenario 8: which pipeline stage is slow?
    → Likely: eviction is synchronous (blocking on SSD flush)

IF max_backlog_depth is bounded AND zero_drops == true:
    → Pull model is working as designed
    → Target stays in control regardless of initiator behavior
    → IPU never receives unexpected data
    → This is the key architectural validation

IF headroom_ratio < 1.0:
    → System is unstable (queue grows forever)
    → Need: faster eviction, more DRAM, or fewer initiators
```

**The firehose comparison (for the report):**
```
# Compute what push model would require:
push_buffer_needed = num_initiators × concurrent_reqs × layers × page_size
                   = 4 × 10 × 80 × 256KB = 800 MB of pre-allocated receive buffers

# What pull model actually needs:
pull_buffer_needed = max_concurrent_pulls × page_size
                   = 64 × 256KB = 16 MB (DMA descriptors × page size)

# Ratio: push needs 50× more pre-allocated buffer for the same workload.
# At 32KB IPU cache, push model is physically impossible.
```

**NVMe/TCP R2T overhead (scenario 6):**
```
r2t_cost = scenario_06_latency - scenario_05_variant_a_latency

# R2T adds exactly 1 half-RTT (target → initiator → target):
# Expected: RTT/2 = ~2.5us for rack-local

IF r2t_cost > 10 us:
    → Either RTT is high (check ping latency)
    → Or R2T processing is slow (NVMe/TCP stack overhead)
    → R2T is the NVMe/TCP equivalent of target-initiated RDMA Read
```

### Scenario 8: Eviction Pipeline

**Question answered:** Can the allocator keep up with page turnover?

```
# The full pipeline per page under pressure:
#   receive_intent → evict_decision → ssd_flush → free_slot → alloc → post_rdma_read → index
#
# With ASYNC flush (required for production):
#   pipeline_latency ≈ 12 us → capacity = 83K pages/sec
#
# With SYNC flush (unacceptable):
#   pipeline_latency ≈ 92 us → capacity = 10K pages/sec
#
# Required: 40K pages/sec (at 5:1 ratio, 200K reads → 40K writes)

IF pipeline_latency_avg > 20 us:
    → Check: is SSD flush synchronous? (must be async)
    → Check: lock contention between eviction thread and read-serve thread
    → Fix: io_uring fire-and-forget for SSD flush

IF read_interference_pct > 20%:
    → Eviction is contending with the read serve path
    → Check: shared locks between eviction policy and read lookup
    → Fix: separate the hot-cache read index from the eviction tracking
    → IPU implication: DMA read completions stall while CPU is evicting

IF flush_queue_stability == false (flush queue growing):
    → SSD write bandwidth < page eviction rate
    → Check: all 8 SSDs balanced? Or single-device bottleneck?
    → Implication: eventually OOM as evicted pages pile up waiting for SSD
    → Fix: backpressure from flush queue → admission control (defer new pulls)
```

**DMA stall budget (for IPU team):**
```
dma_stall_us = pipeline_latency_p99  # worst-case: IPU waits this long for buffer addr
concurrent_dma_needed = dma_stall_us × drain_rate / 1e6

# Example: 50us × 50K/sec = 2.5 concurrent DMA slots for writes
# Plus reads: 80 per burst = need 80+ DMA descriptors
# Total: ~85 DMA descriptors needed simultaneously
# IPU descriptor table must be >= this number
```

### Scenario 7: Mixed Traffic (Production)

**Question answered:** Do reads and writes coexist without interference?

```
read_degradation = (solo_throughput - mixed_throughput) / solo_throughput

IF read_degradation > 15%:
    → Write pulls are contending with read DMA
    → Check: are reads and writes on same QP? (should be separate)
    → Check: PCIe bandwidth (bidirectional may saturate bus)

IF tx_rx_ratio < 4:
    → Writes are consuming too much link bandwidth
    → Either write rate is higher than 5:1 assumption, or read path is stalled
```

## Cross-Scenario Comparisons

### Transport Selection (RDMA vs NVMe/TCP)

| Metric | RDMA (S01/S03/S05) | NVMe/TCP (S02/S04/S06) | Delta | Acceptable? |
|--------|-------|---------|-------|------------|
| Hit throughput | X GB/s | Y GB/s | X-Y | < 20% |
| Miss latency | X us | Y us | Y-X | < 20 us |
| Write latency | X us | Y us | Y-X | < 10 us |
| CPU utilization | X% | Y% | Y-X | < 10% absolute |

If NVMe/TCP overhead is acceptable, it provides operational simplicity
(standard Linux TCP stack, no RDMA fabric requirements).

### Model Scaling

Run scenarios 1, 5, 7 across all model configs. Plot:

```
X-axis: burst_size (layers × page_size)
Y-axis: throughput, latency, queue_depth

Expected:
- Throughput: constant (DMA doesn't care about burst structure)
- Latency: linear in layers (more pages = longer burst)
- Queue depth: higher for larger models (more overlapping pages)
```

If throughput drops with model size, the DMA scheduler is not pipelining
across pages within a burst.

## Summary Report Template

After running the full test matrix, produce:

```
## IPU Traffic Validation Report

### System Under Test
- Target: Xeon [model] + 2× IPU + 8× NVMe SSD
- Initiator: NVIDIA RTX 5090 host
- Network: 400GbE [link type]
- DRAM: [size] GiB registered

### Results Summary
| Scenario | Transport | Throughput | Latency (p50/p99) | CPU% | PASS? |
|----------|-----------|-----------|-------------------|------|-------|
| L1 hit   | RDMA      | XX GB/s   | XX/XX us          | X%   | Y/N   |
| L1 hit   | NVMe/TCP  | XX GB/s   | XX/XX us          | X%   | Y/N   |
| L1 miss  | RDMA      | XX GB/s   | XX/XX us          | X%   | Y/N   |
| ...      | ...       | ...       | ...               | ...  | ...   |

### Key Findings
1. ...
2. ...

### Blocking Issues
1. ...

### Recommendations
1. ...
```
