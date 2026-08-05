# DeepSeek-V3 proxy payloads on fs_native — mkp1, 2026-08-04

**Result: 28 MiB/key reads sustain 95.94 Gbps for 120 s, at parity with the
matched fio comparator on the same filesystem.** The read path sits at the
fabric ceiling, with no adapter overhead resolvable at this speed, and
`--in-flight` is already saturated at the smallest value tested (4).

**Classification — read this before citing any number here.** All figures are
**Falcon-backed kernel NVMe-oF, existing-controller, single-process `fs_native`
sustained load**. The IPU acts solely as the `irdma` verbs device beneath the
kernel `nvme_rdma`/`nvmet_rdma` path. These are **not** 64-QP, **not** R2,
**not** physical multi-initiator, **not** 400 GbE, and **not** Falcon offload
evidence. `--in-flight` is user-space submission concurrency; it does not
create QPs. No perftest was run, no controller was created, no NVMe-oF
reconnect occurred, and no queue count was changed — the already-established
controllers carried every byte.

## Payload rationale

DeepSeek-V3 KV-cache proxy geometry, one key per prefill chunk:

| Proxy | Tokens | Bytes/key | `--data-size-kb` | Status |
|---|---:|---:|---:|---|
| 256-token chunk | 256 | 28 MiB | 28672 | **complete, below** |
| 512-token chunk | 512 | 56 MiB | 57344 | not yet run |

Before this run, neither geometry had an accepted sustained result. The only
prior 28 MiB data (`rd_ca4_inf4/inf16/inf64`) was rounds-mode — wave-barriered,
so not a steady-state number — and 56 MiB had never been run at all.

## Result — 28 MiB/key, 100% read

| Cell | in-flight | Window | Submits | Keys succ/total | Throughput | Counter ratio |
|---|---:|---:|---:|---:|---:|---:|
| `ds28m_inf4` | 4 | 120.01 s | 48857 | 48857 / 48857 | 95.63 Gbps | 0.9999 |
| `ds28m_inf16` | 16 | 120.02 s | 49024 | 49024 / 49024 | **95.94 Gbps** | 1.0001 |
| `ds28m_inf64` | 64 | 120.14 s | 49072 | 49072 / 49072 | **95.94 Gbps** | 1.0000 |

Throughput is corroborated two ways per cell that now agree **exactly** at all
three: the bench's own field and an app-bytes-÷-window figure derived
independently of it (95.63/95.63, 95.94/95.94, 95.94/95.94).

All three cells: every key succeeded, no timeout, and all six error counters
(`RetransSegs`, `Nak Sequence Error`, `RTO`, `RNR received`,
`Rcvd Out of order packets`, `InProtoErrors`) delta **zero**. All three passed
the 6-condition acceptance gate.

**Concurrency is already saturated at 4, the smallest value tested.** in-flight
16 and 64 are identical to the reported precision (95.94 both), and 4 is only
0.3% lower. At 28 MiB/key even 4 outstanding submits carry 112 MiB of in-flight
payload, which is already enough to fill the link. **This sweep therefore does
not locate a concurrency knee** — it only bounds it at or below 4. Finding the
knee for this payload would need in-flight 1 and 2, which were not run.

### Against the matched ceiling

New matched comparator captured at this exact block size, read-only against the
same corpus on the same XFS/md0 NVMe-oF path (`/root/fio28m.sh`):

| fio cell (bs=28m, randread, O_DIRECT) | GB/s | Gbps | `fs_native` vs it |
|---|---:|---:|---:|
| numjobs=8, iodepth=2 | 12.01 | 96.04 | **99.9%** |
| numjobs=16, iodepth=2 | 12.04 | 96.28 | 99.6% |
| numjobs=32, iodepth=2 | 12.31 | 98.46 | *(see caveat)* |

**The numjobs=32 cell is not usable as a ceiling.** 98.46 Gbps exceeds the
98.04 Gbps RoCEv2 goodput limit for `active_mtu` 4096 (38 B Ethernet +
44 B IP/UDP/BTH/ICRC per 4096 B payload), so it cannot be pure wire traffic.
The likely cause is readahead serving part of the run from memory: 32 jobs
striped over only 32 distinct files is a narrow working set. It is recorded
here for completeness, not used as a denominator.

