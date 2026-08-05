# `bench l2` + `fs_native` remote-L2 characterization — mkp1 (initiator) / mkp2 (target)

**Date:** 2026-08-03
**Scope:** LMCache `lmcache bench l2` driving the `fs_native` L2 adapter against
remote NVMe-oF (md0 RAID0 + XFS) over the 100 GbE Falcon reliable-transport
link between the two MEV IPUs. Store and load paths, concurrency and
payload-size sweeps, host-CPU cost on the read path.

**What this is:** the first counter-validated LMCache-path numbers on this rig,
and the D2 CPU-side companion to the fio-only D1 ceiling in
[mkp1-mkp2-d1-fs-ceiling-2026-08-02.md](mkp1-mkp2-d1-fs-ceiling-2026-08-02.md).

**What this is NOT:** not D2 sign-off. No WAL, no FUA/FLUSH ordering, no payload
digest, no atomic publication — `fs_native` is a plain filesystem adapter. No
GPU in the path (CPU-resident L1 buffers only). No mixed R/W: the harness is
strictly two-phase.

**Classification (2026-08-04).** These are **Falcon-backed kernel NVMe-oF**
results. Earlier revisions labelled the fabric "RoCEv2" and one companion
document said "not Falcon"; that was a misread of the host software stack.
[`falcon-bringup-provenance.md`](falcon-bringup-provenance.md) §1 shows the two
IPU QSFP port 0s directly cabled at 100 GbE with the Falcon app (`rtcmd`)
running on both ACCs, carrying `rocep69s0f0` / `ens2f0` / `200.0.0.35`↔`.37` —
the same device and addresses used here. `idpf` + `irdma` is the host-facing
stack above that path. This is a transport *classification*, not a packet-level
claim: the evidence does not capture on-wire headers, so "RoCE-style verbs over
a Falcon-backed link" is the supportable wording.
Reclassifying the transport does **not** relax any measurement caveat: this
is still rounds mode (not sustained), CPU-buffers-only with no GPU, and a
full filesystem + NVMe-oF + media path rather than a bare link test.

> **Throughput retraction (2026-08-05).** Do not cite the GB/s, Gbps, or
> percent-of-fio figures in this document. The historical
> `throughput_avg_mbps` field was computed in MiB/s (`1024 * 1024`) but was
> interpreted here as decimal MB/s. Its raw JSON retains only aggregate
> round statistics, not the individual round durations required to recompute a
> valid aggregate goodput after the correction. This affects the 95.1 Gbps
> read ladder, the 89.5 Gbps longest-run value, and the write figures. The
> later sustained-read results use successful bytes divided by the measured
> window with the correct binary-to-decimal conversion; use
> `mkp1-fsnative-sustained-2026-08-04.md` and
> `mkp1-deepseek-proxy-2026-08-04.md` for reportable read measurements.

## Headline

| Path | Achieved | Matched comparator | % of comparator |
|---|---:|---:|---:|
| **Read (load)** | **11.88 GB/s** (95.1 Gbps) | 11.98 GB/s fio, XFS+md0 saturated | **99.2 %** |
| **Write (store)** | **5.67 GB/s** (45.4 Gbps) | ~5.6 GB/s fio, direct-block 2-drive | ~101 % |

**Comparator provenance — read the caveats, they are not symmetric.**

- **Read comparator 11.98 GB/s** is the matched surface: XFS+md0 saturated-QD
  cells from the D1 FS-ceiling run (`rand QD64` 11.98, `rand QD256` 11.98,
  `sanity agg128` 11.98). The XFS cell range is 11.65–11.98; 11.65 is the
  low-QD `rand QD16` outlier. **11.99 GB/s is the RAW no-filesystem number** and
  is the wire ceiling, not the right comparator for a filesystem path.
- **Write comparator ~5.6 GB/s** comes from
  [mkp1-mkp2-baseline-2026-08-02.md](mkp1-mkp2-baseline-2026-08-02.md) finding 1.
  It is **direct-block, 15 s, single-repetition** evidence — *not* an XFS+md0
  write ceiling, and the D1 FS-ceiling run explicitly says its own write ceiling
  must not be inferred (that run was reads-only). Treat "matches the ceiling" as
  *consistent with the prior two-drive raw-block plateau*, pending a matched
  XFS write control.

