# Metrics Reference

Metrics collected across all benchmark scenarios, organized by what they tell
the IPU validation team.

## Primary Metrics

### Bandwidth

| Metric | Unit | Source | Meaning for IPU |
|--------|------|--------|-----------------|
| `throughput_gbps` | GB/s | transfer_channel / storage_backend_io | Sustained data rate through DMA path |
| `aggregate_tx_gbps` | GB/s | mixed scenario | Total outbound (read serve) bandwidth |
| `aggregate_rx_gbps` | GB/s | mixed scenario | Total inbound (write pull) bandwidth |
| `tx_rx_ratio` | ratio | derived | Confirms asymmetric traffic (expect >= 5:1) |

**Target:** 50 GB/s (400 Gb/s line rate). Expect 80%+ utilization on hot path.

### Latency

| Metric | Unit | Source | Meaning for IPU |
|--------|------|--------|-----------------|
| `latency_median_us` | microseconds | per-burst completion | Typical request service time |
| `latency_p99_us` | microseconds | tail | Worst-case DMA scheduling delay |
| `read_latency_avg_us` | microseconds | SSD fetch scenarios | Miss penalty budget |
| `pull_latency_avg_us` | microseconds | write scenarios | Buffer allocation + DMA pull time |
| `evict_latency_p99_us` | microseconds | eviction scenario | Time IPU waits for free buffer |

**Context:** Per-page DMA at 256KB over 400Gb/s = ~5us wire time. Anything above
that is overhead (scheduling, allocation, SSD fetch).

### Queue Depth & Concurrency

| Metric | Unit | Source | Meaning for IPU |
|--------|------|--------|-----------------|
| `queue_depth_sustained` | count | in-flight ops | DMA slots needed simultaneously |
| `max_concurrent_ops` | count | peak | IPU descriptor table sizing |
| `burst_overlap_ratio` | ratio | derived | How many requests overlap in time |

**IPU sizing implication:** max_concurrent_ops directly maps to DMA descriptor
table entries required. If this exceeds IPU hardware limit, back-pressure occurs.

### Operations Rate

| Metric | Unit | Source | Meaning for IPU |
|--------|------|--------|-----------------|
| `ops_per_sec` | ops/s | all scenarios | Total page operations/sec |
| `eviction_rate_per_sec` | evictions/s | scenario 8 | Buffer turnover rate |
| `pages_per_second` | pages/s | derived | Sustained page serving throughput |
| `bursts_per_sec` | bursts/s | derived | Inference requests served/sec from cache |

## Derived Metrics

These are computed from primary metrics and provide IPU-relevant insights:

| Derived Metric | Formula | What It Tells You |
|---------------|---------|-------------------|
| `per_layer_latency_us` | `burst_latency / num_layers` | DMA time per 256KB page |
| `bandwidth_utilization` | `throughput / 50.0` | Fraction of 400Gb/s used |
| `miss_penalty_ratio` | `miss_latency / hit_latency` | Cost of SSD tier |
| `nvme_tcp_overhead_us` | `nvme_latency - rdma_latency` | Protocol framing cost |
| `r2t_overhead_us` | `nvme_write - rdma_write` | Pull model extra RTT |
| `read_degradation_pct` | `(solo - mixed) / solo * 100` | Write pressure impact on reads |
| `dma_stall_budget_us` | `1e6 / eviction_rate` | IPU idle time waiting for buffers |

## Per-Scenario Expected Values

| Scenario | Key Metric | Expected Range | Red Flag |
|----------|-----------|----------------|----------|
| 01 (L1 hit, RDMA) | throughput_gbps | 40-50 GB/s | < 35 GB/s |
| 02 (L1 hit, NVMe/TCP) | cpu_utilization | < 10% | > 20% (TSO not working) |
| 03 (L1 miss, RDMA) | read_latency_avg_us | 50-100 us | > 200 us |
| 04 (L1 miss, NVMe/TCP) | nvme_tcp_overhead_us | 5-20 us | > 50 us |
| 05 (Write, RDMA) | queue_depth_sustained | 16-64 | < 8 (under-pipelining) |
| 06 (Write, NVMe/TCP) | r2t_overhead_us | 2-10 us | > 20 us |
| 07 (Mixed) | tx_rx_ratio | 4-6 | < 3 (reads starved) |
| 08 (Eviction) | eviction_rate_per_sec | > 50K | < 25K (allocator bottleneck) |

## Collection Method

All benchmarks report metrics to stdout in structured format. For automated
collection:

```bash
# transfer_channel_benchmark outputs directly:
# Throughput: XX.XX GB/s | Latency best/median/mean: XX.XX/XX.XX/XX.XX ms

# storage_backend_io outputs JSON:
# {"benchmark": "...", "ops_per_sec": ..., "throughput_gbps": ...}

# controller_benchmark outputs:
# Overall RPS: XXXXX | Per-op latency: {...}
```

Parse with standard tools (jq, grep) or the analysis scripts described in
`analysis.md`.