Taking numjobs=8/16 as the matched comparator, `fs_native` at 28 MiB is at
**parity** — 99.6–99.9%. Combined with the [4 MiB result](mkp1-fsnative-sustained-2026-08-04.md)
(95.91 Gbps, also parity), the read path sits at the fabric ceiling across a 7×
payload range, with no adapter overhead resolvable at this speed.

## Configuration

| Item | Value |
|---|---|
| Initiator | mkp1, Xeon Gold 6430, RHEL 9.4, kernel `5.14.0-427.13.1.el9_4`, 251 GiB DRAM |
| Target | mkp2, 2× Samsung PM9A3 1.92 TB, one NQN per drive |
| Fabric | 100 GbE Falcon-backed direct IPU↔IPU link, `rocep69s0f0`, `active_mtu` 4096 |
| NVMe-oF | kernel `nvme_rdma` / `nvmet_rdma`, `--nr-io-queues=16` per controller |
| Initiator storage | RAID 0 `md0` across the two remote namespaces, 256k chunk, XFS (`sunit=512,swidth=1024`), `noatime,nodiratime` |
| Adapter | `fs_native`, `num_workers=16`, `use_odirect=true`, `max_capacity_gb=4000` |
| Geometry | `--num-keys 1 --data-size-kb 28672 --warmup-rounds 0`, rounds = 11072/in_flight |
| Window | `--duration-sec 120 --warmup-sec 10` |
| Corpus | 11,072 keys × 28 MiB = **303 GiB**, prefix `ds28m` |
| Commit | `66cba0a5` on `feat/bench-l2-sustained-only` |

`max_capacity_gb=4000` is deliberately far above anything written here.
`fs_native` documents that value as driving "usage tracking / eviction", so a
tight value risks evicting the pre-existing 102,400-file 4 MiB corpus.

### Capacity and write-wear decisions

- The 303 GiB corpus **exceeds 251 GiB DRAM** (1.21×), so a 120 s window cannot
  be served from page cache. O_DIRECT independently bypasses the cache — both
  guards are in place, not just one.
- Written **once**; all three load cells are read-only reuse, so the sweep costs
  303 GiB of flash wear total rather than per cell.
- Capacity was checked before writing and is checked in-script against *missing*
  objects only, so a read-only reuse run cannot abort on a space check it does
  not need. Free space after: 2.7 TB of 3.5 TB.
- **No existing corpus was deleted.** Each payload gets a unique prefix, and the
  script aborts rather than removing data it cannot verify.

### Corpus identity is verified, not assumed

A file count alone cannot distinguish a clean 28 MiB corpus from a truncated
one, a 56 MiB one, or a prepop that died mid-run — all of which would silently
produce a wrong result. `prepop()` therefore writes a manifest only after the
store exits 0 **and** the full object count is on disk, and refuses to reuse a
corpus whose manifest is missing or mismatched.

The `ds28m` corpus predates that logic, so it was verified post-hoc before its
manifest was backfilled: 11,072 objects, exactly **one** distinct object size of
29,360,128 B (= 28672 × 1024), prepop `total_success` 11,072/11,072, and zero
abort or failure lines in the prepop log.

## Reproduce

```bash
# on mkp1; artifacts land in /root/mkp1-sustained
KB=28672 bash /root/ipu-poc/run_payload_sweep.sh              # gate + prepop + sweep
KB=28672 bash /root/ipu-poc/run_payload_sweep.sh --sweep-only  # reuse verified corpus
bash /root/fio28m.sh                                           # matched comparator
```

Committed as `scripts/ipu-poc/run_payload_sweep.sh` with `interior_rate.py`
and `sustained_report.py` in commit `fbae0676`. The sweep itself ran with the pre-fix helpers; the
recorded JSON and counter samples were re-scored with the committed versions
(see below) and every cell still passes, so the numbers above are the
committed-helper numbers even though the original transcript shows the older
ones. The integrity gate (`--no-skip-verify`, 4 keys at 28 MiB) passed before any
measurement: `[Verify] All 4 keys data verified OK`.

## Units correction applied to this run

The reporting script converted the bench's `throughput_aggregate_mbps` as
decimal MB/s, but `result.py` defines `_MB = 1024*1024`, so the field is
**MiB/s**. Every Gbps figure was understated by 4.86%. Detected here because
the Prometheus counter slope (95.93 Gbps) disagreed with the reporter (91.49
Gbps) by exactly that factor.

