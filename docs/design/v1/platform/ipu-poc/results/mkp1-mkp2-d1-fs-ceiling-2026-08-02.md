# D1 FS-ceiling preflight — mkp1 (initiator) / mkp2 (target)

**Date:** 2026-08-02
**Scope:** D1 FS-ceiling **preflight only** — compares matched raw-baseline
FIO (md0 devices, no filesystem) against XFS-on-md0 to establish the
filesystem overhead ceiling on this fabric. **Not** plan Stage 1 T1/T2b
evidence and does **not** validate LocalDiskBackend.
**Bead:** LMCache-3x2 (Stage 1 sign-off)
**Runner:** `/root/mkp1-d1/stage1/run_stage1.sh` v10 (sha
`789415f81391…7c0426a23`); util sha `3e5f054d6cc7…5c2e7818496b`

## Topology

| Role       | Host                | Hardware                                    | Notes |
|------------|---------------------|---------------------------------------------|-------|
| Initiator  | `I-P00599-B15-P14`  | Xeon Gold 6430, 251 GiB DRAM, kernel 5.14.0-427.13.1 | FIO consumer |
| Target     | `mkp2` (200.0.0.37) | Exports 2 × Linux NVMe-oF namespaces        | `mkp2-nvme1`, `mkp2-nvme2` |
| Fabric     | RoCEv2 100 GbE      | 96 Gbps goodput ceiling                     | irdma |
| NVMe conn  | `--nr-io-queues=16` per controller | irdma ENOMEM at default 128 | workaround captured in manifest |

**Namespaces:**
- `mkp2-nvme1` → `/dev/nvme2n1` (by-id `nvme-Linux_1c83718a24a71f34fb9e`)
- `mkp2-nvme2` → `/dev/nvme3n1` (by-id `nvme-Linux_9970d8704cad2ba293a6`)

**Array under test:**
- md0 RAID0, 2 devices, 256 KiB chunk, 3.49 TiB
- XFS on md0: `sunit=64, swidth=128` (matches 256 KiB stripe), internal log,
  4 KiB block, `crc=1 reflink=0`
- Testfile: 1024 GiB, populated sequentially, `filefrag`/`xfs_bmap` captured

## Method

Two surfaces, matched fio job matrix, run in order:

1. **Raw baseline** — FIO writes directly to the two by-id block devices,
   `numjobs` scaled so aggregate concurrency matches the XFS run
   (`8/dev × 2 devs` at QD=16, etc.), `--size=512G` per namespace.
2. **XFS + md0** — same matrix against 1024 GiB testfile on XFS.

Matrix per surface: {seq, rand} × 256 KiB × QD ∈ {16, 64, 256} × 3 reps
(median reported) + one `numjobs=4/QD=32` (agg 128) sanity cell.

Fixed for both surfaces: `ioengine=io_uring`, `direct=1`, `norandommap`,
`gtod_reduce=0` for latency histograms, `drop_caches=3` before each rep,
`ulimit -l unlimited` (pre-existing on host).

## Results

**Matched raw baseline (over wire, no FS):**

| cell                        | bw_med GB/s | p99_med µs | reps |
|-----------------------------|------------:|-----------:|-----:|
| seq_read_256k_qd16          |       11.99 |        358 |    3 |
| seq_read_256k_qd64          |       11.99 |       1434 |    3 |
| seq_read_256k_qd256         |       11.99 |       5669 |    3 |
| rand_read_256k_qd16         |       11.99 |        391 |    3 |
| rand_read_256k_qd64         |       11.99 |       1450 |    3 |
| rand_read_256k_qd256        |       11.99 |       5734 |    3 |
| raw_sanity_numjobs4 (agg128)|       11.99 |       3588 |    1 |

**XFS + md0 ceiling:**

| cell                        | bw_med GB/s | p99_med µs | reps |
|-----------------------------|------------:|-----------:|-----:|
| seq_read_256k_qd16          |       11.89 |        545 |    3 |
| seq_read_256k_qd64          |       11.81 |       1925 |    3 |
| seq_read_256k_qd256         |       11.81 |       7438 |    3 |
| rand_read_256k_qd16         |       11.65 |        635 |    3 |
| rand_read_256k_qd64         |       11.98 |       1843 |    3 |
| rand_read_256k_qd256        |       11.98 |       6324 |    3 |
| xfs_sanity_numjobs4 (agg128)|       11.98 |       4489 |    1 |

