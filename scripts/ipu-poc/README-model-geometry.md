# Model-page geometry sweep — DeepSeek-V3, 61 × 144 KiB

**Headline: the model-shaped read workload reaches 95.35 Gbps — parity with the
28 MiB proxy — once `num_workers` is raised from 16 to 32.** At the default 16 it
stalls at 82.29 Gbps, and the shortfall is a benchmark tuning artifact, not a
property of the small-page workload.

`fs_native` fans one submit into `min(num_workers, num_keys)` tiles, each handled
by a worker thread that opens, `read()`s, and closes its files **serially**. So
`num_workers`, not `--in-flight`, caps concurrent disk reads. 16 workers × 144
KiB is only ~2.3 MiB of outstanding I/O — far too little to keep the device busy,
where 16 × 28 MiB was 448 MiB and ample. Raising in-flight cannot compensate: it
adds queued submits behind the same 16 threads, which is why depth past 2 bought
+1.5% and pure latency.

Reporting label, verbatim: **Falcon-offloaded kernel NVMe-oF,
existing-controller, single-process `fs_native` sustained load.** The MEV IPU
supplies the Falcon/`irdma` transport beneath kernel `nvme_rdma`/`nvmet_rdma`,
so this *is* a Falcon-offloaded measurement — but there is no unoffloaded
control, so it does **not** quantify offload benefit. Not 64-QP, not fresh-QP
scale, not R2, not physical multi-initiator, not 400 GbE.

## What this run is

A 256-token DeepSeek-V3 retrieval is **61 objects of 147,456 bytes**, not one
28 MiB object. Every prior sweep used the 28 MiB proxy. This one drives the real
page geometry, resolved by `--kvcache-shape-profile` rather than hand-computed:

    profile   docs/design/tools/ipu_traffic_benchmarks/models/deepseek_v3_fp8.yaml
    sha256    b437f3aab29c557162e931a5761eda89299898fd26ee5ffa85433895fd211b63
    resolved  61 objects/submit × 144 KiB = 8.58 MiB per submit, 256 tokens/chunk

## What this run is not

**An O_DIRECT model-page geometry test over a re-read corpus** — not a
replacement for the earlier >DRAM large-object saturation methodology.

- Corpus is 292,800 objects = **40.2 GiB, well under the 251 GiB DRAM.** At
  ~82 Gbps a 120 s window re-reads it **≈28 times.** Quote this caveat with
  every number here.
- `use_odirect: true` is what removes the data-page-cache concern, not corpus
  size. `md0` and XFS are on the *initiator*; `nvmet-rdma` exports raw block
  devices on the target and operates directly on them, so the initiator-side
  XFS page cache that O_DIRECT bypasses is the only host page cache in the path.
- Caches are **not** dropped between cells — that would make every cell
  artificially cold and confuse the steady-state question. A full-corpus
  direct-read calibration runs before the first timed cell, so no timed result
  sits immediately after prepopulation.
- SSD/controller on-device cache remains a caveat, but it is far smaller than
  the corpus.

## Results — 100% read, 120 s sustained windows

| in-flight | Gbps | % of 11.99 GB/s ceiling | app bytes | mean submit latency | counter ratio |
|---:|---:|---:|---:|---:|---:|
| 1 | 53.12 | 55.4% | 742.14 GiB | 1.351 ms | 1.0014 |
| 2 | 81.10 | 84.5% | 1132.92 GiB | 1.771 ms | 1.0000 |
| 4 | 82.18 | 85.7% | 1148.05 GiB | 3.499 ms | 1.0000 |
| 8 | **82.29** | **85.8%** | 1149.69 GiB | 6.991 ms | 1.0003 |

Every cell: all pages succeeded, all six fabric-error counters delta zero,
corpus count unchanged at 292,800, reported and independently derived goodput
agreed to the printed precision.

**Concurrency knee is at 2.** in-flight 1 → 2 buys +52.9%; 2 → 8 buys +1.5%.
Latency then scales linearly at flat throughput (1.771 → 6.991 ms over a 4×
range), which is queueing delay for depth the path cannot use. The 28 MiB sweep
never located a knee because it started at in-flight 4, already past it.

## Worker sweep — the actual knob (60 s windows, in-flight 8)