Resolved against three independent instruments, which agree to 0.1% on
`ds28m_inf64`:

| Instrument | Gbps |
|---|---:|
| App bytes ÷ window (49,072 × 29,360,128 B ÷ 120.142 s) | 95.94 |
| JSON field × 2²⁰ × 8 / 10⁹ | 95.95 |
| Prometheus `rate(lmcache_bench_l2_success_bytes_total)` | 95.93 |

The RDMA counter ratios were **never** affected — the gate compares app bytes to
counter ops and never reads the throughput field — so all acceptance decisions
stand unchanged. `sustained_report.py` now prints an app-bytes-derived figure
next to the bench's own, so a future units change appears as a visible
divergence instead of silently rescaling every result. The fio comparators are
computed from `bw_bytes / 1e9` and were never affected.

## Two further harness fixes applied to these numbers

Both were found by review after the run and re-applied to the recorded JSON and
counter samples — no benchmark rerun was needed, since the raw artifacts retain
everything required to recompute.

**1. The drain tail was double-counted.** `runner.py:474` sets
`sustained_window_sec = last_observed - t_start`, which *already* spans first
submit to last completion, and `sustained_drain_sec = last_observed -
refill_end` is a **subset** of that same interval. Both `sustained_report.py`
and `interior_rate.py` used `window + drain`, so they over-stated the time base.
Fixed to use `window` alone in both places.

The effect is small but it was exactly what broke the independent-check
contract. Before the fix, `ds28m_inf64` reported 95.94 Gbps from the bench field
against 95.83 Gbps derived — a 0.11% divergence that was pure artifact. After,
the two agree to the reported precision at every cell. The counter ratios also
improved, most at the cell with the largest tail:

| Cell | drain | ratio as-run | ratio fixed |
|---|---:|---:|---:|
| `ds28m_inf4` | 0.00 s | 0.9999 | 0.9999 |
| `ds28m_inf16` | 0.02 s | 1.0002 | 1.0001 |
| `ds28m_inf64` | 0.14 s | 1.0011 | **1.0000** |

That the largest correction lands on the largest drain tail is the expected
signature, which is the reason to believe the fix rather than just prefer its
output.

**2. An `NA` interior estimate could still be accepted.** The documented gate
says `NA` never counts as a pass, but both drivers fell back to the
whole-process delta — the estimator that is *known* biased high, and the exact
reason the 1.0783 attempt was rejected. A cell could therefore pass on a number
the method had already ruled out. Both drivers now reject on `NA` and stop the
sweep. This did not affect any cell here: all three returned valid interior
estimates.

## Live metrics during the run

`bench l2` served `--serve-metrics 9101 --metrics-bind-address 127.0.0.1` on
each cell (loopback-only on mkp1). Scraped over the management-plane SSH tunnel:

```bash
ssh -N -L 19102:127.0.0.1:9101 mkp1
curl -s -X POST http://127.0.0.1:9090/-/reload
```

Prometheus job `lmcache_bench` at 5 s, labels `host: mkp1`,
`workload: fs_native`, target `host.docker.internal:19102`. Observed 79 `up=1`
samples across the three 120 s cells (~26 per cell, matching the ~24 expected)
and five real bench series, split by `phase` into `measured` and `warmup` —
which is what let the measured-phase rate be compared against the JSON
independently.

`--web.enable-lifecycle` was added to the Prometheus container so config reloads
no longer require a restart that would interrupt scraping mid-benchmark. The
container was recreated **before** the sweep started, not during it.

## Result — 28 MiB/key, 5:1 read:write

The mixed runner was added after the 100% read sweep and exercised on the same
existing-controller `fs_native` path. It uses one global user-space
`--in-flight 16` window, a prepopulated read prefix (`ds28m`), and a distinct
monotonic write prefix. This is not a 64-QP test: it does not create QPs.

| Run | Window | Read goodput | Write goodput | Aggregate | Achieved ratio |
|---|---:|---:|---:|---:|---:|
| `mix51201785858700` | 120.037 s | 63.89 Gbps | 12.78 Gbps | **76.67 Gbps** | **5.0000:1** |

