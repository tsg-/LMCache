# fs_native sustained-window read result — mkp1, 2026-08-04

**Result: 95.91 Gbps sustained for 120 s, reproduced twice, counter-validated
and gate-accepted.** LMCache `fs_native` drives the existing 100 GbE
Falcon-backed NVMe-oF read path to parity with the matched fio comparator
(100.1%), in a true steady-state window with no wave barrier. The read path
sits at the fabric ceiling, with no adapter overhead resolvable at this speed.

> **Correction, 2026-08-04.** An earlier revision of this document reported
> 91.47 Gbps. That was a unit error in the reporting script, not in the
> measurement: `result.py` defines `_MB = 1024*1024`, so
> `throughput_aggregate_mbps` is **MiB/s**, and `sustained_report.py` converted
> it as decimal MB/s — understating every figure by 4.86%. The RDMA counter
> ratios were never affected (they compare bytes to counter ops and never read
> the throughput field), so the acceptance decisions stand. Corrected against
> three independent instruments that agree to 0.1%: app bytes ÷ window, the
> JSON field under the correct scale, and the Prometheus counter slope. The
> reporter now prints an independent app-bytes-derived figure alongside the
> bench's own, so a future units change surfaces as a visible divergence.

**Classification — read this before citing any number here.** This is a
**Falcon-backed kernel NVMe-oF** measurement. The IPU acts solely as the
`irdma` verbs device beneath the kernel `nvme_rdma`/`nvmet_rdma` path. It is
**not** a Falcon-offload result and **not** a 400 GbE result. Nothing here
extrapolates to the 400/4×400 GbE MMG platform, which remains unavailable.

## Result

| Run | Window | Submits | Keys succ/total | App bytes | Throughput | Counter ratio |
|---|---:|---:|---:|---:|---:|---:|
| `sus31785852646` | 120.15 s (0.13 s of it drain) | 5366 | 343424 / 343424 | 1341.50 GiB | **95.91 Gbps** | 1.0001 |
| `sus41785852800` | 120.12 s (0.12 s of it drain) | 5365 | 343360 / 343360 | 1341.25 GiB | **95.91 Gbps** | 1.0002 |

Ratios recomputed after the drain-tail fix (was 1.0013 / 1.0012). Throughput is
corroborated two ways that agree exactly: the bench's own field and an
app-bytes-÷-window figure derived independently of it.

Mean submit latency 179.01 / 179.03 ms; p99 within the same 1%. Both runs:
every key succeeded, no timeout, and all six error counters
(`RetransSegs`, `Nak Sequence Error`, `RTO`, `RNR received`,
`Rcvd Out of order packets`, `InProtoErrors`) delta **zero**.

Reproducibility is 0.0% apparent spread on throughput between independent runs
— but note both ran back-to-back on one host state, so this bounds run-to-run
jitter, not cross-boot or cross-rig variance.

### Where this sits against the known ceilings

| Reference | Value | This result vs it |
|---|---:|---:|
| Matched fio comparator, XFS on md0 ([rounds-mode](mkp1-fsnative-l2-2026-08-03.md) §Findings) | 11.98 GB/s = 95.84 Gbps | **100.1%** |
| fio raw-block wire ceiling, no filesystem ([baseline](mkp1-mkp2-baseline-2026-08-02.md) §Results) | 11.99 GB/s = 95.92 Gbps | 100.0% |
| Best rounds-mode `fs_native` (wave-barriered, not steady state) | 11.88 GB/s = 95.04 Gbps | 100.9% |
| Local 2-SSD block ceiling (not fabric-limited) | 14.3 GB/s = 114 Gbps | 84.1% |

The matched comparator is the meaningful one: it runs fio through the same XFS
on md0 that `fs_native` uses, so it isolates the adapter rather than also
charging it for the filesystem. The fio figures are computed from
`bw_bytes / 1e9` — true decimal GB/s with no MiB ambiguity — so they are
directly comparable to the corrected numbers above.

