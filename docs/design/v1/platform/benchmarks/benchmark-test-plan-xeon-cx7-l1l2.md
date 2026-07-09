# LMCache IPU PoC — Benchmark Test Plan

**Branch:** `ipu-poc`
**Hardware:** bmg0 / bmg1, CX7 RoCEv2, 192.168.200 fabric (200Gbps)
**Status:** Runbook ready; results TBD.

---

## Hardware Reference

| Node | Role | NIC | Interface | IP |
|------|------|-----|-----------|-----|
| bmg0 | Source | CX7 mlx5_1 | ens1f1np1 | 192.168.200.3 |
| bmg1 | Puller / storage | CX7 mlx5_1 | ens1f1np1 | 192.168.200.4 |

Cross-wire: bmg0:mlx5_1 ↔ bmg1:mlx5_1, RoCEv2 GID index 3.
Effective bandwidth ceiling: ~23–24 GB/s (200Gbps link, RoCEv2).

NVMe devices on bmg1 (SOLIDIGM SB5PH27X019T Gen5, ~12 GB/s write / ~14 GB/s read):

| Device | Mount | Status |
|--------|-------|--------|
| nvme4n1p2 | /mnt/p2p_ext4 | ext4, mounted |
| nvme5n1 | /mnt/nvme5 | format ext4, mount before use |

---

## Environment (both nodes)

```bash
export UCX_TLS=rc,sm
export UCX_NET_DEVICES=mlx5_1:1
export UCX_MEMTYPE_CACHE=n
export LMCACHE_RDMA_GID_INDEX=3
export NIXL_NET_BACKEND=UCX
export NIXL_PLUGIN_DIR=$HOME/install/nixl/lib/x86_64-linux-gnu/plugins
export LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/nvidia:$HOME/install/ucx/lib:$HOME/install/nixl/lib
```

Python venv: `.venv-ipu/bin/python3` (Python 3.12.3).

---

## Run Isolation (prerequisite for all NVMe tests)

**Bead:** LMCache-qsw

FSConnector persists files after a run. Without isolation, a second run gets NVMe hits
from prior data — warm retrieve measures local NVMe read latency instead of NIXL transfer
latency, making numbers incomparable.

**Pattern:** use a per-run subdirectory as the FSConnector `base_path`:

```bash
BASE=/mnt/p2p_ext4
RUN_LABEL=run_$(date +%Y%m%dT%H%M)
RUN_DIR=$BASE/lmcache_bench/$RUN_LABEL
mkdir -p $RUN_DIR
# pass $RUN_DIR as FSConnector base_path
```

**Cleanup flag (optional):** pass `--cleanup` to delete `$RUN_DIR` after the run.
Default is to retain files — useful for intentional warm-NVMe re-runs that measure
NVMe read latency without any network transfer.

---

## Token Sizes

All benchmarks run the same five token counts: **32, 64, 128, 256, 512**.
This produces a latency-vs-transfer-size curve comparable across L1, L2, and tiered tests.

---

## Test Matrix

| # | Bead | Test | Tier config on bmg1 | NVMe |
|---|------|------|---------------------|------|
| 1 | LMCache-die | L1 DRAM baseline | LocalCPUBackend only | — |
| 2 | LMCache-zfs | L2 single NVMe baseline | FSConnector → /mnt/p2p_ext4 | nvme4n1p2 |
| 3 | LMCache-q9c | L2 dual-NVMe striped throughput | FSConnector → /mnt/p2p_ext4,/mnt/nvme5 | nvme4n1p2 + nvme5n1 |
| 4 | LMCache-ss2 | L1+L2 tiered (DRAM + NVMe) | LocalCPUBackend + FSConnector → /mnt/p2p_ext4 | nvme4n1p2 |

Run order: 1 → 2 → 3, 4 (3 and 4 can run in parallel after 2).

---

## Test 1 — L1 DRAM Baseline (LMCache-die)

**Goal:** latency for NIXL pull from bmg0 host DRAM into bmg1 host DRAM.
No NVMe involved. Reference point for all other tests.

**Bandwidth note:** test is latency-bound at these page sizes; 200Gbps link will not be
saturated until ~256–512 token concurrent load.

### Procedure

```bash
# bmg0 — terminal 1
lmcache coordinator --host 0.0.0.0 --port 9300

# bmg0 — terminal 2
lmcache server \
  --host 0.0.0.0 --port 5601 \
  --coordinator-url http://192.168.200.3:9300 \
  --p2p-advertise-url nixl://192.168.200.3:5605 \
  --p2p-listen-url nixl://0.0.0.0:5605

# bmg1
lmcache server \
  --host 0.0.0.0 --port 5601 \
  --coordinator-url http://192.168.200.3:9300 \
  --p2p-advertise-url nixl://192.168.200.4:5605 \
  --p2p-listen-url nixl://0.0.0.0:5605

# For each NUM_TOKENS in 32 64 128 256 512:

# bmg0 — populate
lmcache bench server \
  --rpc-url tcp://127.0.0.1:5601 \
  --url http://127.0.0.1:8080 \
  --mode gpu \
  --transfer-mode lmcache_driven \
  --num-tokens $NUM_TOKENS \
  --start 100 --end 105 --interval 0

# bmg1 — pull benchmark (no local copy)
lmcache bench server \
  --rpc-url tcp://127.0.0.1:5601 \
  --url http://127.0.0.1:8080 \
  --mode gpu \
  --transfer-mode lmcache_driven \
  --num-tokens $NUM_TOKENS \
  --start 100 --end 105 --interval 0
```

