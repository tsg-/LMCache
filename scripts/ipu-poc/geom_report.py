#!/usr/bin/env python3
"""Acceptance gate for a model-page-geometry sustained fs_native load run.

Same gate as ``sustained_report.py`` with two differences that the 144 KiB
model-page geometry forces:

  1. The read counter model ``bytes / SEG`` is passed in rather than hardcoded.
     ``sustained_report.py`` pins ``SEG = 52428``, a value calibrated against
     28 MiB objects where a 0.2-op rounding residue per object is invisible.
     At 147456-byte objects, ``147456 / 52428 = 2.81`` -- the residue is 7% of
     one object's ops, so the constant must be re-established at this geometry
     instead of inherited. The driver derives it from a known-byte calibration
     run and passes it here.
  2. Objects per submit is not 1, so application bytes are
     ``successful_keys * data_size_kb * 1024`` where a "key" is one page, not
     one submit. That is the same formula, but the driver must pass the page
     size (144), never the submit size.

Accept ONLY when:
  1. application bytes and the calibrated RDMA counter delta agree within
     tolerance;
  2. every key succeeded;
  3. no timeout, retransmit, NAK, RTO, RNR, out-of-order or proto error;
  4. the pre-existing corpus file count is unchanged.
"""

import json
import sys

TOL = 0.05  # counter/app-bytes agreement; matches the existing sweeps
# The bench's own throughput field and app_bytes/window are two independent
# derivations of the same quantity. They should agree to well under 1%; a wider
# gap means a units change, a window definition change, or a partially counted
# success set, any of which invalidates the headline number.
GOODPUT_TOL = 0.01

USAGE = (
    "geom_report.py <load.json> <counter_delta> <RetransSegs> <NakSeqErr> "
    "<RTO> <RNRrecv> <OutOfOrder> <InProtoErrors> <corpus_before> "
    "<corpus_after> <run> <page_kb> <seg_bytes> <objects_per_submit>"
)

if len(sys.argv) != 15:
    print(USAGE)
    sys.exit(2)

(
    js,
    d_w,
    d_rt,
    d_nak,
    d_rto,
    d_rnr,
    d_oo,
    d_pe,
    c_before,
    c_after,
    run,
    kb,
    seg,
    ops,
) = sys.argv[1:15]
d_w, d_rt, d_nak = int(d_w), int(d_rt), int(d_nak)
d_rto, d_rnr, d_oo, d_pe = int(d_rto), int(d_rnr), int(d_oo), int(d_pe)
c_before, c_after = int(c_before), int(c_after)
seg = float(seg)
ops_per_submit = int(ops)
if seg <= 0:
    print(f"ABORT: seg_bytes must be positive, got {seg}")
    sys.exit(2)

d = json.load(open(js))
m = d["metrics"]
load = next(v for k, v in m.items() if k != "config" and v.get("operation") == "Load")

keys = load.get("total_keys") or 0
succ = load.get("total_success") or 0
window = load.get("window_sec") or 0.0
drain = load.get("drain_tail_sec") or 0.0
timed_out = bool(load.get("timed_out"))
# MiB/s, not MB/s: result.py defines _MB = 1024 * 1024. Decimal conversion
# understates every figure by 4.86%.
MIB_S_TO_GBPS = 2**20 * 8 / 1e9
gbps = (load.get("throughput_aggregate_mbps") or 0.0) * MIB_S_TO_GBPS
succ_gbps = (load.get("throughput_success_mbps") or 0.0) * MIB_S_TO_GBPS
run_mode = (m.get("config") or {}).get("mode")
cfg_keys = (m.get("config") or {}).get("num_keys")

app_bytes = succ * int(kb) * 1024
expect_ops = app_bytes / seg
ratio = (d_w / expect_ops) if expect_ops else 0.0

print(f"\n===== MODEL-PAGE GEOMETRY LOAD RESULT — run {run} =====")
print(f"  mode                 : {run_mode}")
print(f"  page / objects/submit: {kb} KiB x {ops_per_submit}")
print(f"  window / drain       : {window:.2f} s / {drain:.2f} s")
print(f"  completed submits    : {load.get('submits')}")
print(f"  pages succ / total   : {succ} / {keys}")
print(f"  app bytes            : {app_bytes / 2**30:.2f} GiB")
print(f"  throughput (req)     : {gbps:.2f} Gbps")
# Derived independently of the bench's own throughput field, so a units change
# there shows up as a divergence here rather than silently shifting the result.
# window ALONE, never window + drain: drain is a SUBSET of window.
derived_gbps = (app_bytes * 8 / window / 1e9) if window > 0 else 0.0
if window > 0:
    print(
        f"  throughput (derived) : {derived_gbps:.2f} Gbps"
        f"  [app bytes / window; must track the line above]"
    )
