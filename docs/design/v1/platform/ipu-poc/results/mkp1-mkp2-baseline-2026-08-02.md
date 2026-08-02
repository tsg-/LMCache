# MKP1↔MKP2 §4.1 Pre-flight Baseline — 2026-08-02

> **Status: pre-flight sanity run, not the plan-conformant T7 baseline.**
> This run uses 15-second FIO cells with no repetition, on 2 SSDs, over
> **RoCEv2 on 100 GbE** (E810 `irdma`). It is **not Falcon**, does not
> meet plan T2b protocol (60 s latency, 5-min bandwidth, 5 repetitions),
> and cannot be cited as the D1/T7 kernel-path baseline for the offload
> comparison. It is retained here as a smoke test that (a) the fabric
> comes up cleanly, (b) the harness is functional end-to-end, and (c)
> the read side can saturate 100 GbE. Rerun to protocol before any
> customer-visible baseline claim.

## Setup

| Component | Detail |
|---|---|
| Target (MKP2) | Xeon Gold 6430 (SPR), 2×32c, RHEL 9.4, 256 GB DRAM, NUMA 0/1 |
| Initiator (MKP1) | Xeon Gold 6430 (SPR), 2×32c, RHEL 9.4 |
| SSDs on target | 2× Samsung PM9A3 1.92 TB (`nvme1n1`, `nvme2n1`), both PCIe Gen4×4 on NUMA 0 |
| Fabric | Intel E810 (`irdma`), **100 GbE RoCEv2** (not Falcon), MTU 4096, `200.0.0.35 ↔ 200.0.0.37`, both NICs on NUMA 0 |
| NVMe-oF | kernel `nvmet-rdma` (target), kernel `nvme_rdma` (initiator), **`--nr-io-queues=16` per controller — see manifest note below** |
| Namespace layout | One NQN per drive (`mkp2-nvme1`, `mkp2-nvme2`) — no RAID, no LVM |
| FIO | 3.35, libaio, `--direct=1`, `--time_based --runtime=15 --ramp_time=2`, single repetition |
| PMU | `perf stat -A` on `uncore_imc_*/cas_count_read/write` for 8 DDR channels on the target socket, **Config A only** |

**Queue-count manifest entry (R5 comparability).** Default `nvme connect`
creates 128 I/O queues per controller (one per initiator CPU). On MKP1
this failed with `Cannot allocate memory / Failed to write to
/dev/nvme-fabrics` on the second controller, because irdma MR/QP
resource pool couldn't back 2 × 128 queues + associated RQ/SQ buffers.
Workaround: `--nr-io-queues=16` per controller. This is a load-bearing
operational constraint, not a free tuning knob. Any comparison run
(local vs wire, kernel vs offload) must use the same queue count or
attribute the delta to it explicitly. Failed connect verbatim:
`nvme connect -t rdma -n mkp2-nvme2 -a 200.0.0.37 -s 4420` → ENOMEM.

## What ran

Two configs completed:

- **Config A (local block)** — FIO on MKP2 directly against `/dev/nvme1n1` and `/dev/nvme2n1`, no `nvmet`, no RDMA. Hardware ceiling on the target host.
- **Config C (wire baseline over RoCEv2)** — FIO on MKP1 against `/dev/nvme{2,3}n1` (remote namespaces from MKP2 attached via `nvme_rdma` over 100 GbE RoCEv2). Full NVMe-oF stack + fabric. **This is not a Falcon measurement.**

**Config B (NVMe-oF loopback on MKP2)** was attempted with `rdma_rxe` on `lo` and separately with `nvme-loop`; both failed at connect time (route resolution timeouts on rxe; nvme-loop module not loadable on this kernel). Skipped. Note: skipping Config B is compatible with the plan's aggregate D1/T7 baseline, which does not require a stack-vs-wire decomposition. It is **not** valid to attribute the A→C delta to any specific component (framing, target dispatch, queue count, wire latency) without further isolation.

Matrix (per config, 24 cells each): access pattern × block size × queue depth = 3 × 2 × 4 = 24. Drive count fixed at 2. **Cell runtime = 15 s, single repetition — does not meet plan T2b (60 s latency runs, 5-min bandwidth runs, 5 repetitions).**