### Results

| Tokens | Cold lookup mean | Warm retrieve mean | p50 | p99 |
|--------|------------------|--------------------|-----|-----|
| 32 | | | | |
| 64 | | | | |
| 128 | | | | |
| 256 | | | | |
| 512 | | | | |

---

## Test 2 — L2 Single NVMe Baseline (LMCache-zfs)

**Goal:** latency for NIXL pull from bmg0 into bmg1 FSConnector (single NVMe).
Per-device write ceiling ~12 GB/s is the bottleneck vs 25 GB/s link.

**Pre-run:** apply run isolation pattern above; use `/mnt/p2p_ext4/lmcache_bench/$RUN_LABEL`.

### Procedure

Same as Test 1 except bmg1 lmcache server is configured with FSConnector
pointing at the per-run subdir on `/mnt/p2p_ext4`.

### Results

| Tokens | Cold lookup mean | Warm retrieve mean | p50 | p99 |
|--------|------------------|--------------------|-----|-----|
| 32 | | | | |
| 64 | | | | |
| 128 | | | | |
| 256 | | | | |
| 512 | | | | |

---

## Test 3 — L2 Dual-NVMe Striped Throughput (LMCache-q9c)

**Goal:** verify that two Gen5 NVMes via FSConnector comma-separated `base_path`
approaches the 200Gbps link ceiling (~24 GB/s write across both devices vs 25 GB/s link).

**Depends on:** Test 2 (single-NVMe baseline for comparison).

**Pre-run setup on bmg1:**
```bash
mkfs.ext4 /dev/nvme5n1
mount /dev/nvme5n1 /mnt/nvme5
```

FSConnector shards by `hash(key) % 2` — keys distribute evenly across both mounts.
No separate LMCache instances needed.

**Pre-run:** apply run isolation pattern for both mounts:
```bash
mkdir -p /mnt/p2p_ext4/lmcache_bench/$RUN_LABEL
mkdir -p /mnt/nvme5/lmcache_bench/$RUN_LABEL
# pass "/mnt/p2p_ext4/lmcache_bench/$RUN_LABEL,/mnt/nvme5/lmcache_bench/$RUN_LABEL"
# as FSConnector base_path
```

### Results

| Tokens | Warm retrieve mean | p50 | p99 | Peak throughput (GB/s) |
|--------|--------------------|-----|-----|------------------------|
| 32 | | | | |
| 64 | | | | |
| 128 | | | | |
| 256 | | | | |
| 512 | | | | |

**Expected:** throughput plateaus at ~24–25 GB/s if link-saturated; at ~24 GB/s if NVMe-write-bound.

---

## Test 4 — L1+L2 Tiered (LMCache-ss2)

**Goal:** characterize LMCache automatic L1→L2 tiering with a mixed working set.
L1=LocalCPUBackend (bmg1 DRAM), L2=FSConnector (/mnt/p2p_ext4).

**Depends on:** Test 2 (L2-only baseline for comparison).

**Key behavior to verify:**
- On L1 miss: falls through to L2 (NVMe) automatically.
- Prefix continuity: if a middle chunk was evicted from L1 to L2, the full prefix is not
  served partially — confirm no silent truncation.
- L2→L1 promotion on hit: **not implemented** (noted TODO in storage_manager.py line 218).
  Repeated access to evicted chunks always goes to NVMe.

**Setup:** set L1 size small enough to force some eviction to L2 so both tiers are exercised
with the chosen key range.

**Pre-run:** apply run isolation pattern above.

### Results

| Tokens | L1 hit p50 | L1 hit p99 | L2 fallthrough p50 | L2 fallthrough p99 |
|--------|------------|------------|--------------------|--------------------|
| 32 | | | | |
| 64 | | | | |
| 128 | | | | |
| 256 | | | | |
| 512 | | | | |

**Prefix continuity check:** pass / fail (note any partial-prefix bugs here).

---

## Cleanup

```bash
# both nodes — clear ports before re-running
fuser -k 5601/tcp 5605/tcp 9300/tcp

# bmg1 — remove a specific run's NVMe data
rm -rf /mnt/p2p_ext4/lmcache_bench/$RUN_LABEL
rm -rf /mnt/nvme5/lmcache_bench/$RUN_LABEL
```
