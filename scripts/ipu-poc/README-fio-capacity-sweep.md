# Matched read-capacity + mixed-ratio FIO sweep

One job shape across three storage surfaces, so a difference between capacity rungs
is a difference in the surface rather than a difference in how it was measured.

**Why it exists.** The three rungs currently on record were each measured with a
different job shape — local at 15 s / ramp 2 / QD16–256 on mkp2, remote raw at
60 s / ramp 5 / **QD32 only**, remote XFS at 60 s / ramp 5 / QD16–256. The gaps
between them therefore confound surface with runtime and depth. This sweep fixes
the shape and varies only the surface.

## Scripts

| File | Role |
|--|--|
| `run_fio_capacity_sweep.sh` | Runs one surface's matrix; writes job files, fio JSON, counter brackets, `iostat -x`, `pidstat`, SMART |
| `fio_sweep_report.py` | Collapses one run's artifacts into a summary JSON with per-cell gates |

## Surfaces

Run each **on the host that owns the surface**, as root.

| `--surface` | Host | Target | Notes |
|--|--|--|--|
| `local_raw` | mkp2 | raw exported PM9A3s | reads only; requires `--confirm-quiesced` |
| `remote_raw` | mkp1 | imported NVMe-oF namespaces | reads only |
| `remote_xfs` | mkp1 | XFS on md0 | the surface `fs_native` uses; the only rung directly comparable to `bench l2` |

Devices are resolved from the live system, not hardcoded. This matters: mkp2 exports
`nvme1n1` and **`nvme2n2`**, not the `nvme1n1`/`nvme2n1` pair the 2026-08-02 baseline
runner named, and mkp1 holds two *idle local* PM9A3s (`nvme0n1`, `nvme1n1`) that a
`nvme*` glob would wrongly include alongside the imported namespaces.

## Matrix

Per surface: **20 read cells** — 5 block sizes × (QD32 × 3 reps + QD16 × 1).
On `remote_xfs`, **8 more mixed cells** — 2 ratios × (QD32 × 3 + QD16 × 1). 28 total.

Block sizes are `4k 16k 144k 256k 512k`. The three large sizes are the model page
geometries; 4 KiB and 16 KiB are the small-page/latency end.

### The reps sit at QD32, not QD16

This is a deliberate change from the original plan. Depth curve measured on
`remote_xfs` at 144 KiB, 2026-08-11:

| QD | 8 | 16 | 24 | 32 | 48 | 64 |
|--|--|--|--|--|--|--|
| Gb/s | 53.5 | 82.6 | 93.6 | **95.6** | 95.7 | 95.7 |

QD16 is ~13 Gb/s short of the ceiling, and its own run-to-run spread is 5.5%
(78.85 / 82.70 / 83.20 over three reps) — larger than the inter-surface gaps the
sweep exists to resolve. Repeating an unsaturated depth three times measures the
generator, not the surface. QD32 is the first saturated depth, so it carries the
reps; QD16 is kept as one point on the depth curve.

### Small block sizes are not capacity results

Measured on `remote_xfs`, 2026-08-11:

| BS | QD16 | QD32 |
|--|--|--|
| 4 KiB | 1.97 Gb/s | 9.12 Gb/s |
| 16 KiB | 18.61 Gb/s | 32.33 Gb/s |
| 144 KiB | 82.56 Gb/s | 95.58 Gb/s |

An order of magnitude under the large-block ceiling, because the binding limit is
per-request cost, not bandwidth. They answer a different question — the per-request
floor, the IOPS ceiling, and how much of the path a small-page geometry could ever
reach — and must not be charted on a capacity axis beside a 512 KiB rung. The
reporter tags every sub-saturation cell so this cannot happen by accident.

### Mixed ratios

`83` is the bytes mix matching `bench l2 --read-write-ratio 5:1` (5/6 = 83.3%), and
`bench l2`'s achieved ratio is also a bytes ratio, so the two are directly
comparable. `90` is a second point on the duplex curve with **no L2 comparator** —
storage-path characterisation only; do not chart it against an L2 bar.

fio applies the mix per-IO, so the achieved ratio is not the requested one
(82.9% observed for a requested 83%). The reporter records the achieved value;
use it, don't assume the requested one.

## Running it

```bash
# mkp1, remote XFS — the L2-comparable surface
./run_fio_capacity_sweep.sh --surface remote_xfs

# mkp1, remote raw (reads only; --read-only skips the mixed matrix, which is
# already implied on non-XFS surfaces)
./run_fio_capacity_sweep.sh --surface remote_raw

# mkp2, local raw — only with the target quiesced (see below)
./run_fio_capacity_sweep.sh --surface local_raw --confirm-quiesced

# summarise
python3 fio_sweep_report.py --run-dir /root/mkp-fio-sweep/<run> --output summary.json
```

Useful overrides: `BS_LIST`, `QD_MAIN`, `QD_SECOND`, `REPS`, `RUNTIME`, `RAMP`,
`MIX_RATIOS`, `RUN_ID`, `FIO_FILE_GB`, `XFS_DIR`. `XFS_DIR` defaults to
`$XFS_MOUNT/fio-sweep` and must not overlap the benchmark corpus. `--dry-run`
prints the matrix without running anything, which is the cheap way to confirm
device resolution on a host.

