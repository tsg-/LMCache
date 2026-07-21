# LMCache Storage-Owned Pull/Serve — CX7 Baseline Test Plan

**Branch:** `ipu-poc`
**Hardware:** bmg0 / bmg1, CX7 RoCEv2, 192.168.200 fabric (200Gbps)
**Status:** CX7 storage-owned pull/serve benchmark — first-order deliverable.

## Scope

This plan measures the **storage-owned pull architecture only** (LMCache
runs on both compute and storage nodes; storage node owns admission,
BLAKE3 verification on commit, MR leases, and L1/L2 eviction).

The **initiator-owned + remote-NVMe-oF-L2 alternative** (no target-side
LMCache agent) is a different architecture and is measured on a separate
track. Its plan lives with the alt-branch scenarios (10+); its numbers
are not interchangeable with the M1/M2 verbs baselines here.
See [../ipu-poc/nvmeof-initiator-only-alternative.md](../ipu-poc/nvmeof-initiator-only-alternative.md).

## Scope note (2026-07-14)

The target architecture is a Xeon **storage node** with RDMA NIC, L1 DRAM,
and attached NVMe. GPU/TPU **inference nodes** register source memory but
own no local NVMe role. The storage node owns admission, allocation,
eviction, pull scheduling, and cache visibility.

The property this plan proves is not "RDMA works." It is:

> Source nodes announce available KV pages; the storage node decides
> whether, when, and where to pull them. No payload reaches storage DRAM
> unless target admission has succeeded.

### Milestone ladder

| M | Focus | Beads |
|---|-------|-------|
| M0 | Trustworthy harness — NUMA/provenance gate, generalized verification | LMCache-awi, LMCache-tj2 |
| M1 | Raw CX7 verbs storage-owned READ + WRITE baselines (transport evidence) | LMCache-dz1, LMCache-y32 |
| M2 | Integrated admission-gated store path + overload/fairness | LMCache-i5e, LMCache-8ey |
| M2 gate | Admission invariants + fault matrix (blocks IPU + Falcon) | LMCache-e95 |
| M3 | NVMe lifecycle — L1↔L2 eviction, staging, recovery, prefix continuity | LMCache-ymb |
| M4 | IPU host-RDMA baseline (Falcon bypassed) — same-hardware A/B for Falcon | LMCache-bjy, LMCache-n7o |
| M5-A | Falcon CQ-offload batch=1 acceptance + batching sweep | LMCache-6v7, LMCache-ao7 |
| M5-B | Falcon-assisted admission/scheduling offload | LMCache-d2z |

### Topology naming (avoid ambiguous headline numbers)

- **CX7 baseline**: both endpoints on Mellanox CX7 (M1, M2, M3).
- **Asymmetric**: source on CX7 or a GPU NIC, storage on IPU host-RDMA. Any
  cross-fabric run must be labeled asymmetric — do NOT call it "IPU
  latency."
- **IPU-to-IPU**: reserved for the case where BOTH endpoints use an IPU
  NIC. That is the only labeling permitted for headline IPU numbers (M4).
- **Falcon-offload**: only when the completion / doorbell (M5-A) or the
  admission scheduler (M5-B) actually runs on Falcon cores. Same-hardware
  IPU host-RDMA (M4) is not a Falcon result.

### Deliverable scoping

Results collected on CX7 (M1):

- ARE a legitimate CX7 storage-owned transport baseline in their own right,
  with hardened verification.
- Are NOT a full storage-cache demonstration — that is M2 (admission-gated
  pull + overload) and M3 (NVMe lifecycle).
- ARE NOT to be labeled as IPU-NIC latency or Falcon-offload latency —
  those are M4 and M5.

### BLAKE3 policy

- **M1 (raw baselines)**: optional in headline timing runs. Require ONE
  separate validation run per (page-size, QD) cell so timing is anchored
  to a verified-correct transfer.
- **M2 and beyond (admission-gated commit)**: mandatory on every committed
  page — the commit gate depends on it.

### NIXL P2P status

