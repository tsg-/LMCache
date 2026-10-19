#!/bin/bash
# fs_native SUSTAINED-window read proof — mkp1 initiator / mkp2 target.
#
# GOAL: one defensible 120 s sustained load result over the existing 100 GbE
# Falcon-offloaded kernel NVMe-oF path.
#
# CLASSIFICATION: Falcon-offloaded kernel NVMe-oF; the MEV IPU carries the
# Falcon/irdma transport beneath kernel nvme_rdma. There is no unoffloaded
# control, so no offload benefit is quantified. NOT 400 GbE.
#
# ---------------------------------------------------------------------------
# WHY ATTEMPT 1 (sus1785851) WAS REJECTED, and what changed:
#
#  1. 1536 keys missed. total_submit_slots = (warmup_rounds + rounds)*in_flight
#     and --warmup-rounds DEFAULTS TO 1. Prepop ran --warmup-rounds 0 => 1600
#     slots (idx 0..102399); the load omitted the flag => 1608 slots, so 8 slots
#     (idx 102400..102911 = 512 keys) were never stored. 24 submits landed there
#     => 24*64 = 1536 misses, exactly as observed.
#     FIX: pass --warmup-rounds 0 on the load so its wrap range is byte-for-byte
#     the range prepop covered. Verified: 57536/57536 keys, zero misses.
#
#  2. Counter ratio 1.0783 (outside +/-5%). See the estimator note below.
#
# CORPUS REUSE: prefix sus1785851 already holds 102400 keys / 400 GiB from
# attempt 1's prepop, and fix (1) makes the load's wrap range identical to it.
# So this run is READ-ONLY: no store phase, no new write wear. Never rm -rf the
# base dir; the pre-existing Stage 2 corpus survives untouched. The mgmt plane is
# never touched -- data plane is ens2f0 / 200.0.0.x only.
#
# READ counter model: NVMe-oF read => target RDMA-writes into initiator memory
# => InRdmaWrites is the instrument. Segments cap at ~52428 B, so expected ops =
# bytes / 52428 for >=256 KiB. hw_counters refresh ASYNCHRONOUSLY.
set -uo pipefail

H=/sys/class/infiniband/rocep69s0f0/ports/1/hw_counters
VENV=/root/lmcache-stage2/.venv/bin/lmcache
B=/mnt/lmcache-stage2/kvcache
OUT=/root/mkp1-sustained
PREFIX=${PREFIX:-sus1785851}          # reuse attempt 1's prepopulated corpus
RUN=${RUN_ID:-sus3$(date +%s)}

# Identical geometry to the prepop that built the corpus.
W=16; INF=8; NK=64; KB=4096; ROUNDS=200
DUR=${DUR:-120}; WARM=${WARM:-10}
SETTLE=2
POLL=0.25
TRIM=${TRIM:-3}                       # sec trimmed from each window edge

ADP="{\"type\":\"fs_native\",\"base_path\":\"$B\",\"num_workers\":$W,\"use_odirect\":true,\"max_capacity_gb\":900}"
mkdir -p "$OUT"

KEYS=$(( ROUNDS * INF * NK ))
GIB=$(( KEYS * KB / 1024 / 1024 ))
echo "=== run $RUN (read-only, corpus prefix=$PREFIX)"
echo "=== geom w=$W inf=$INF nk=$NK kb=$KB rounds=$ROUNDS warmup-rounds=0 dur=${DUR}s warm=${WARM}s"
echo "=== wrap range: $KEYS keys / ${GIB} GiB (DRAM 251 GiB, so it exceeds DRAM)"

have=$(find "$B" -name "${PREFIX}-bench-model@*.data" | wc -l)
echo "=== corpus for prefix: $have files (need $KEYS)"
if [ "$have" -lt "$KEYS" ]; then echo "ABORT: corpus short"; exit 1; fi
corpus_before=$(find "$B" -name "${PREFIX}-bench-model@*.data" | wc -l)

sync; echo 3 > /proc/sys/vm/drop_caches; sleep 2

# --- continuous timestamped counter sampling -------------------------------
POLLF="$OUT/${RUN}_poll.txt"
: > "$POLLF"
( while :; do echo "$(date +%s.%N) $(cat $H/InRdmaWrites)"; sleep $POLL; done ) >> "$POLLF" 2>/dev/null &
POLLPID=$!
trap 'kill $POLLPID 2>/dev/null' EXIT

pre_w=$(cat "$H/InRdmaWrites")
pre_rt=$(cat "$H/RetransSegs")
pre_nak=$(cat "$H/Nak Sequence Error")
pre_rto=$(cat "$H/RTO")
pre_rnr=$(cat "$H/RNR received")
pre_oo=$(cat "$H/Rcvd Out of order packets")
pre_pe=$(cat "$H/InProtoErrors")