**At parity with the matched comparator, the read path sits at the fabric
ceiling, with no adapter overhead resolvable at this speed.** `fs_native` adds
no *measurable* overhead over fio on the same filesystem, which is the
substantive finding: there is no adapter-side headroom this measurement can
recover, and the remaining ~16% to the local 2-SSD block ceiling is not
attributed here to any single component. Reading 100.1% and 100.9% as "faster than
the comparator" would over-read the data — those sit inside the run-to-run
spread of the comparator cells (11.65–11.98 GB/s), so the honest statement is
parity.

## Configuration

| Item | Value |
|---|---|
| Initiator | mkp1, Xeon Gold 6430, RHEL 9.4, kernel `5.14.0-427.13.1.el9_4`, 251 GiB DRAM |
| Target | mkp2, 2× Samsung PM9A3 1.92 TB, one NQN per drive |
| Fabric | 100 GbE Falcon-backed direct IPU↔IPU link, `rocep69s0f0`, `fw_ver 1.145`, `active_mtu` 4096, `200.0.0.35 ↔ .37` on `ens2f0` |
| NVMe-oF | kernel `nvme_rdma` / `nvmet_rdma`, **`--nr-io-queues=16`** per controller (verified: `queue_count` 17 = 16 I/O + 1 admin) |
| Initiator storage | RAID 0 `md0` across the two remote namespaces, 256k chunk, XFS (`sunit=512,swidth=1024`), `noatime,nodiratime` |
| Adapter | `fs_native`, `num_workers=16`, `use_odirect=true`, `max_capacity_gb=900` |
| Geometry | `--num-keys 64 --data-size-kb 4096 --in-flight 8 --warmup-rounds 0 --rounds 200` |
| Window | `--duration-sec 120 --warmup-sec 10` |
| Working set | 102,400 keys × 4 MiB = **400 GiB**, which exceeds 251 GiB DRAM, so the window cannot be served from page cache |
| Commit | `66cba0a5` on `feat/bench-l2-sustained-only` |

The load phase is **read-only**: it reuses the corpus a prior `--only store`
pass wrote under `--key-prefix sus1785851`, so repeat runs add no write wear.
Caches are dropped before each run.

Reproduce with:

```bash
# on mkp1; artifacts land in /root/mkp1-sustained
RUN_ID=sus$(date +%s) bash scripts/ipu-poc/run_sustained_load.sh
```

The driver, the interior-rate estimator, and the acceptance gate are committed
under `scripts/ipu-poc/` as `run_sustained_load.sh`, `interior_rate.py`, and
`sustained_report.py` (commit `fbae0676`), so this measurement is reproducible
from source control rather than from a copy in `/root`.

**One caveat on exact reproduction.** The runs on this page were executed with
the pre-fix helpers; the committed versions carry the units, drain-tail, and
`NA`-rejection fixes described below. The recorded artifacts were re-scored with
the committed versions and the acceptance outcomes are unchanged, but a fresh run
is not bit-identical to the original transcript.

## Why two earlier attempts were rejected

Both rejections were harness defects, not fabric faults. Recording them because
each would silently corrupt a future measurement.

**1. Keyspace overrun — 1536 keys missed (`sus1785851`).**
`total_submit_slots = (warmup_rounds + rounds) × in_flight`, and
`--warmup-rounds` **defaults to 1**. The prepopulating store ran
`--warmup-rounds 0` → 1600 slots (key idx 0–102399); the load omitted the flag
→ 1608 slots, leaving 8 slots (idx 102400–102911 = 512 keys) never stored. 24
of 5391 submits landed there: 24 × 64 = 1536 misses, matching the observed
count exactly, with the first failing key at idx `0x19000` = 102400.
**Fix:** pass `--warmup-rounds 0` on the load so its wrap range is exactly the
range the store covered. Any sustained load must match its prepopulator on
*both* `--rounds` and `--warmup-rounds`, not just `--rounds`.

**2. Counter-model mis-bracketing.** Three estimators were tried; only the
third is sound.

