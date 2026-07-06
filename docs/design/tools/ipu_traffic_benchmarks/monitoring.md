# System Monitoring

Instrumentation to run alongside LMCache benchmarks to validate IPU data plane
behavior. These measurements confirm that the data path works as designed
(zero-copy, DMA-driven, CPU uninvolved in data movement).

## Host-Level Monitoring

### CPU Utilization (validates zero-copy)

```bash
# During benchmark, CPU should NOT be touching KV data bytes.
# High CPU = software is copying/segmenting data = not zero-copy.

# Per-core utilization (expect < 10% on data path cores):
mpstat -P ALL 1

# System calls on benchmark process (should see NO read/write/sendmsg on data):
strace -c -p <benchmark_pid> -e trace=network,read,write
```

**What to look for:**
- CPU utilization on cores handling network I/O should be < 10%
- `sendmsg`/`writev` syscalls should be near zero during data transfer
- If CPU is busy, TSO/offload is not working or data is bouncing through kernel

### Memory Bandwidth (validates DMA path)

```bash
# PCIe bandwidth monitoring (IPU reads from DRAM via PCIe/CXL):
sudo pcm-memory 1

# NUMA node traffic (data should stay on local node):
sudo numastat -p <benchmark_pid>

# Page faults (should be zero during steady-state — memory is pre-registered):
perf stat -e page-faults -p <benchmark_pid>
```

**What to look for:**
- Memory bandwidth on the NUMA node with registered DRAM should match throughput
- Cross-NUMA traffic indicates misplaced buffers
- Page faults during benchmark = registration failed or buffer not pinned

### Network Interface (validates wire utilization)

```bash
# 400G NIC counters:
ethtool -S <iface> | grep -E 'tx_bytes|rx_bytes|tx_packets|rx_packets'

# TSO segment counter (should see large segments, not 1500B frames):
ethtool -S <iface> | grep -E 'tso|gso|gro'

# Real-time bandwidth:
sar -n DEV 1 | grep <iface>

# Or with bpftrace for per-second TX/RX rates:
bpftrace -e 'tracepoint:net:net_dev_xmit { @bytes = sum(args->len); }'
```

**What to look for:**
- TX bytes/sec should match benchmark throughput report
- TSO segment counts should be high (indicates offload is working)
- TX packet sizes should be large (TSO produces ~64KB super-frames)
- Small packets indicate TSO failure → CPU is segmenting

### NVMe/SSD (validates storage tier)

```bash
# NVMe device utilization and latency:
sudo nvme smart-log /dev/nvme0n1

# io_uring completion rate:
sudo bpftrace -e 'tracepoint:io_uring:io_uring_complete { @lat = hist(args->res); }'

# Per-device IOPS and bandwidth:
iostat -x -p nvme0n1 nvme1n1 ... 1

# Queue depth at device level:
cat /sys/block/nvme0n1/inflight
```

**What to look for (miss path scenarios):**
- NVMe read latency should be < 100us for 256KB sequential reads
- Queue depth at device should match benchmark concurrency setting
- All 8 SSDs should show balanced utilization (not single-device bottleneck)
- IOPS × 256KB should approach SSD sequential read bandwidth

## RDMA-Specific Monitoring

```bash
# RDMA verb counters:
rdma statistic show

# Per-QP statistics:
rdma statistic show link <device>/1

# Completion queue depth (should not overflow):
cat /sys/class/infiniband/<device>/ports/1/counters/*

# UCX transport stats (if using UCX backend):
export UCX_LOG_LEVEL=info
export UCX_STATS_DEST=file:ucx_stats.txt
```

**What to look for:**
- RDMA READ completions/sec should match pages/sec from benchmark
- No CQ overflows (would indicate IPU/software not consuming completions fast enough)
- No retransmissions (would indicate network congestion or QP errors)

## LMCache-Internal Monitoring

```bash
# LMCache observability (if running with MP observability enabled):
# Lookup hash events → shows actual cache hit/miss pattern
export LMCACHE_LOG_LEVEL=INFO
export LMCACHE_ENABLE_OBSERVABILITY=1

# The MP_LOOKUP events captured here are what cache_simulator consumes.
# During benchmarks, these show:
# - Hit rate (should match scenario expectations)
# - Eviction frequency
# - Page allocation rate
```

## Monitoring Checklist per Scenario

| Scenario | Must Monitor | Reason |
|----------|-------------|--------|
| 01 (L1 hit, RDMA) | CPU util, NIC TX, RDMA stats | Confirm zero-copy, measure utilization |
| 02 (L1 hit, NVMe/TCP) | CPU util, TSO counters, NIC TX | Confirm TSO offload working |
| 03 (L1 miss, RDMA) | NVMe iostat, memory BW, NIC TX | End-to-end: SSD → DRAM → wire |
| 04 (L1 miss, NVMe/TCP) | NVMe iostat, CPU util, TSO | Full miss path + TCP overhead |
| 05 (Write, RDMA) | NIC RX, memory BW, RDMA stats | Confirm pull model (RX data) |
| 06 (Write, NVMe/TCP) | NIC RX/TX, CPU util | R2T + H2CData pattern |
| 07 (Mixed) | All of above simultaneously | Validates no interference |
| 08 (Eviction) | CPU util on allocator, NVMe write | Eviction path performance |

## Automated Collection Script

```bash
#!/bin/bash
# Run alongside any benchmark scenario
IFACE=${1:-eth0}
DURATION=${2:-60}
OUTDIR=${3:-./monitoring}

mkdir -p $OUTDIR

# Start collectors in background
mpstat -P ALL 1 $DURATION > $OUTDIR/cpu.log &
sar -n DEV 1 $DURATION > $OUTDIR/network.log &
iostat -x 1 $DURATION > $OUTDIR/disk.log &

# Collect NIC stats at start and end
ethtool -S $IFACE > $OUTDIR/nic_stats_before.log

wait  # wait for benchmark to complete

ethtool -S $IFACE > $OUTDIR/nic_stats_after.log

echo "Monitoring data collected in $OUTDIR"
```
