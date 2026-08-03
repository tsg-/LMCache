# Stage 2 smoke — first Python-driven `LocalDiskBackend` numbers on mkp1

**Date:** 2026-08-02 23:50 MST
**Scope:** Stage 0 harness bring-up + first four smoke cells with
`benchmarks/storage_backend_io/storage_backend_io_benchmark.py` against
`LocalDiskBackend`. **Not** Stage 2 sign-off — this is the "does the
Python path collapse at 12 GB/s?" first look. Single drive (not md0).
**Bead:** LMCache-3x2 Stage 0 kickoff.

## TL;DR — the "moment of truth"

The bottleneck story on this rig, after tonight:

| Layer | Read ceiling | Notes |
|---|---:|---|
| 2× PM9A3 local (Config A, prior baseline) | **14.3 GB/s** | media / Gen4 lanes; not the limit here |
| 2× PM9A3 over 100 GbE RoCEv2 (Config C, prior baseline) | **12.0 GB/s** | **wire-bound** at 96 Gbps goodput |
| md0 RAID0 + XFS over the wire (Stage 1 preflight) | **11.99 GB/s** | filesystem costs ≤ 3 % |
| **Python `LocalDiskBackend` O_DIRECT, single wire drive** | **6.21 GB/s** | **89 % of fio single-drive ceiling (6.96 GB/s)** |

**Headline:** the Python control plane does **not** collapse. Plan §4.4.4
predicted a filesystem-contention cliff (~47K files/sec at 12 GB/s with
256 KB chunks), but the *actual* DeepSeek-V3 default config puts
**28 MiB per chunk = ~430 files/sec at 12 GB/s** — two orders of
magnitude fewer `open()/close()` ops. That's why LocalDiskBackend holds
89 % of the raw fio single-drive number on the first Python-driven read.

**Reads look fine.** Writes are ~2× worse under O_DIRECT (see Run 4);
that's the next thing to isolate. And single-drive doesn't answer the
2-drive md0 question, which is where the plan-of-record 12 GB/s wire
ceiling actually lives. Stage 2 continues.

## Environment

- **Host:** mkp1 (I-P00599-B15-P14), Xeon Gold 6430, 251 GiB DRAM
- **Python:** 3.11.7 (RHEL AppStream; NOT the earlier-noted 3.9.18 blocker
  — `python3.11` is preinstalled). New venv at `/root/lmcache-stage2/.venv`.
- **LMCache checkout:** `/root/lmcache-stage2` @ `50b6452` (tonight's commit)
- **Deps:** CPU-only `torch==2.13.0+cpu` from pytorch.org/whl/cpu, then
  `pip install --no-build-isolation -e .` after installing
  `python3.11-devel` for the pybind11 headers.
- **Storage under test:** `/mnt/lmcache-stage2-test` — freshly `mkfs.xfs`ed
  `/dev/nvme2n1` (a single remote NVMe-oF namespace on mkp2 via RoCEv2).
  No md0. No RAID. Single wire, single drive.

## Bytes/op calibration

From `_build_metadata`/`_get_test_data_size_gb` in the harness (verified
by reading source, not just running):

```
bytes_per_op = num_layers × KV × chunk_size × num_heads × head_size × dtype
             = 28       × 2  × 256        × 8         × 128      × 2 (bf16)
             = 29_360_128 bytes = 28.0 MiB per op
```

**All GB/s numbers below use 28 MiB/op**, not the harness's `--chunk-size`
token count.

## Runs

| # | surface | O_DIRECT | ops | conc | write ops/s | write GB/s | read ops/s | read GB/s |
|--:|---------|----------|----:|-----:|------------:|-----------:|-----------:|----------:|
| 1 | tmpfs | no | 128 | 4 | 265.4 | 7.42 | 378.9 | 10.6 |
| 2 | tmpfs | no | 1024 | 32 | 358.3 | 10.0 | 422.3 | 11.8 |
| 3 | XFS on remote nvme2n1 | no | 512 | 32 | 292.9 | 8.19 | 402.2 | 11.3 |
| 4 | XFS on remote nvme2n1 | **yes** | 512 | 32 | 95.1 | 2.66 | 221.9 | 6.21 |

## Findings

1. **Run 3 read = 11.3 GB/s is cached, not wire.** The harness writes and
   then reads the same 14 GiB working set in a single process, no
   `drop_caches`. 251 GiB DRAM catches all of it. Only Run 4 (O_DIRECT)
   forces wire reads.

2. **Wire single-drive fio ceiling on same device:** 6.96 GB/s (fio 256k
   QD16 libaio, direct, measured moments before Run 4). **Run 4 read is
   6.21 GB/s = 89 % of fio.** LocalDiskBackend does **not** collapse under
   `open()/write()/close()` overhead at DeepSeek-V3 chunk sizes.

3. **The plan §4.4.4 pessimism was based on the wrong chunk size.**
   §4.4.4 assumed 256 KB chunks → ~47K files/sec at 12 GB/s. The actual
   DeepSeek-V3 default config is 28 MiB/chunk → ~430 files/sec at
   12 GB/s. Two orders of magnitude fewer inode/dentry ops. The
   filesystem contention story simply doesn't materialize at these
   sizes; it might reappear at smaller chunk sizes or larger fleets.

4. **O_DIRECT drops writes from 8.19 → 2.66 GB/s.** With writeback,
   Linux batches; with O_DIRECT, every write goes to the wire, and 32
   Python threads doing 28 MiB `pwrite` each stalls the pipeline. The
   fio write ceiling on the same drive at direct=1 was measured
   previously at 5.6 GB/s on 2 drives (see baseline doc); the
   Python-direct-write path recovers only 47 % of that on one drive.
   Writes are more Python-limited than reads.

5. **Read at concurrency=32 already saturates single-drive.** No need
   to push higher yet; when we go to md0 RAID0 across 2 drives, we
   expect the Python read to move toward the ~12.0 GB/s wire ceiling.

## Method caveats and gotchas

- **Cache correctness.** The harness's `--write_bench False` is
  write-then-read in one process. Without `drop_caches` (or O_DIRECT),
  read numbers are DRAM speed. Always use `--local-disk-odirect` for a
  wire number, or drop caches between phases.
- **integrity_passed=false in JSON** — the harness reports this whenever
  `--verify-integrity` is not passed. Not a failure.
- **`ulimit -l unlimited`** required for `io_uring` on this host; fio
  fell back to `libaio` for the sanity number.
- **--nr-io-queues=16 workaround** still in effect on this initiator
  (irdma pool exhaustion at default 128), same as prior baselines.

## Next steps for Stage 2

1. Re-assemble md0 on `/dev/nvme{2,3}n1` (superblocks preserved by
   Stage 1 v10 teardown), remount XFS with the exact `sunit=64
   swidth=128` from Stage 1.
2. Rerun O_DIRECT read/write matrix at concurrency ∈ {8, 16, 32, 64,
   128}, ops ∈ {512, 2048}, chunk_size ∈ {128, 256} (halve for a
   smaller-chunk stress test — the interesting question is where the
   inode-contention knee actually is).
3. Add temporary put-queue depth + enqueue-to-completion instrumentation
   to `local_disk_backend.py` per Stage 0 checklist.
4. Correlate with Grafana panels — CPU% per NUMA, RDMA fabric bytes on
   ens2f0, per-drive queue depth — that machinery lit up tonight.

## Artifacts

- `results/stage2-smoke-2026-08-02/all-runs.json` — raw output of all
  four cells above.
- Remote workspace: `mkp1:/root/lmcache-stage2/` (Python 3.11 venv,
  editable install of tonight's commit).