All 32,650 read keys and 6,530 written keys completed successfully. Three
deterministic samples from the new write prefix were loaded back after the
window and matched their source buffers. The read counter gate uses the
previously calibrated `InRdmaWrites * 52428` model; the write-only calibration
established `InRdmaReads * 4096` for stores:

| Direction | App-expected ops | Interior-slope ops | Ratio |
|---|---:|---:|---:|
| Read | 18,284,279 | 18,272,828 | 0.99937 |
| Write | 46,807,040 | 46,778,795 | 0.99940 |

All six fabric-error deltas were zero. On `mkp2`, both PM9A3s held roughly
15.5k read IOPS plus 3.1k write IOPS at 256 KiB request size and about 100%
utilization, with zero SMART media-error and error-log deltas.

This accepts the current mixed-harness measurement, but it does **not**
attribute the 100%-read to 5:1 read-goodput reduction solely to NAND. The
single global window can let slow stores occupy slots, and the IPU, driver,
PCIe, filesystem, and target media remain shared. A 1:1 falsifier and
operation-level effective-concurrency telemetry are still needed for causal
attribution.

The write-only calibration (`calw1785857800`) ran first: 3,832 successful
stores, 104.78 GiB app bytes, and `InRdmaReads` ratio 1.00001 against the
4,096-byte model. The 120 s mixed cell consumed 178.55 GiB under its fresh
write prefix. No existing corpus was deleted. Source is `f44b42f9` on
`feat/bench-l2-sustained-only`, applied to the rig as `4ad45386`; artifacts are
under `/root/mkp1-sustained/` and target telemetry under `/tmp/` on `mkp2`.

## What this does and does not establish

**Establishes:**

- `fs_native` sustains 95.94 Gbps of 28 MiB reads for 120 s, at parity with the
  matched fio comparator, with a 303 GiB working set that exceeds DRAM.
- Steady state, not a wave-barriered average: ≤0.14 s drain tail on 120 s.
- Submission concurrency is saturated at or below `--in-flight 4` for this
  payload; 16 and 64 are indistinguishable.
- The fabric is clean at this load — zero retransmits, NAKs, RTOs, RNRs,
  out-of-order packets, or protocol errors across all three cells.
- Counter-corroborated by an independent instrument at every cell (ratios
  0.9999–1.0011 against a ±5% tolerance that was never loosened).

**Does NOT establish:**

- **Any Falcon offload capability.** The IPU is only the verbs device here.
- **Anything about 400 GbE / 4×400 GbE.** No figure extrapolates.
- **64-QP, R2, or physical multi-initiator behavior.** Single process, existing
  controllers, 16 I/O queues per controller.
- **Any adapter-side headroom claim.** Parity means this measurement cannot
  resolve `fs_native` overhead from zero, not that the overhead is zero.
- **56 MiB / 512-token behavior.** Not yet run.
- **A bounded 5:1 mixed payload result.** The 120 s cell above proves the
  completed-byte ratio and separate counter models at `--in-flight 16`; it does
  not establish 1:1 behavior, 64-QP behavior, or a causal bottleneck.
- **Host CPU cost.** Not measured.

## Outstanding

1. **56 MiB / 512-token geometry** — 5,568 files = 304 GiB, same sweep. Capacity
   is available (2.7 TB free); needs its own integrity gate and prepop.
2. **Mixed follow-up** — repeat the 5:1 cell, add `--in-flight 64`, and run a
   bounded 1:1 falsifier. Keep fresh write prefixes and a documented reclaim
   policy; add operation-level effective-concurrency telemetry before making a
   causal claim about any read-goodput change.
3. **The current-feature-pack perftest gate remains BLOCKED** — unchanged by this
   run; see [the 4 MiB result](mkp1-fsnative-sustained-2026-08-04.md).

## Artifacts

On mkp1 under `/root/mkp1-sustained/`:

- `ds28m_inf{4,16,64}_load.{json,log}` + `_poll.txt` (timestamped `InRdmaWrites`
  series, so the interior-rate estimate is recomputable at any trim width)
- `ds28m_prepop.{json,log}`, `ds28m_corpus.manifest`
- `sweep28m.out` — full sweep transcript including gate output
- `/root/fio28m.sh` — matched comparator script