# PYTHONUNBUFFERED=1 is load-bearing: without it Python block-buffers into the
# pipe and every line is stamped at process exit, making the window marker
# useless (observed in val1785852152: warmup/window/cleanup all one instant).
echo "--- sustained load ${DUR}s (+${WARM}s discarded warmup) ---"
PYTHONUNBUFFERED=1 "$VENV" bench l2 --l2-adapter "$ADP" --only load \
  --key-prefix "$PREFIX" \
  --num-keys $NK --data-size-kb $KB --in-flight $INF \
  --l1-align-bytes 4096 --warmup-rounds 0 --rounds $ROUNDS \
  --duration-sec "$DUR" --warmup-sec "$WARM" \
  --output "$OUT/${RUN}_load.json" --format json 2>&1 \
  | python3 -u -c 'import sys,time
for l in sys.stdin: sys.stdout.write("%.3f %s" % (time.time(), l))' \
  > "$OUT/${RUN}_load.log"
rc=${PIPESTATUS[0]}

sleep $(( SETTLE + 3 ))   # grace so interior samples exist past the window
post_w=$(cat "$H/InRdmaWrites")
kill $POLLPID 2>/dev/null

d_w=$((post_w - pre_w))
d_rt=$(( $(cat "$H/RetransSegs") - pre_rt ))
d_nak=$(( $(cat "$H/Nak Sequence Error") - pre_nak ))
d_rto=$(( $(cat "$H/RTO") - pre_rto ))
d_rnr=$(( $(cat "$H/RNR received") - pre_rnr ))
d_oo=$(( $(cat "$H/Rcvd Out of order packets") - pre_oo ))
d_pe=$(( $(cat "$H/InProtoErrors") - pre_pe ))
corpus_after=$(find "$B" -name "${PREFIX}-bench-model@*.data" | wc -l)

if [ $rc -ne 0 ]; then
  echo "LOAD FAILED rc=$rc — see $OUT/${RUN}_load.log"
  exit "$rc"
fi

# ---------------------------------------------------------------------------
# Acceptance estimator: INTERIOR STEADY-STATE RATE.
#
# Three estimators were tried; only this one is sound.
#   (a) whole-process delta -- WRONG: includes the discarded warmup's wire
#       traffic while app bytes count the measured window only (attempt 1
#       sus1785851: 1.0783, and 10s@91.9Gbps is the exact excess).
#   (b) edge bracket at t0+SETTLE / t1+SETTLE -- WRONG: assumes the async
#       hw_counters lag is symmetric so it cancels in the difference. It is not
#       (val21785852334: 0.9475, a ~1.06 s undercount).
#   (c) interior least-squares slope over [t0+TRIM, t1-TRIM] -- used here.
#       Depends only on the steady-state slope, so both edge lag and warmup drop
#       out. Measured stable to 0.4% across TRIM = 1,2,3,4 s.
#
# Residual overhead is real wire traffic, not fabric loss: XFS inode and
# directory-block reads also cross NVMe-oF and are absent from the app-byte
# denominator. Measured on this corpus, 4 MiB O_DIRECT, cold caches:
#   sequential  300 files: +0.26 extra ops/file (ratio 1.0032)
#   random      300 files: +2.47               (1.0309)
#   random     1000 files: +2.00               (1.0249)
#   random     2000 files: +1.54               (1.0193)
# It amortizes as directory blocks cache, so a 120 s window (~3.4 passes over
# the same 102400 files) sits lower than a 20 s one.
win_delta=$(python3 "$(dirname "$0")/interior_rate.py" \
  "$OUT/${RUN}_load.log" "$POLLF" "$OUT/${RUN}_load.json" "$TRIM")

echo "=== interior-rate equivalent delta: $win_delta   (whole-process cross-check: $d_w)"

# NA must REJECT, never fall back. The whole-process delta is known biased high
# (it includes the discarded warmup's wire traffic -- that is exactly why the
# 1.0783 attempt was rejected), so substituting it here would accept a run on a
# number this method has already ruled out.
if [ "$win_delta" = "NA" ]; then
  echo "interior estimator returned NA (no window marker, <8 interior samples,"
  echo "or degenerate time base) — the run is unmeasured, not slow."
  echo "RESULT: REJECTED — no valid counter estimate"
  exit 1
fi
USE=$win_delta

python3 "$(dirname "$0")/sustained_report.py" "$OUT/${RUN}_load.json" \
  "$USE" "$d_rt" "$d_nak" "$d_rto" "$d_rnr" "$d_oo" "$d_pe" \
  "$corpus_before" "$corpus_after" "$RUN" "$KB" 2>&1
report_rc=$?

echo "=== artifacts: $OUT/${RUN}_load.json  $OUT/${RUN}_load.log  $POLLF"
exit "$report_rc"