**The consequence for the PoC:** on 100 GbE this host path reaches the wire on
reads and the prior write plateau on stores. **There is no remaining
*throughput* headroom on this fabric in which an offload could show a bandwidth
win.** That is a narrower claim than "no benefit": this run says nothing about
host-CPU reduction, tail latency, mixed R/W, multi-initiator scaling, or a
different target implementation — all of which remain open and are the stated
success criteria elsewhere in the plan. A bandwidth-visible offload result needs
a faster fabric or more drives.

## Setup

| Item | Value |
|---|---|
| Initiator | mkp1, MEV IPU `rocep69s0f0` (`8086:1452`, `vendor_part_id` 5202), `idpf` + `irdma` host stack |
| Target | mkp2, `nvmet-rdma`, 2× Samsung PM9A3 Gen4 |
| Fabric | 100 GbE **Falcon reliable transport**, IPU-to-IPU direct attach, RoCE-style verbs on top; 96 Gbps goodput ceiling |
| Remote surface | md0 RAID0 + XFS at `/mnt/lmcache-stage2/kvcache` |
| Adapter | `fs_native`, `use_odirect: true`, `max_capacity_gb: 900` |
| Harness | `lmcache bench l2`, `--l1-align-bytes 4096`, `--warmup-rounds 0` |
| NVMe attach | `--nr-io-queues=16` per controller (ENOMEM at default 128) |
| MTU on `ens2f0` | **9000** — see caveat below |

Per cell: `rm -rf` the corpus, `sync`, `echo 3 > /proc/sys/vm/drop_caches`,
bracket the run with RDMA `hw_counters` reads, settle 2 s before the post-read.
Read cells additionally prepopulate at identical key geometry, then drop caches,
and bracket with `/proc/stat` CPU jiffies.

Scripts: `fsnative_probe.sh` / `fsnative_report.py` (store),
`read_probe.sh` / `read_report.py` (read). Raw JSON:
`mkp1:/root/mkp1-fsnative/*.json` (28 cells).

**MTU caveat — resolved 2026-08-04.** `ens2f0` was at *link* MTU **9000**
during these runs; `LMCache-awi` and the D1 manifest refer to the *QP path*
MTU, `active_mtu=IBV_MTU_4096`. These are different values, not a conflict:
`ibv_devinfo` on `rocep69s0f0` reports `max_mtu: 4096` / `active_mtu: 4096`
([`falcon-bringup-provenance.md`](falcon-bringup-provenance.md) §2), and plan
Appendix C
([`nvmeof-poc-plan.md:1029-1033`](../nvmeof-poc-plan.md)) states the link MTU
only needs ≥ 4200 of headroom. So no host drifted and the requirement is not
stale. The counter model's ~52428 B write-segment cap was derived at this
configuration and remains valid for it; **re-derive it if `active_mtu` itself
ever changes.** The
throughput numbers themselves are unaffected (they come from the harness, and the
counter check agreed within 1 % at the MTU actually in use).

## Methodology — three things that will burn you

**1. RDMA counter direction is inverted from intuition, and differs per direction.**

| NVMe-oF op | What the target does | Instrument | Ops per I/O |
|---|---|---|---|
| **WRITE** | RDMA-**reads** from initiator memory | `InRdmaReads` | `bytes / 4096` — exactly 4096 B/op, block-size invariant |
| **READ** | RDMA-**writes** into initiator memory | `InRdmaWrites` | `ceil(bytes / 52428)` — RDMA write segment cap |

`OutRdmaWrites` stays ~0 on the initiator — it never RDMA-writes. Payloads
≤ 4096 B ride **in-capsule** (`OutRdmaSends` only) and register zero RDMA
read/write ops, so this model holds only for > 4 KiB I/O. Segment cap verified at
bs 4k/64k/128k/256k/1M → 1/2/3/5/20 ops.