## Results

### Aggregate throughput and tail latency

| pattern | bs | QD | local GB/s | local p99 µs | wire GB/s | wire p99 µs | wire/local |
|---|---:|---:|---:|---:|---:|---:|---:|
| seq_read | 64k | 1 | 2.43 | 25 | 1.09 | 59 | 45% |
| seq_read | 64k | 16 | 13.92 | 114 | 9.86 | 146 | 71% |
| seq_read | 64k | 64 | 14.15 | 322 | 11.95 | 453 | 84% |
| seq_read | 64k | 256 | 14.20 | 1532 | 11.95 | 1663 | 84% |
| seq_read | 256k | 1 | 3.62 | 68 | 2.11 | 121 | 58% |
| seq_read | 256k | 16 | 14.28 | 301 | 11.99 | 449 | 84% |
| seq_read | 256k | 64 | 14.29 | 1237 | 11.99 | 1647 | 84% |
| seq_read | 256k | 256 | 14.28 | 4751 | 11.99 | 7504 | 84% |
| rand_read | 64k | 1 | 2.52 | 25 | 1.08 | 116 | 43% |
| rand_read | 64k | 16 | 14.05 | 90 | 11.94 | 153 | 85% |
| rand_read | 64k | 64 | 14.10 | 375 | 11.94 | 461 | 85% |
| rand_read | 64k | 256 | 14.20 | 1516 | 11.95 | 1565 | 84% |
| rand_read | 256k | 1 | 4.14 | 61 | 2.28 | 165 | 55% |
| rand_read | 256k | 16 | 14.28 | 338 | 11.99 | 436 | 84% |
| rand_read | 256k | 64 | 14.28 | 1221 | 11.99 | 1597 | 84% |
| rand_read | 256k | 256 | 14.29 | 4751 | 11.99 | 7504 | 84% |
| seq_write | 64k | 1 | 2.43 | 27 | 0.83 | 77 | 34% |
| seq_write | 64k | 16 | 5.61 | 635 | 5.61 | 676 | 100% |
| seq_write | 64k | 64 | 5.61 | 1253 | 5.61 | 1106 | 100% |
| seq_write | 64k | 256 | 5.62 | 3621 | 5.62 | 3752 | 100% |
| seq_write | 256k | 1 | 3.37 | 80 | 1.83 | 157 | 54% |
| seq_write | 256k | 16 | 5.61 | 1253 | 5.61 | 1188 | 100% |
| seq_write | 256k | 64 | 5.62 | 3752 | 5.62 | 3916 | 100% |
| seq_write | 256k | 256 | 5.62 | 12255 | 5.62 | 12911 | 100% |

### DDIO health (target socket DRAM traffic during local reads)

For read workloads, DRAM-write traffic during the run indicates SSD→LLC DMA is spilling to DRAM instead of being consumed from LLC. Values are 15-second sums across 8 target-socket IMC channels; DDIO health is `DRAM_write_BW / SSD_read_BW`.

| workload | SSD BW GB/s | DRAM_R GB/s | DRAM_W GB/s | spill ratio | verdict |
|---|---:|---:|---:|---:|---|
| seq_read 64k QD 1 | 2.43 | 0.08 | 0.08 | 3% | LLC hit |
| seq_read 64k QD 16 | 13.92 | 0.07 | 0.07 | 0% | **LLC hit** |
| seq_read 64k QD 64 | 14.15 | 0.07 | 0.08 | 1% | LLC hit |
| seq_read 64k QD 256 | 14.20 | 0.07 | 1.38 | 10% | LLC hit (marginal) |
| seq_read 256k QD 1 | 3.62 | 0.07 | 0.07 | 2% | LLC hit |
| seq_read 256k QD 16 | 14.28 | 0.07 | 0.08 | 1% | **LLC hit** |
| seq_read 256k QD 64 | 14.29 | 0.07 | 1.91 | 13% | LLC hit (marginal) |
| seq_read 256k QD 256 | 14.28 | 0.08 | 6.52 | **46%** | **PARTIAL SPILL** |
| rand_read 64k QD 16 | 14.05 | 0.07 | 0.07 | 0% | LLC hit |
| rand_read 64k QD 256 | 14.20 | 0.07 | 1.54 | 11% | LLC hit (marginal) |
| rand_read 256k QD 16 | 14.28 | 0.07 | 0.07 | 0% | LLC hit |
| rand_read 256k QD 64 | 14.28 | 0.07 | 1.36 | 10% | LLC hit (marginal) |
| rand_read 256k QD 256 | 14.29 | 0.08 | 6.60 | **46%** | **PARTIAL SPILL** |