print(
    f"  throughput (success) : {succ_gbps:.2f} Gbps"
    if succ_gbps
    else "  throughput (success) : n/a (all keys succeeded; field omitted)"
)
print(f"  mean submit latency  : {load.get('submit_latency_avg_ms')} ms")
print(f"  --- RDMA counter bracket (read model: bytes / {seg:.1f}) ---")
print(f"  counter delta        : {d_w}")
print(f"  expected ops         : {expect_ops:.0f}")
print(f"  ratio observed/expect: {ratio:.4f}   (tolerance +/-{TOL:.0%})")
print("  --- error counters (all deltas must be 0) ---")
print(
    f"  RetransSegs={d_rt} NakSeqErr={d_nak} RTO={d_rto} "
    f"RNRrecv={d_rnr} OutOfOrder={d_oo} InProtoErrors={d_pe}"
)
print(f"  corpus files before/after: {c_before} / {c_after}")

fail = []
if not (1 - TOL) <= ratio <= (1 + TOL):
    fail.append(f"counter/app-bytes disagree: ratio {ratio:.4f} outside +/-{TOL:.0%}")
if keys == 0 or succ != keys:
    fail.append(f"not all keys succeeded: {succ}/{keys}")
if timed_out:
    fail.append("run reported timed_out")
for name, v in (
    ("RetransSegs", d_rt),
    ("Nak Sequence Error", d_nak),
    ("RTO", d_rto),
    ("RNR received", d_rnr),
    ("Rcvd Out of order packets", d_oo),
    ("InProtoErrors", d_pe),
):
    if v != 0:
        fail.append(f"{name} advanced by {v}")
# Exact equality, not "did not shrink". A read-only cell that somehow adds
# objects under the read prefix means it wrote where it should not have, and a
# growing corpus silently changes the key universe for every later cell.
if c_after != c_before:
    fail.append(f"corpus count changed: {c_before} -> {c_after} (read-only cell)")
# Both throughput derivations must agree, else the headline is not trustworthy.
if gbps > 0 and derived_gbps > 0:
    gap = abs(gbps - derived_gbps) / derived_gbps
    if gap > GOODPUT_TOL:
        fail.append(
            f"reported {gbps:.2f} and derived {derived_gbps:.2f} Gbps "
            f"disagree by {gap:.2%} (> {GOODPUT_TOL:.0%})"
        )
else:
    fail.append("could not derive goodput independently; no cross-check possible")
if run_mode != "sustained":
    fail.append(f"not sustained mode: {run_mode}")
# Fail closed on a missing window: without it there is no independent goodput
# derivation and no cross-check, so nothing here would catch a units or
# window-definition change. An unverifiable cell is not a passing cell.
if window <= 0:
    fail.append(f"window_sec is {window}; no independent goodput derivation")
# The whole point of this run is that the geometry resolver drove it. If
# num_keys came through as anything but the profile's layer count, the cell
# measured the old flat-object shape under a new name. Absent provenance is
# also a failure: it means the run cannot be shown to have used the profile.
if cfg_keys is None:
    fail.append("config.num_keys absent; cannot confirm the profile drove this run")
elif int(cfg_keys) != ops_per_submit:
    fail.append(f"num_keys {cfg_keys} != profile objects/submit {ops_per_submit}")

print()
if fail:
    print("RESULT: REJECTED")
    for f in fail:
        print(f"  - {f}")
    sys.exit(1)
print("RESULT: ACCEPTED — Falcon-offloaded kernel NVMe-oF, existing-controller,")
print("  single-process fs_native sustained load, model-page geometry,")
print("  O_DIRECT re-read corpus.")
print("  Does NOT quantify offload benefit (no unoffloaded control), and is")
print("  NOT fresh-QP/64-QP, R2, physical multi-initiator, or 400 GbE evidence.")