**Cross-host confirmation (2026-08-03 11:04 UTC, both hosts idle).** Reading the
same counters from the *target* side closes the loop independently of the fio
derivation:

| Counter | mkp1 (initiator) | mkp2 (target) |
|---|---:|---:|
| `InRdmaWrites` | 2,600,854,612 | 5,000 |
| `OutRdmaWrites` | 5,000 | **2,600,854,612** |
| `InRdmaReads` | 595,271,879 | 16,000 |
| `OutRdmaReads` | 16,000 | **595,271,879** |

mkp1's `In*` equals mkp2's `Out*` **exactly**, in both directions. The target is
the one issuing the RDMA writes (for NVMe-oF reads) and RDMA reads (for NVMe-oF
writes), exactly as the model states. A 10 s idle window showed delta 0 on both
hosts, so no background traffic contaminates the cells above.

**2. `irdma` refreshes `hw_counters` asynchronously (~1 s).** A delta read issued
immediately after a workload returns a **stale snapshot showing delta = 0**,
which is indistinguishable from "no data moved on the wire." This produced a
false negative during bring-up that took a raw pre/post dump to catch — the
missing +524298 appeared in the *next* iteration's pre-read. Characterized: 0 s
settle fails, ≥ 1 s is exact. All cells here use 2 s.

**3. `ethtool -S` is blind to RDMA payload on irdma.** A 34 GB RDMA read moved
`port_rx_bytes` by ~3.8 KB. The port counters see control traffic only. A
"three-way counter agreement" gate that includes NIC bytes is therefore
unsatisfiable **as measured on this rig** (`irdma` over Falcon, these directions) — use
a two-way gate (RDMA `hw_counters` vs application bytes). Scoped observation, not
a claim about all drivers; mlx5 on the bmg rig does account payload in
`rx_bytes_phy`/`tx_bytes_phy`, which is why the dashboard queries differ per
platform.

**Throughput metric.** `payload / (rounds × duration_avg_ms)` — total payload
over total *measured* time. This excludes ~13 s of interpreter/torch/adapter
startup, which would otherwise dominate (a 55 ms measurement window behind 13 s
of startup reads as 0.03 GB/s instead of 4.80).

**Both this metric and the harness's own `throughput_avg_mbps` are computed from
the same `round_durations` array, so neither excludes the drain barrier.** Each
round duration spans the submit loop *and* the wait for every task in that round
(`runner.py:157`–`168`, `:303`–`:306`), so the barrier is inside every sample.
The only difference is the averaging order: ours is total-payload / total-time
(harmonic-like), the harness's is the arithmetic mean of per-round rates
(`result.py:110`, `:124`). Arithmetic-mean-of-rates over-weights fast rounds, so
where the two diverge the harness figure reads higher. Neither is wall-clock:
inter-round Python work outside the timed region is in neither.

They agree within 1 % on every cell except `rd_ca4_inf16` (9.32 vs 10.55 GB/s,
13 %). A single slow round is the arithmetic that produces that gap, and the cell
does have the sweep's only heavy tail (p99/p50 = 2.6×) — but **per-round
durations were not retained, so this remains an inference, not a demonstrated
straggler.** Flagged rather than resolved.

## The concurrency ceiling is a min over three knobs

Each submit fans its batch into `min(num_workers, num_keys)` tiles
(`connector_base.h:313`). All `in_flight` submits enqueue onto **one shared FIFO**
before the runner waits (`connector_base.h:432`), so tiles from concurrent
submits coexist and the steady-state closed form is:

```
active tile workers = min(num_workers, in_flight * min(num_workers, num_keys))
```

`fs_native` has no per-operation lane split — Store and Load share the same
worker pool.

**Read this as "active tile workers", not "active NVMe I/O".** A worker processes
every key in its tile *serially*, so with `num_keys > num_workers` the in-flight
device queue is shallower than the worker count suggests. The columns below are
worker-level concurrency.

This is **load-bearing for reading the tables** and it invalidated one of our own
sweeps. Setting `--in-flight 256` against the `fs_native` default `num_workers=4`
does not give 256-deep I/O; it queues 252 requests behind four active workers.

## Results — store path