| Estimator | Ratio | Verdict |
|---|---:|---|
| Whole-process delta | 1.0783 | **Wrong.** Includes the discarded warmup's wire traffic while app bytes count the measured window only. The 7.83% excess is 105 GiB, and the 10 s discarded warmup carries ~112 GiB at the measured rate — the right magnitude for warmup traffic, and nothing else in the run accounts for it. |
| Two-point edge bracket at `t0+2s` / `t1+2s` | 0.9475 | **Wrong.** Assumes the async `hw_counters` refresh lag is symmetric and cancels in the difference. It does not — a ~1.06 s undercount. |
| **Interior least-squares slope** over `[t0+3s, t1-3s]` | **1.0001** | **Used.** Reads neither edge, so both the refresh lag and warmup traffic drop out. Stable to 0.4% across trim widths 1/2/3/4 s. |

The whole-process delta is still recorded per run as a cross-check, never as
the acceptance number.

## Counter model and its calibration

NVMe-oF READ → the target RDMA-writes into initiator memory → **`InRdmaWrites`**
is the instrument. Segments cap at ~52428 B, so expected ops = bytes / 52428
for I/O ≥ 256 KiB.

Two properties of this counter are load-bearing and easy to get wrong:

- **It refreshes asynchronously.** Settle ≥ 2 s or a delta reads 0 — which is
  indistinguishable from "no data crossed the wire." The 0-delta failure was
  observed directly.
- **The refresh lag is not symmetric between two sampling points**, which is
  what defeats a two-point bracket (above).

Recalibrated at this run's exact geometry (4 MiB O_DIRECT through XFS on the
256k-chunk RAID 0), which the original 512 KiB `dd` calibration did not cover.
Fixed per-call overhead amortizes cleanly:

| Read pattern | Ratio observed/expected |
|---|---:|
| sequential, 80 MiB | 1.0200 |
| sequential, 400 MiB | 1.0062 |
| sequential, 2 GiB | 1.0022 |

**Random access costs measurably more, and it is filesystem metadata, not
fabric loss.** XFS inode and directory-block reads also traverse NVMe-oF, and
they are absent from the app-byte denominator:

| Read pattern (cold caches) | Extra ops/file | Ratio |
|---|---:|---:|
| sequential, 300 files | +0.26 | 1.0032 |
| random, 300 files of 102400 | +2.47 | 1.0309 |
| random, 1000 files | +2.00 | 1.0249 |
| random, 2000 files | +1.54 | 1.0193 |

This overhead amortizes as directory blocks cache, which yields a falsifiable
prediction that the run then confirmed: a 20 s window (<1 pass over the corpus,
all-cold dentries) measured 1.044, while the 120 s window (~3.4 passes) came in
at 1.0001 — tighter, in the predicted direction, without any change to the
tolerance. That agreement is the main reason to believe the residual is
metadata rather than unexplained wire loss.

The ±5% acceptance tolerance was **not** loosened at any point.

## Acceptance gate

A run is accepted only when all of the following hold; any one failure prints
`REJECTED` with the reason:

1. `mode == sustained` (a rounds-mode JSON cannot be passed off as sustained).
2. Interior-rate counter delta within **±5%** of bytes / 52428.
3. `total_success == total_keys` — every key succeeded.
4. `timed_out` absent.
5. All six error-counter deltas zero.
6. Corpus file count not shrunk (write-safety check).

The gate was dry-run to a deliberate REJECT before first use, and it rejected
two real runs before accepting these two — it is not a rubber stamp. The
estimator returns `NA` rather than a number when its inputs are unusable
(missing window marker, <8 interior samples, degenerate time base), and `NA`
never counts as a pass.

> **Correction, 2026-08-04.** That last sentence described the intent, not the
> code: the driver actually fell back to the whole-process delta on `NA` — the
> estimator known to be biased high, and the exact one that got attempt 1
> rejected at 1.0783. A cell could therefore have passed on a number the method
> had already ruled out. Both drivers now reject on `NA`. The runs on this page
> are unaffected: each produced a valid interior estimate, so the fallback never
> fired. Separately, both helpers were double-counting the drain tail
> (`sustained_drain_sec` is a *subset* of `sustained_window_sec`, not an
> addition); using `window` alone shifts these two runs by ~0.1%.

