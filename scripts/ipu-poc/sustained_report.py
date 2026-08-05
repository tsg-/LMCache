#!/usr/bin/env python3
"""Apply the step-6 acceptance gate to a sustained fs_native load run.

Accept ONLY when:
  1. application bytes and the calibrated RDMA counter delta agree within
     tolerance (read model: ops = bytes / 52428);
  2. every key succeeded;
  3. no timeout, retransmit, NAK, RTO, RNR, out-of-order or proto error;
  4. the pre-existing corpus file count is unchanged.
"""

import json
import sys

SEG = 52428.0  # calibrated read segment cap, bytes per InRdmaWrite op
TOL = 0.05  # existing tolerance: 5%

(js, d_w, d_rt, d_nak, d_rto, d_rnr, d_oo, d_pe, c_before, c_after, run, kb) = sys.argv[
    1:13
]
d_w, d_rt, d_nak = int(d_w), int(d_rt), int(d_nak)
d_rto, d_rnr, d_oo, d_pe = int(d_rto), int(d_rnr), int(d_oo), int(d_pe)
c_before, c_after = int(c_before), int(c_after)

d = json.load(open(js))
m = d["metrics"]
load = next(v for k, v in m.items() if k != "config" and v.get("operation") == "Load")

keys = load.get("total_keys") or 0
succ = load.get("total_success") or 0
# Emitted key names differ by mode: sustained runs emit window_sec /
# drain_tail_sec / submits; timed_out appears ONLY when true; and
# throughput_success_mbps appears only when success != keys.
window = load.get("window_sec") or 0.0
drain = load.get("drain_tail_sec") or 0.0
timed_out = bool(load.get("timed_out"))
# The bench reports these in MiB/s: result.py defines _MB = 1024*1024. Treating
# them as decimal MB/s understates every figure by 4.86%, so scale by 2**20
# rather than 1e6. Cross-checked three ways on ds28m_inf64: app bytes/window
# 95.94, this conversion 95.95, Prometheus counter slope 95.93 Gbps.
MIB_S_TO_GBPS = 2**20 * 8 / 1e9
gbps = (load.get("throughput_aggregate_mbps") or 0.0) * MIB_S_TO_GBPS
succ_gbps = (load.get("throughput_success_mbps") or 0.0) * MIB_S_TO_GBPS
# mode lives in the config section, not the per-op section.
run_mode = (m.get("config") or {}).get("mode")

app_bytes = succ * int(kb) * 1024
expect_ops = app_bytes / SEG
ratio = (d_w / expect_ops) if expect_ops else 0.0

print(f"\n===== SUSTAINED LOAD RESULT — run {run} =====")
print(f"  mode                 : {run_mode}")
print(f"  window / drain       : {window:.2f} s / {drain:.2f} s")
print(f"  completed submits    : {load.get('submits')}")
print(f"  keys succ / total    : {succ} / {keys}")
print(f"  app bytes            : {app_bytes / 2**30:.2f} GiB")
print(f"  throughput (req)     : {gbps:.2f} Gbps")
# Derived from app bytes and the window independently of the bench's own
# throughput field, so a units change there shows up as a divergence here
# instead of silently shifting every reported number.
# window ALONE, never window + drain: runner.py sets
# sustained_window_sec = last_observed - t_start, which already spans first
# submit to last completion, and sustained_drain_sec = last_observed -
# refill_end is a SUBSET of that same interval. Adding them double-counts the
# tail and understates the rate.
if window > 0:
    print(
        f"  throughput (derived) : {app_bytes * 8 / window / 1e9:.2f} Gbps"
        f"  [app bytes / window; must track the line above]"
    )
print(
    f"  throughput (success) : {succ_gbps:.2f} Gbps"
    if succ_gbps
    else "  throughput (success) : n/a (all keys succeeded; field omitted)"
)
print(f"  mean submit latency  : {load.get('submit_latency_avg_ms')} ms")
print("  --- RDMA counter bracket (read model: InRdmaWrites) ---")
print(f"  InRdmaWrites delta   : {d_w}")
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
if c_after < c_before:
    fail.append(f"corpus shrank: {c_before} -> {c_after}")
if run_mode != "sustained":
    fail.append(f"not sustained mode: {run_mode}")

print()
if fail:
    print("RESULT: REJECTED")
    for f in fail:
        print(f"  - {f}")
    sys.exit(1)
print("RESULT: ACCEPTED — Falcon-backed kernel NVMe-oF sustained read")
print("  (NOT Falcon offload; NOT 400 GbE)")