**Sweep A — `num_workers` 1→64, `in_flight=4`, `num_keys=8`, 1 MiB/key, 128 rounds, 4 GiB/cell.**
Active tile workers genuinely vary here, so this sweep is valid.

| cell | `num_workers` | active tile workers | GB/s | Gbps | p50 ms | p99 ms | keys |
|---|---:|---:|---:|---:|---:|---:|---|
| A_w1 | 1 | 1 | 3.76 | 30.1 | 7.97 | 16.82 | 4096/4096 |
| A_w2 | 2 | **2** | **5.48** | 43.8 | 5.94 | 9.68 | 4096/4096 |
| A_w4 | 4 | 4 | 5.56 | 44.5 | 5.92 | 6.73 | 4096/4096 |
| A_w8 | 8 | 8 | **5.67** | 45.4 | 5.89 | 7.98 | 4096/4096 |
| A_w16 | 16 | 16 | 5.63 | 45.0 | 5.89 | 6.76 | 4096/4096 |
| A_w32 | 32 | 32 | 5.63 | 45.0 | 5.91 | 6.76 | 4096/4096 |
| A_w64 | 64 | 32 | 5.59 | 44.7 | 5.93 | 9.09 | 4096/4096 |

**Throughput flattens by concurrency 2.** One worker reaches 3.76 GB/s; two
reach 5.48, within 3 % of the best cell in the sweep. Going 2→32 (16×) buys
**+2.7 %**, and the sweep maximum is 5.67 GB/s at 8 workers (+3.5 % over 2).
p99 does not degrade with concurrency, which is consistent with a saturated
medium rather than a contended software path.

**Stated as an observation, not a proven knee:** single repetition per cell, no
error bars, and the 5.48–5.67 GB/s spread is only ~3.5 %, which is within
plausible run-to-run variance for this rig. The defensible claim is "flat from
2 workers upward at ~5.5–5.7 GB/s", not a sharp knee at exactly 2.

**Sweep B — `in_flight` 1→32, `num_workers=16`, `num_keys=8`. DEGENERATE, do not
cite as a queue-depth result.**

| cell | `in_flight` | active tile workers | GB/s | p50 ms | p99 ms |
|---|---:|---:|---:|---:|---:|
| B_inf1 | 1 | 8 | 5.41 | 1.55 | 2.12 |
| B_inf2 | 2 | **16** | 5.65 | 2.96 | 3.15 |
| B_inf4 | 4 | **16** | 5.64 | 5.89 | 7.89 |
| B_inf8 | 8 | **16** | 5.64 | 11.75 | 14.83 |
| B_inf16 | 16 | **16** | 5.64 | 23.67 | 26.23 |
| B_inf32 | 32 | **16** | 5.55 | 48.06 | 51.08 |

`min(16, inf × 8)` pins active tile workers at 16 from `inf=2` upward. The flat throughput
is therefore **largely an artifact of the cap**, not evidence that queue depth
does not matter. What *is* real: p50 scales almost perfectly linearly with
`in_flight` (1.55 → 48.06 ms, 31× over a 32× range) at constant throughput. That
is pure queueing delay for depth that never reached the disks. Re-run tracked in
`LMCache-m5o.2`.

## Results — read path

**Payload-size sweep — `num_workers=16`, `in_flight=8`, `num_keys=64`. Active I/O
is constant at 16 across all four cells, so only payload varies.**

| cell | KiB/key | GB/s | Gbps | % of 11.98 | p50 ms | p99 ms |
|---|---:|---:|---:|---:|---:|---:|
| rd_kb256 | 256 | 8.01 | 64.1 | 66.9 % | 16.51 | 23.38 |
| rd_kb512 | 512 | 10.61 | 84.9 | 88.6 % | 24.50 | 34.20 |
| rd_kb1024 | 1024 | 10.91 | 87.3 | 91.1 % | 46.61 | 91.54 |
| rd_kb4096 | 4096 | **11.88** | **95.1** | **99.2 %** | 180.50 | 183.78 |