## Findings

1. **Local hardware ceiling is ~14.3 GB/s aggregate reads, ~5.6 GB/s aggregate writes.** Reads are Gen4-lane-limited (2×~7 GB/s), writes are PM9A3-media-limited (~2.8 GB/s sustained per drive). QD 16 is enough to saturate reads; writes plateau immediately.

2. **Wire baseline is ~12.0 GB/s reads = 96 Gbps of goodput at MTU 4096 on 100 GbE RoCEv2.** ~84% of local ceiling on reads across all block-size × QD points that saturate. **The 16% delta cannot be attributed to any specific component** (framing, target dispatch, initiator stack, queue-count workaround, wire latency) without Config B or a separate isolation. This is a clean 100 GbE read saturation number, nothing more.

3. **Writes are unaffected by the wire.** Local == wire == 5.6 GB/s. Writes are media-bound on 2 drives; adding the network doesn't cost anything because the SSDs are the bottleneck at ~45 Gbps.

4. **Low-QD tax is real.** At QD 1, wire delivers 34–58% of local. Round-trip latency (fabric + `nvmet-rdma` command dispatch + `nvme_rdma` completion) dominates when there's no pipelining. QD ≥ 16 hides it.

5. **DDIO PMU signal on the local-block Config A only.** At QD 16–64 on 64k or 256k, target-socket DRAM writes are <15% of SSD read BW during 14 GB/s SSD reads. Interpretation: inbound SSD DMA lands cleanly in target LLC on the local path. **This is not evidence that the wire path (Config C) is DDIO-hit-heavy** — Config C introduces target-side NIC DMA on top of SSD DMA, and PMU was not collected on the target during wire runs. Any "SSD → LLC → NIC without DRAM" claim requires target-side PMU under Config C.

6. **Local-path DDIO spill knee at (QD 256, 256k) on 2 drives.** 256 in-flight pages × 256 KB = 64 MB working set, DRAM writes climb to 46% of SSD read BW. On 4–8 drives the knee will shift to lower QD. Same caveat as (5): this is Config A only; the wire-path knee could be different.

7. **100 GbE is a fabric ceiling for 2-SSD reads on this rig.** ~12 GB/s wire vs 14.3 GB/s local. To saturate the fabric we need more than 2 drives; conversely, adding drives past 2 doesn't help until we upgrade the fabric or the transport.

## For §4.2 planning

- **Read side, RoCEv2 100 GbE goodput ceiling at MTU 4096: ~12.0 GB/s.** Any Falcon-side number lands against a *different* transport; do not use 12.0 GB/s as the Falcon reference. When Falcon becomes available, rerun and produce a Falcon-labeled baseline separately.
- **Write side is media-bound at ~5.6 GB/s** on 2 drives. Cannot be improved by network changes on this rig.
- **DDIO way-count tuning (`iio_llc_ways`) is a candidate knob for §4.2** — the local-path knee at (QD 256, 256k) gives a rough concurrency bound, but the wire-path knee needs its own measurement before it can be cited as a concurrency ceiling.
- **A→C delta interpretation.** The 16% delta at QD ≥ 16 on reads is an aggregate number that includes wire framing, target `nvmet-rdma` dispatch, initiator `nvme_rdma` cost, and the `--nr-io-queues=16` operational cap. **It is not a "stack tax" datum**; that label was misapplied in earlier drafts (R1 in the plan means initiator-only ownership, not a stack-tax measurement). A separated stack-vs-wire number requires a working Config B — e.g., `nvmet-tcp` loopback or a second local NIC binding.