## What this does and does not establish

**Establishes:**

- `fs_native` sustains 95.91 Gbps of 4 MiB reads for 120 s over this fabric, at
  parity with the measured fio wire ceiling, with a 400 GiB working set that
  exceeds DRAM. The read path sits at the fabric ceiling, with no adapter
  overhead resolvable at this speed.
- Steady state, not a wave-barriered average: 0.13 s drain tail on a 120 s
  window.
- The fabric is clean at this load — zero retransmits, NAKs, RTOs, RNRs,
  out-of-order packets, or protocol errors across both runs.
- The result is counter-corroborated by an independent instrument, not just
  self-reported application throughput.

**Does NOT establish:**

- **Any Falcon offload capability.** The IPU is only the verbs device here.
  Do not cite this against the offload beads.
- **Anything about 400 GbE / 4×400 GbE.** No figure here extrapolates.
- **Host CPU cost.** Not measured in this run; no cores-per-100-Gbps claim is
  made or implied.
- **The current-feature-pack perftest gate.** Still open — see below.
- **Any adapter-side headroom claim.** Parity with the comparator means this
  measurement cannot distinguish `fs_native` overhead from zero; it does not
  prove the adapter is free, only that it is below the fabric's resolution here.
- **Attribution of the ~0.1–4% counter residual** beyond the metadata evidence
  above.
- **Write or mixed-workload behavior.** Read path only.

## Open: the current-feature-pack perftest gate is still unmet

Step 2 of this effort (rerun perftest against the running feature pack) could
**not** be completed. `ib_send_bw` fails reproducibly at exit 17 (EEXIST)
immediately after QP exchange, with no data row, on both directions and across
alternate ports, explicit GID index, reduced iteration counts, and loopback.
`dmesg` shows `ae = 0x50a` (*Connection error: max retries reached*) — exactly 7
events, all within ~175 s, i.e. one per attempt. `qp_cnt=1` rules out QP
exhaustion.

**The failure is isolated to new-QP establishment, not the data path.** The
established `nvme_rdma` connections carrying this corpus stayed healthy
throughout, and an independent 2 GiB `dd` check moved real bytes with an
`InRdmaWrites` delta of exactly 40000 — the model-predicted value — and all six
error counters at zero. That is what makes this result trustworthy despite the
perftest blocker.

Remediation is ACC-side Falcon app work (rtcmd restart / cert regeneration),
which would drop the live NVMe-oF connection, so it was not attempted while the
measurement was in flight. The
[§5.2 perftest gate](../nvmeof-poc-plan.md) therefore remains
**BLOCKED — historical baseline only**; see
[falcon-bringup-provenance.md](falcon-bringup-provenance.md) §5.

Manifest facts captured while attempting the rerun: Feature Pack 0.8 on disk,
`fw_ver 1.145`, `idpf 0.0.774`. **Discrepancy worth recording:** the FP 0.8
README references `idpf 0.0.772`, so the running driver does not match the
documented drop.

## Artifacts

On mkp1 under `/root/mkp1-sustained/`:

- Accepted: `sus31785852646_load.{json,log}` + `_poll.txt`,
  `sus41785852800_load.{json,log}` + `_poll.txt`
- Rejected, retained as evidence for the two defects above:
  `sus1785851_load.json` (ratio 1.0783, 1536 misses),
  `val1785852152_*` (0-delta buffering failure),
  `val21785852334_*` (edge-bracket 0.9475)
- Drivers on the host: `/root/sustained_load3.sh`, `/root/interior_rate.py`,
  `/root/sustained_report.py` — committed as `scripts/ipu-poc/run_sustained_load.sh`,
  `interior_rate.py`, `sustained_report.py`

`_poll.txt` holds the timestamped `InRdmaWrites` series, so the interior-rate
estimate is independently recomputable from the raw samples at any trim width.