The LMCache P2P integration path via NIXL is currently a **diagnostic
path**, P2, out of the primary ladder (LMCache-szh: deterministic
prepared-handle invalidation, working hypothesis `loadRemoteSections`
rollback). Retained for eventual upstream NIXL filing and integration
acceptance testing; not a source of headline CX7 / IPU / Falcon numbers.

---

## Hardware Reference

| Node | Role | NIC | Interface | IP |
|------|------|-----|-----------|-----|
| bmg0 | Source | CX7 mlx5_1 | ens1f1np1 | 192.168.200.3 |
| bmg1 | Puller / storage | CX7 rocep153s0f0 | ens1f0np0 | 192.168.200.4 |

Cross-wire (post-2026-07-21 fabric repair):
bmg0:mlx5_1 ↔ bmg1:rocep153s0f0, RoCEv2. GID indices are per-host —
bmg0 uses GID 4 (`mlx5_1` -> 192.168.200.3), bmg1 uses GID 5
(`rocep153s0f0` -> 192.168.200.4).
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
# NOTE: UCX_NET_DEVICES / LMCACHE_RDMA_GID_INDEX are per-host post-repair.
# bmg0: UCX_NET_DEVICES=mlx5_1:1        LMCACHE_RDMA_GID_INDEX=4
# bmg1: UCX_NET_DEVICES=rocep153s0f0:1  LMCACHE_RDMA_GID_INDEX=5
export UCX_NET_DEVICES=mlx5_1:1
export UCX_MEMTYPE_CACHE=n
export LMCACHE_RDMA_GID_INDEX=4
export NIXL_NET_BACKEND=UCX
export NIXL_PLUGIN_DIR=$HOME/install/nixl/lib/x86_64-linux-gnu/plugins
export LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/nvidia:$HOME/install/ucx/lib:$HOME/install/nixl/lib
```

Python venv: `.venv-ipu/bin/python3` (Python 3.12.3).

---

## Run Isolation (prerequisite for all NVMe tests — M3)

**Bead:** LMCache-qsw (isolation harness), LMCache-ymb (M3 lifecycle).

FSConnector persists files after a run. Without isolation, a second run gets NVMe hits
from prior data — warm retrieve measures local NVMe read latency instead of NIXL transfer
latency, making numbers incomparable.

> **NVMe run isolation is mandatory for M3.** If isolation setup fails, the
> run fails closed — do NOT fall back to shared paths. The raw M1 verbs
> baselines (LMCache-dz1 / LMCache-y32) do NOT depend on FSConnector or
> this NIXL runner and are not gated on the isolation script; the M3
> lifecycle bead is.

**Pattern:** use `scripts/bench_run.sh` to create per-run subdirs and get their paths:

```bash
RUN_LABEL=run_$(date +%Y%m%dT%H%M)

# Single-NVMe run (Tests 1, 2, 4):
RUN_DIR=$(scripts/bench_run.sh --label $RUN_LABEL --paths /mnt/p2p_ext4)
# pass $RUN_DIR as FSConnector base_path

# Dual-NVMe run (Test 3):
readarray -t RUN_DIRS < <(scripts/bench_run.sh --label $RUN_LABEL \
    --paths /mnt/p2p_ext4,/mnt/nvme5)
# pass "${RUN_DIRS[0]},${RUN_DIRS[1]}" as FSConnector base_path
```

**Cleanup flag (optional):** `--cleanup` starts a cleanup owner process. It
prints the directories, keeps them available while the benchmark runs, and deletes
them exactly once when its driver sends `SIGINT` or `SIGTERM`. Default setup mode
retains files, which is useful for intentional warm-NVMe re-runs that measure NVMe
read latency without any network transfer.

```bash
# Retain (default):
RUN_DIR=$(scripts/bench_run.sh --label $RUN_LABEL --paths /mnt/p2p_ext4)

# Start an owner before the benchmark and stop it after the benchmark exits:
scripts/bench_run.sh --label $RUN_LABEL --paths /mnt/p2p_ext4 --cleanup &
CLEANUP_PID=$!
# ... run benchmark ...
kill -TERM $CLEANUP_PID
wait $CLEANUP_PID
```

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