**Payload size is the throughput knob on the read path**, at fixed concurrency.
Going 256 KiB → 4 MiB per key buys +48 % throughput. At 4 MiB the path is within
0.8 % of the matched XFS comparator.

**Longest rounds-mode run** — `rd_long_r200`, 200 rounds, 102,400 keys,
**100 GiB moved** (*not* sustained mode; see the barrier caveat below):
11.18 GB/s / 89.5 Gbps, 102400/102400 keys succeeded. Longest window in the set;
use it as the conservative figure. Not a true steady-state measurement — the wave
barrier idles the pool at each of the 200 round edges (finding 6).

### Host-CPU cost — WITHDRAWN, do not cite

Earlier drafts carried a `cores/100Gbps` column (205 / 114 / 71 / 47 across the
payload sweep, 47.8 sustained). **Those numbers are withdrawn.** The `/proc/stat`
jiffie bracket spans the whole process — including ~8–12 s of interpreter, torch
import, and adapter construction — while the actual measured I/O window is only a
fraction of it:

| cell | measured I/O window | bracketed wall | I/O as % of bracket |
|---|---:|---:|---:|
| rd_kb256 | 0.34 s | 8.12 s | 4 % |
| rd_kb512 | 0.51 s | 8.40 s | 6 % |
| rd_kb1024 | 0.98 s | 9.00 s | 11 % |
| rd_kb4096 | 3.61 s | 12.89 s | 28 % |
| rd_long_r200 | 9.60 s | 20.81 s | 46 % |

So 54–96 % of the attributed CPU is startup, and the apparent 4.4× efficiency
"improvement" with payload size is substantially just the measured window growing
relative to fixed startup cost. The dimensional form is
`(jiffies / CLK_TCK / elapsed_s) / (Gbps / 100)`; the defect is the *window*, not
the algebra.

A host-CPU-per-GB number is a **stated success criterion** for the offload
decision, so this needs redoing properly: sample only the measurement interval
(or run long enough that startup is <5 %), and attribute per-thread rather than
whole-host. Tracked in `LMCache-m5o.3`.

**LMCache-ca4 geometry** — 28 MiB/key (DeepSeek-V3 KV chunk), `num_keys=1`,
`num_workers=16`:

| cell | `in_flight` | active tile workers | GB/s | Gbps | p50 ms | p99 ms |
|---|---:|---:|---:|---:|---:|---:|
| rd_ca4_inf4 | 4 | 4 | 10.57 | 84.6 | 10.84 | 11.87 |
| rd_ca4_inf16 | 16 | 16 | 9.32 | 74.5 | 39.88 | 102.56 |
| rd_ca4_inf64 | 64 | 16 | **11.88** | 95.1 | 158.05 | 158.80 |

Non-monotonic, and the dip is explained: at `num_keys=1` each submit is one tile,
so `inf=4` caps active I/O at 4. `rd_ca4_inf16` is the one cell where wall-clock
(9.32) and harness (10.55) throughput disagree by >1 % — its p99/p50 ratio is
2.6× (102.56 / 39.88), the only cell with a heavy tail, indicating a straggler
round that the harness's per-round mean discounts. The `inf=64` cell reaches the
same 11.88 GB/s as the 4 MiB payload cell.

## Verification

`--skip-verify` defaults **True**, and the verify gate structurally requires
**both** store and load batches, so `--only load` can never self-verify. A
combined store+load cell was therefore armed separately:

```
gate: w=16 inf=4 nk=32 kb=1024 rounds=4
  Store 512/512 keys, 5.45 GB/s
  Lookup 512 keys
  Load  512/512 keys, 10.16 GB/s
  [Verify] All 16 keys data verified OK.
```

Store fills `i & 0xFF`, load fills `(i+1) & 0xFF`, so a silent no-op load is
caught. Coverage is the last measured round only. Every other cell relies on
counter validation: observed RDMA ops within **1 %** of the payload-derived
expectation on all 28 cells (`r=1.000`–`1.008`), plus `total_success ==
total_keys` on every cell.

## Findings