| `num_workers` | Gbps | % of ceiling | mean submit latency |
|---:|---:|---:|---:|
| 16 (default) | 82.29 | 85.8% | 6.991 ms |
| 24 | 92.56 | 96.5% | 6.216 ms |
| **32** | **95.28 / 95.35 / 95.27** | **99.4%** | 6.0–6.2 ms |
| 40 | 92.05 | 96.0% | 6.238 ms |
| 48 | 70.65 | 73.7% | 8.107 ms |
| 64 | 44.26 / 44.22 / 41.55 | 46.1% | 13.673 ms |

W=32 and W=64 each have a third run from the NUMA experiment below, listed
above; all three agree within run-to-run spread.

**Non-monotonic, with a sharp peak at 32 and a collapse beyond it.** Both ends
were run twice and reproduce tightly (95.28/95.35 and 44.26/44.22), so the
collapse is real and not jitter.

## NUMA implications

The obvious reading of "peak at 32 = cores per socket" was cross-socket
scheduling. **The measurements do not support it as the cause.** Measured, not
inferred (60 s windows, in-flight 8, placement sampled 60× during the timed
window by `numa_placement.py`):

| cell | binding | threads on node0 / node1 | Gbps |
|---|---|---:|---:|
| W=32 | none | 22.1% / 77.9% | 95.27 |
| W=32 | node 0 | 100% / 0% | 95.45 |
| W=48 | node 0 | 100% / 0% | 83.41 |
| W=64 | none | 48.2% / 51.8% | 41.55 |
| W=64 | node 0 | 100% / 0% | 55.87 |
| W=64 | node 1 | 0% / 100% | 53.97 |

Three observations point the same way:

1. **The peak cell is mostly on the far socket.** At W=32 — 95.27 Gbps, 99.4%
   of ceiling — 77.9% of thread samples are on node 1, while the NIC
   (`rocep69s0f0`) is on node 0. If NIC locality drove the peak, this cell should
   have been the slow one.
2. **Confining to one node does not restore W=64.** `numactl
   --cpunodebind=0 --membind=0` achieved full containment (100% node 0, 0 of
   193 threads migrating) and W=64 recovered only to 55.87 Gbps — still 41% below
   peak. Most of the collapse survives the removal of cross-socket scheduling.
3. **Which node makes little difference.** node 0 (with the NIC) 55.87 vs node 1
   (without) 53.97 Gbps — 3.5%. Compare that to the 39% lost from W=32 → W=64.
   NUMA locality is a small term next to the worker-count effect.

`numastat` deltas corroborate: `numa_miss` and `numa_foreign` are **0 across
every cell**, so no allocation ever fell back to a remote node. (These counters
are system-wide rather than per-process, so their node attribution is weak
evidence; the zero misses are the load-bearing part.)

**The pinned curve is the more useful finding.** Holding placement fixed at node
0 and varying only worker count: 32 → 95.45, 48 → 83.41, 64 → 55.87 Gbps. The
collapse reproduces at constant, verified-single-node placement, so it tracks
**worker count** rather than thread placement. Binding did shift the unpinned
W=64 number (41.55 → 55.87), so cross-socket scheduling does cost something at
high thread counts — it looks like a secondary effect on top of a larger cause,
not the cause itself.

Note the direction, which also argues against CPU starvation: halving the
available CPUs (128 logical → 64) made W=64 *faster*. A CPU-contention
explanation predicts the opposite. And with SMT, node 0 is CPUs `0-31,64-95` —
**64 logical CPUs per socket** — so a 32-thread pool never needed both sockets,
and "32 = cores/socket" was a coincidence of numbers rather than a mechanism.

**What remains unexplained.** Each controller exposes 16 NVMe-oF I/O queues
(`queue_count=17` = 16 + admin) across a 2-device RAID0, so 32 workers is
exactly the aggregate hardware queue count — 64 workers oversubscribe it 2:1.
That is now the leading hypothesis and it is *also* just a coincidence of
numbers until tested. Testing it means varying the I/O queue count, which
requires an NVMe-oF reconnect — outside this run's constraints. Untested
alternatives: per-`open`/`close` serialization inside a tile, and md/RAID0
submission-path contention.

At `num_workers=32`, deeper submission concurrency adds nothing: in-flight 8, 16,
and 32 all give 95.33 Gbps while mean latency scales linearly (6.2 → 12.1 → 24.1
ms). The path is saturated; extra depth is pure queueing.

**Recommendation: `num_workers` should scale with page size, not be a fixed
default.** 16 is well-tuned for multi-MiB objects and badly under-provisioned at
144 KiB. Aim for enough outstanding bytes to cover the device queue, and cap it
near the storage path's aggregate I/O queue count rather than at any CPU-derived
number — see the NUMA section for why the per-socket core count is not the
ceiling it appeared to be.