Full runtime per surface at defaults: 28 cells × 70 s ≈ 33 min, plus a one-time
64 GB file layout on the XFS surface.

## The maintenance window (local_raw)

`local_raw` is the only surface needing one, because a local-capacity number
measured while the target is still serving reads is measuring both loads at once.
Sequence, in order:

1. On mkp1, stop all L2 work and disconnect: `nvme disconnect -n mkp2-nvme1`,
   `nvme disconnect -n mkp2-nvme2`. Unmount `/mnt/lmcache-stage2` and stop `md0` first —
   disconnecting underneath a mounted RAID0 will produce I/O errors, not a clean detach.
2. On mkp2: `./run_fio_capacity_sweep.sh --surface local_raw --confirm-quiesced`.
   The script re-checks `/sys/block/*/inflight` and aborts if anything is still
   attached, but that check is a backstop, not a substitute for step 1.
3. Reconnect on mkp1, reassemble `md0`, remount, and confirm the corpus file count
   matches what it was before.
4. **Immediately** rerun one DeepSeek W=32 read cell and one byte-verified 5:1
   mixed cell. If the reconnect changed anything, this is where it surfaces — in
   the same session, not a week later.

If no window is available: run `remote_raw` and `remote_xfs` on the live
configuration and keep the historical local bar, labelled
**"two-SSD local aggregate, target quiesced, 2026-08-02, 15 s × 1"**. The date
alone is not enough; the 15 s single-repetition runtime is the part that makes it
not comparable to a 60 s × 3 cell.

## What the artifacts are

Per run directory:

```
context.txt              host, kernel, fio version, full parameters, cell tallies,
                         corpus count before/after
jobs/<cell>.fio          the exact generated job file that ran
fio/<cell>.json          fio --output-format=json (group-reported aggregate)
counters/<cell>.pre      eight RDMA counters before the cell
counters/<cell>.delta    their deltas, plus the wall-clock bracket
counters/<cell>.poll     0.25 s counter poll across the cell
iostat/<cell>.txt        iostat -x -t, covering ramp+measured
pidstat/<cell>.txt       per-thread CPU for the fio process tree
smart/<dev>-{before,after}.txt   SMART bracket for the run
```

Bandwidth always comes from fio's `group_reporting` aggregate (`bw_bytes`), never a
per-job number or a hand-rolled sum — the sweep deliberately spreads one job across
two devices, so `disk_util` is the reliable witness of how many devices a cell
touched, not `job_options.filename` (which reports only the last one).

## Gates

Per cell, fatal to that cell only:

- **Fabric errors** — any nonzero delta among `RetransSegs`, `Nak Sequence Error`,
  `RTO`, `RNR received`, `Rcvd Out of order packets`, `InProtoErrors`. The payload
  counters `InRdmaWrites`/`InRdmaReads` are exempt: they are instruments, not errors.
- **Counters present** — a missing counter means no verdict is possible.

Reported but **not** gating:

- **Counter agreement** — the RDMA read counter scaled by the calibrated segment
  constant, against fio's read bandwidth. Non-gating because the constant is
  calibrated per geometry and is not expected to hold at every block size here.
  On the initiator `InRdmaWrites` is the **read** instrument (an NVMe-oF read is
  satisfied by the target RDMA-writing into initiator memory). Do not "fix" that.
- **`bracket_overrun`** — the cell's wall-clock bracket ran materially longer than
  ramp+runtime, meaning work that was not the measured workload landed inside it.
  The counter cross-check is suppressed for such cells rather than computed against
  a diluted elapsed time.

Run-level: the `bench l2` corpus file count must be unchanged. The sweep writes only
under `$XFS_DIR` (default `$XFS_MOUNT/fio-sweep`) and refuses a directory
overlapping the corpus; raw block surfaces additionally refuse any writing pattern
outright, since on `local_raw` those devices hold the exported corpus.

## Known trap: file layout inside the bracket

fio lays out any missing working file *inside the job that needs it*. On a fresh run
that put a 64 GB layout inside the first cell's bracket: 51 s elapsed against a 16 s
job, which made the counter cross-check read 0.30 instead of ~0.99. Throughput itself
survived (fio's ramp/runtime accounting is internal), but every bracket-derived
artifact was wrong. The script now lays the files out before the first timed cell and
the reporter flags any cell whose bracket overruns, so a recurrence is visible rather
than silently absorbed.

## Grafana

The dashboard row **"NVMe IOPS / latency / utilization (matched-capacity sweep)"** in
`docs/design/v1/platform/ipu-poc/instrumentation/dashboards/lmcache-mkp.json` adds the
per-NVMe IOPS, await, and utilization views this sweep needs. Validated against a
4.9 GB/s md0 read: md0 IOPS = 2 × namespace IOPS exactly (md counts the striped
request, the namespaces count the pieces), initiator await 0.144–0.156 ms against
target await 0.094 ms — the difference being the fabric plus target-stack cost of one
request.

Keep `iostat -x` and the `pidstat` brackets as the artifacts of record for
CPU-per-GB claims. Scraped host CPU is fine for trends but too coarse to attribute
cycles to the benchmark. On the irdma path, **do not** use ethtool byte counters as
payload throughput — they do not account offloaded traffic.