1. **Read path reaches 99.2 % of the matched XFS+md0 fio comparator.** 11.88 GB/s
   / 95.1 Gbps at ≥ 4 MiB per key, reproduced from two independent geometries
   (10240×4 MiB and 384×28 MiB); 89.5 Gbps over the 100 GiB run. The comparison
   is against fio's saturated-QD XFS cells, not a workload-matched control (fio
   ran 256 KiB QD-swept, the bench ran 4 MiB at 16 workers), so read this as
   "the LMCache read path gets essentially all of the available bandwidth", not
   as a like-for-like efficiency measurement.
2. **Write path plateaus at ~5.5–5.7 GB/s / 44–45 Gbps** from 2 workers upward.
   Consistent with the prior two-drive direct-block plateau, which is the best
   available comparator but is not a matched XFS write control (see Headline).
   The flatness across a 32× concurrency range and the non-degrading p99 both
   point at the medium rather than software, but a matched control is needed
   before "media-bound" is stated as established.
3. **Payload size, not queue depth, is the read-path knob.** 64.1 → 95.1 Gbps
   across 256 KiB → 4 MiB at constant 16 active tile workers.
4. **100 GbE is no longer the interesting variable for bandwidth.** Both
   directions sit at their respective comparators. No *throughput* headroom
   remains on this fabric for an offload to demonstrate a bandwidth win — but
   CPU reduction, tail latency, and mixed R/W are untested here and are the
   criteria that actually decide the offload question.
5. **The harness reports round-level percentiles, not per-task latency.**
   `duration_p50_ms` / `duration_p99_ms` are percentiles over *round durations*
   (`result.py:98-103`) and are genuinely useful — the `rd_ca4_inf16` straggler
   above was found with them. But `latency_per_key_ms` is `avg_duration /
   keys_per_round` (`result.py:165`), an arithmetic artifact of the wave barrier,
   not a latency. **There is no per-request latency distribution.** With
   `in_flight` submits per round, a round percentile cannot separate a uniformly
   slow round from one straggler.
6. **The wave barrier idles the worker pool at each round edge.** Each round
   issues all `in_flight` submits, then does one eventfd wait for the whole group
   before refilling (`runner.py:157`, `:168`, `:303`). This is a per-**round**
   drain barrier, **not** per-request serialization — submits are non-blocking
   all the way down. Costs wall-clock as a sawtooth; does not serialize I/O.

## Implications for the plan

- **D2 CPU-side read path needs no optimization on 100 GbE.** Cite 11.88 GB/s /
  95.1 Gbps at ≥ 4 MiB, or 89.5 Gbps over the 100 GiB run as the conservative
  number. Do **not** cite a host-CPU figure from this run (withdrawn, see above).
- **Any KV chunk geometry below ~1 MiB gives up real bandwidth** — 256 KiB chunks
  cost 33 % of the wire and 4.4× the CPU per Gbps. The DeepSeek-V3 28 MiB chunk
  is comfortably in the efficient regime.
- **`num_workers` and `in_flight` must be swept jointly**, with effective active
  I/O recorded per cell. `LMCache-ca4` amended accordingly.
- **A bandwidth-visible offload-benefit result is blocked on faster fabric or
  more drives**, not on software work. 400 GbE lifts the read ceiling; more
  PM9A3s lift the write plateau. A *CPU-reduction* offload result is not
  blocked — it is simply unmeasured, and needs the CPU methodology fix first.
- **Must NOT be inferred from this run:** GPU staging cost (no GPU in path),
  mixed R/W behavior (strictly two-phase), durability or crash semantics
  (`fs_native` has none), per-request tail latency (finding 5), host-CPU-per-GB
  (withdrawn), a matched XFS write ceiling (not measured), multi-initiator
  behavior.

## Related

- `LMCache-m5o.1` — harness measurement fixes (per-task latency, `--duration-sec`)
- `LMCache-m5o.2` — joint `num_workers` × `in_flight` re-run
- `LMCache-m5o.3` — redo host-CPU attribution (measurement-window-scoped)
- `LMCache-ca4` — amended: concurrency formula, two-way counter gate, verify preflight
- `LMCache-mg1` — retraction addendum for `mkp1-stage2-smoke-2026-08-02.md`