## Supporting baseline — 28 MiB, same rig, 2026-08-04

The 28 MiB number no longer answers "how does the model-shaped workload
perform?" It still answers "can this storage path saturate at large I/O sizes?"
— and that distinction is what makes the 82.29 Gbps result diagnosable.

| Geometry | Gbps | Source |
|---|---:|---|
| 1 × 28 MiB, in-flight 16/64 | 95.94 | `mkp1-deepseek-proxy-2026-08-04.md` |
| 4 MiB/key sustained | 95.91 | `mkp1-fsnative-sustained-2026-08-04.md` |
| fio raw wire ceiling | 95.92 | `mkp1-mkp2-d1-fs-ceiling-2026-08-02.md` |

The 08-03 payload sweep (256 KiB → 66.9% of ceiling, 4 MiB → 99.2%) predicted a
shortfall at 144 KiB, and at the default worker count that is what appeared. But
that sweep held `num_workers=16` throughout, so it conflated page size with
worker under-provisioning. **The 144 KiB result at 32 workers reaches 99.4% of
ceiling, so that curve is not a property of payload size alone** — the earlier
"payload is the read knob" conclusion needs re-examining at matched outstanding
bytes. Tracked as follow-on work; the 256 KiB and 512 KiB cells were not re-run.

## Counter model — recalibrated, not inherited

The existing gate pins `SEG = 52428` bytes per `InRdmaWrites` op, calibrated at
28 MiB. **At 144 KiB the correct value is 45370 — 13.5% different.** Inheriting
it would have biased every cell's counter check. Calibration is recorded scoped
to profile SHA, page size, objects/submit, and key prefix; the driver refuses to
apply an out-of-scope constant.

| direction | counter | short | long | agreement |
|---|---|---:|---:|---:|
| read | `InRdmaWrites` | 45372.0 | 45369.8 | 0.005% |
| write | `InRdmaReads` | 4096.0 | 4083.9 | 0.30% |

Note the inversion: on the initiator `InRdmaWrites` is the **read** instrument
(the target writes the payload back) and `InRdmaReads` is the **write**
instrument. Do not "fix" it. The write model matches the 28 MiB value; only the
read model moved.

## Integrity

`--only store` / `--only load` cannot byte-verify, and the integrity gate checks
a throwaway namespace. So the corpus itself is byte-verified separately:
`geom_readback.py` re-derives the deterministic fill (`data.py` fills object *i*
of a submit with byte `i & 0xFF`) and compares it. Post-sweep: **183 objects
across 3 submit slots matched.** Bounded by design — 3 of 4800 slots, at the
corpus edges and centre, to catch truncation, wrong page size, wrong geometry,
and cross-key mis-offset. Not a full-corpus verification.

## Reproducing

    bash run_model_geometry.sh all        # gate, calibrate, prepopulate, sweep

Or stage by stage: `gate`, `calib`, `prepop`, `calibr`, `sweep`. Read-consuming
modes require an exact object count and a manifest whose profile SHA matches.
The driver never deletes an existing corpus; on any mismatch it aborts and tells
you to use a new `PREFIX`.

`run_payload_sweep.sh` is the historical 28 MiB driver and is deliberately
untouched by this work.

## Not yet run

- **5:1 mixed at this geometry.** Prerequisite for comparing against the 76.67
  Gbps 28 MiB mixed result.
- **2 and 4 synchronized local initiators** with shared read keys and distinct
  write prefixes.
- **Other profiles.** Llama-3 70B/405B and Mixtral 8x22B are provisioned with
  recorded SHAs but unmeasured.
- **Why the pool collapses past 32 workers.** NUMA does not account for it (see
  above), so the cause is still open. The I/O-queue-count hypothesis (32 = 2 ×
  16 hardware queues) needs a queue-count change to test, which requires an
  NVMe-oF reconnect and is out of scope here.
- **Re-running the 08-03 payload sweep at matched outstanding bytes**, to
  separate page size from worker provisioning.
- **The last 0.6%.** 95.35 vs the 95.92 Gbps fio ceiling is within the
  comparator's own 11.65–11.98 GB/s spread, so it reads as parity rather than a
  gap worth chasing. Beyond it, the remaining candidates are per-`open`/`close`
  overhead (one pair per object per read, 8.4M opens in a 120 s window) and
  `preadv`-style batching within a tile — neither measured.