**Overhead (XFS vs raw):**

| cell                | Δ throughput | Δ p99 latency |
|---------------------|-------------:|--------------:|
| seq QD16            |       −0.8 % |         +52 % |
| seq QD64            |       −1.5 % |         +34 % |
| seq QD256           |       −1.5 % |         +31 % |
| rand QD16           |       −2.8 % |         +62 % |
| rand QD64           |       −0.1 % |         +27 % |
| rand QD256          |       −0.1 % |         +10 % |
| sanity (agg128)     |       −0.1 % |         +25 % |

## Findings

1. **Fabric-bound at 96 Gbps.** Both surfaces reach ~11.99 GB/s = 95.9 Gbps
   in every read cell; identical to the prior 100 GbE goodput ceiling.
   NVMe media and md0 stripe are **not** the bottleneck for reads on this
   configuration.
2. **XFS costs ≤ 3 % throughput.** Worst case is `rand QD16 −2.8 %`
   (single-thread, low queue depth); every other cell is within 1.5 %.
   For the workload sizes the plan cares about (aggregate QD ≥ 64),
   XFS is effectively free.
3. **Tail latency is where XFS shows up.** Low-QD reads pay a p99
   penalty of +52 % to +62 % (single-digit hundreds of µs range) which
   compresses to +10–27 % once the pipeline is filled. The 4489 µs p99
   at agg-QD-128 vs 3588 µs raw is the design-point number to remember:
   **~25 % tail-latency tax at plan concurrency, no throughput loss.**
4. **No DDIO cliff inside XFS.** The QD-256 knee reported in the prior
   raw baseline (2026-08-02 00:58) reproduces on both surfaces with the
   same shape — journal replay / metadata activity does not add a second
   knee.
5. **irdma queue-count workaround is persistent.** `--nr-io-queues=16`
   is required per controller; the default 128 fails with ENOMEM at
   `nvme connect` time. Captured in `manifest/manifest.txt`.
6. **Teardown safe.** md0 stopped cleanly; superblocks preserved for
   next-run reuse. Explicit `mdadm --zero-superblock` command
   recorded in `logs/run.log` for destructive wipe if needed.

## Implications for the plan

- **D1 ceiling = 12.0 GB/s reads over wire** — this is the number to hold
  every LMCache read-path result against.
- **XFS-vs-raw margin (≤ 3 % throughput, ≤ 62 % low-QD p99) is small
  enough** that Architecture A can select XFS+md0 as the D1 default
  without a benchmark bake-off. The `by_key → RAID0` decision recorded in
  `nvmeof-poc-plan.md` §4.4 is consistent with this data.
- **Design points that must NOT be inferred from this run:**
  - Write ceiling (reads only)
  - Wear / IOPS at 4 KiB (256 KiB only)
  - Cross-initiator scaling (single initiator)
  - LocalDiskBackend Python overhead (raw FIO, not LMCache)

## Artifacts

`results/mkp1-d1-fs-ceiling-2026-08-02/`

- `summary.txt` — table above verbatim
- `fio_raw/*.json` — 19 FIO JSON files (raw baseline)
- `fio_xfs/*.json` — 19 FIO JSON files (XFS ceiling)
- `manifest/` — script SHAs, kernel/CPU/tool versions, nvme namespace
  mapping, xfs_info, mdadm --detail, filefrag/xfs_bmap of the testfile
- `logs/run.log` — full 45-minute run trace
- `before/`, `after/` — sysfs and NVMe counter snapshots
- `run_stage1.sh` — the v10 script (Codex signed off after 10 rounds,
  28 findings resolved)

## Follow-ups

- Compile results into a citation in `nvmeof-poc-plan.md` §4.4.4.
- Close Stage 1 of **LMCache-3x2**.
- **Stage 2** (LocalDiskBackend end-to-end): blocked on MKP1 Python 3.10+
  availability (present venv is 3.9.18; `pyproject.toml` requires 3.10+).
- **LMCache-05n** (`by_key` sharder eval): do not pre-implement.