## Namespace layout — how LMCache should consume these drives

Target-side layout is settled: each SSD is its own NQN + namespace, no
RAID or LVM on the target. Initiator-side, LMCache has three options;
see `nvmeof-poc-plan.md` §4.4 for the plan-of-record framing.

**Important scope caveat this document does not validate.** The DDIO
numbers above measure two direct block devices with no filesystem, no
RAID 0, and no LMCache. They **do not** prove that RAID 0 + XFS/ext4 +
`LocalDiskBackend` preserves the observed DDIO behavior — the `mdadm`
chunk size can split any I/O larger than the stripe unit into two
device-level operations, moving the DDIO knee. Validating option 1 as
DDIO-friendly requires a separate run with the actual stack in place.

Summary of the three options:

1. **D1 throughput baseline: kernel RAID 0 on the initiator across the
   two remote namespaces**, one filesystem on `md0`,
   `LMCACHE_LOCAL_DISK=/mnt/md0/kvcache`. Uses `LocalDiskBackend`
   unchanged. Upstream docs endorse this for throughput. Two hard
   caveats: (a) `LocalDiskBackend` gives filesystem-durable file writes
   plus an in-memory metadata map; it **does not** deliver the Appendix
   A durable key→LBA map with WAL replay. This option is a D1
   throughput vehicle, not a D2 durability path. (b) One namespace
   loss destroys the entire md0 volume — every acknowledged cache
   entry becomes unavailable. Acceptable only if the D1/D2 scope
   explicitly limits "ACK means recoverable" to process/fabric failure
   with intact media, or if a cache-loss policy is defined.
2. **Alternative: `by_gpu` sharding across two mounts.** With ≥ 2 GPU
   workers on the initiator, `PathSharder` assigns one drive per
   worker deterministically. On MKP1 (no GPU) it does **not**
   degenerate to option 1 — it selects `paths[0]` and leaves the
   second namespace idle, halving usable capacity and bandwidth.
   Useful only when N_workers ≥ N_drives.
3. **Alternative: new `by_key` strategy** — per-page placement by
   chunk-hash modulo drive count. See bead LMCache-05n for scope. Not
   a trivial change: hash must be stable across processes and
   restarts (use `hashlib` or BLAKE3, **not** Python `hash()` which is
   `PYTHONHASHSEED`-salted); every put and get must consult the
   sharder per-op, not just at init; capacity and eviction accounting
   must remain coherent when files land on different paths; and the
   metadata map has to key on `(namespace_id, path_id)` or similar.
   Evaluated alternative only.

For the D1 throughput number on 2 drives, option 1 is the practical
default. It is **not** a D2 durable-cache path without additional
work (a real durable key→LBA map and WAL replay per Appendix A). The
D2 configuration is a separate design decision that this run does not
inform.

## Artifacts

- Per-cell FIO JSON: `mkp2:/root/mkp2-baseline/local/*.json`, `mkp1:/root/mkp1-wire/wire/*.json`
- PMU counters: `mkp2:/root/mkp2-baseline/pmu/local_*.txt`
- Run logs: `mkp2:/root/mkp2-baseline/run.log`, `mkp1:/root/mkp1-wire/wire2.log`

## Known gaps / follow-ups

- **Config B (loopback)** — retry with `nvmet-tcp` loopback or a second physical NIC. `rdma_rxe` on `lo` fails route resolution on this kernel.
- **CHA TOR opcode split** (`IO_ITOMCACHENEAR` vs `IO_ITOM`) — captured only DRAM CAS counts; the CHA-side event pack for SPR wasn't invoked. Add on rerun to attribute spill more precisely per PCIe stack.
- **CPU per GB** — FIO reports usr/sys CPU per cell; not compiled into this report yet. Add if needed for R5 traceability.
- **`iio_llc_ways` sweep** — the knee shifts if we widen the DDIO way budget. Do 2, 4, 6 ways to see how much the QD 256 spill can be recovered.
- **Drive-count scaling** — cannot be done on this rig (2 drives fixed). Repeat when 4+ drive rig is available.
