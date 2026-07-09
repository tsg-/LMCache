# Baseline: NIXL/UCX P2P Pull — CX7 RoCEv2 (192.168.200)

**Status:** Runbook ready; results TBD.

---

## Goal

Establish a measured end-to-end latency baseline for LMCache pull-mode KV
transfer between two server instances over NIXL/UCX, using CX7 RoCEv2 on
the 192.168.200 fabric. This is the reference point against which the IPU
data path will be compared.

---

## Hardware

| Node | Role | RoCEv2 NIC | Interface | IP |
|------|------|-----------|-----------|-----|
| bmg0 | Node1 (source) | CX7 port 1 — mlx5_1 | ens1f1np1 | 192.168.200.3 |
| bmg1 | Node2 (puller) | CX7 port 1 — mlx5_1 | ens1f1np1 | 192.168.200.4 |

Cross-wire: bmg0:mlx5_1 ↔ bmg1:mlx5_1 (192.168.200 subnet, RoCEv2 GID index 3).

---

## Environment

Set on **both nodes** before starting anything:

```bash
export UCX_TLS=rc,sm
export UCX_NET_DEVICES=mlx5_1:1
export UCX_MEMTYPE_CACHE=n
export LMCACHE_RDMA_GID_INDEX=3
export NIXL_NET_BACKEND=UCX
export NIXL_PLUGIN_DIR=$HOME/install/nixl/lib/x86_64-linux-gnu/plugins
export LD_LIBRARY_PATH=/usr/lib/x86_64-linux-gnu/nvidia:$HOME/install/ucx/lib:$HOME/install/nixl/lib
```

Python venv on both nodes: `.venv-ipu/bin/python3` (Python 3.12.3).

---

## Procedure

### Step 1 — Start coordinator on bmg0

```bash
# bmg0
lmcache coordinator --host 0.0.0.0 --port 9300
```

Leave running in its own terminal.

### Step 2 — Start MP server on bmg0 (source node)

```bash
# bmg0
lmcache server \
  --host 0.0.0.0 --port 5601 \
  --coordinator-url http://192.168.200.3:9300 \
  --p2p-advertise-url nixl://192.168.200.3:5605 \
  --p2p-listen-url nixl://0.0.0.0:5605
```

### Step 3 — Start MP server on bmg1 (pulling node)

```bash
# bmg1
lmcache server \
  --host 0.0.0.0 --port 5601 \
  --coordinator-url http://192.168.200.3:9300 \
  --p2p-advertise-url nixl://192.168.200.4:5605 \
  --p2p-listen-url nixl://0.0.0.0:5605
```

### Step 4 — Populate Node1 (cold store on bmg0)

```bash
# bmg0 — stores keys 100–104 into bmg0's L1 host-memory tier
lmcache bench server \
  --rpc-url tcp://127.0.0.1:5601 \
  --url http://127.0.0.1:8080 \
  --mode gpu \
  --transfer-mode lmcache_driven \
  --num-tokens 512 \
  --start 100 --end 105 \
  --interval 0
```

After this, bmg0 holds keys 100–104. bmg1 has none.

### Step 5 — Pull benchmark on bmg1

```bash
# bmg1 — same keys, no local copy → every request pulls from bmg0 over NIXL/UCX
lmcache bench server \
  --rpc-url tcp://127.0.0.1:5601 \
  --url http://127.0.0.1:8080 \
  --mode gpu \
  --transfer-mode lmcache_driven \
  --num-tokens 512 \
  --start 100 --end 105 \
  --interval 0
```

The **warm retrieve** latency in the output is the transfer time. The cold
lookup includes coordinator round-trip and connection setup and is not
a transfer-only number.

---

## Cleanup

```bash
# both nodes — clear ports before re-running
fuser -k 5601/tcp 5605/tcp 9300/tcp
```

---

## Results

<!-- Fill in after running -->

| Metric | Value |
|--------|-------|
| Requests | |
| Chunk hit rate | |
| Checksum pass rate | |
| Cold lookup mean | |
| Warm retrieve mean | |
| Warm retrieve p50 | |
| Warm retrieve p99 | |

---

## Notes

- Pull path only: bmg1 misses locally, discovers bmg0 via coordinator, pulls
  over NIXL/UCX. bmg0 is passive throughout.
- KV data lives in host memory (L1 tier). No GPUDirect RDMA in this test.
- Push mode and the IPU data path are not covered here.
