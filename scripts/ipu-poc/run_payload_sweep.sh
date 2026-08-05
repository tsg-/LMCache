#!/bin/bash
# DeepSeek-V3 proxy payload sweep — 100% READ, sustained windows.
#
# Falcon-backed kernel NVMe-oF, existing-controller, single-process fs_native
# sustained load. NOT 64-QP, NOT R2, NOT physical multi-initiator, NOT 400 GbE,
# NOT Falcon offload evidence. --in-flight is user-space submission concurrency;
# it does not create QPs.
#
# HARD CONSTRAINTS honoured here:
#   - No perftest, no new NVMe controller, no NVMe-oF reconnect, no queue-count
#     change, no fresh RC QP. Uses ONLY the already-established controllers.
#   - Never deletes an existing corpus. Each payload gets its own unique prefix.
#   - max_capacity_gb is set far above anything this writes, because fs_native
#     documents that value as driving "usage tracking / eviction" and the
#     102400-file sus1785851 corpus must not be evicted.
#
# Usage: KB=28672 bash run_payload_sweep.sh [--prepop-only|--sweep-only]
set -uo pipefail

H=/sys/class/infiniband/rocep69s0f0/ports/1/hw_counters
VENV=/root/lmcache-stage2/.venv/bin/lmcache
B=/mnt/lmcache-stage2/kvcache
OUT=/root/mkp1-sustained
SD=$(dirname "$0")

KB=${KB:?set KB=28672 or 57344}
MIB=$(( KB / 1024 ))
case "$KB" in
  28672) NFILES=11072 ;;   # 303 GiB = 1.21x DRAM; divisible by 4,16,64
  57344) NFILES=5568  ;;   # 304 GiB = 1.21x DRAM; divisible by 4,16,64
  *) echo "unsupported KB=$KB (add an NFILES divisible by 64)"; exit 2 ;;
esac
PREFIX=${PREFIX:-ds${MIB}m}
W=16; NK=1
DUR=${DUR:-120}; WARM=${WARM:-10}
SETTLE=2; POLL=0.25; TRIM=3
CAP=4000                  # GB, >> anything written here; keeps eviction inert
METRICS_PORT=${METRICS_PORT:-9101}
METRICS_BIND_ADDRESS=${METRICS_BIND_ADDRESS:-127.0.0.1}

ADP="{\"type\":\"fs_native\",\"base_path\":\"$B\",\"num_workers\":$W,\"use_odirect\":true,\"max_capacity_gb\":$CAP}"
mkdir -p "$OUT"
GIB=$(python3 -c "print(f'{$NFILES*$MIB/1024:.0f}')")

echo "############################################################"
echo "# payload ${MIB} MiB/key  prefix=$PREFIX  corpus=$NFILES files = ${GIB} GiB"
echo "# DRAM 251 GiB, so the corpus exceeds it; O_DIRECT also bypasses cache"
echo "############################################################"

# NOTE: capacity is checked inside prepop(), against only the objects actually
# missing. Checking the full corpus size up front would abort a purely
# read-only reuse run that needs no new space at all.

# Fail fast on the wrong install. These options exist only on the
# sustained-bench branch (66cba0a5,
# feat/bench-l2-sustained-only); on a main-worktree install every command below
# would die in argument parsing instead, which reads as a benchmark failure.
for flag in --key-prefix --duration-sec --warmup-rounds --serve-metrics --metrics-bind-address; do
  if ! "$VENV" bench l2 --help 2>&1 | grep -q -- "$flag"; then
    echo "ABORT: '$VENV bench l2' does not accept $flag."
    echo "       Pin the install to feat/bench-l2-sustained-only (66cba0a5)."
    exit 1
  fi
done
echo "preflight: bench l2 accepts sustained, namespace, and metrics options"

# ---------------------------------------------------------------- integrity
gate() {
  local G="gate${MIB}m$(date +%s)"
  echo "--- [gate] combined store+load --no-skip-verify (${MIB} MiB x 4) ---"
  PYTHONUNBUFFERED=1 "$VENV" bench l2 --l2-adapter "$ADP" \
    --key-prefix "$G" --num-keys $NK --data-size-kb "$KB" --in-flight 4 \
    --l1-align-bytes 4096 --warmup-rounds 0 --rounds 4 --no-skip-verify \
    > "$OUT/${G}.log" 2>&1
  local rc=$?
  grep -iE "verif|All .* keys" "$OUT/${G}.log" | head -3
  if [ $rc -ne 0 ]; then echo "GATE FAILED rc=$rc — STOPPING"; tail -20 "$OUT/${G}.log"; exit 1; fi
  echo "  [gate] PASSED"
}

# ---------------------------------------------------------------- prepopulate
# Corpus identity lives in a manifest, not in a file count. A count alone cannot
# tell a clean 28 MiB corpus from a truncated one, from a 56 MiB one, or from a
# prepop that died mid-run -- all of which would silently produce a wrong result.
# The manifest is written ONLY after a prepop exits 0 with the full object count,
# so its presence is itself the "completed cleanly" signal.
MANIFEST="$OUT/${PREFIX}_corpus.manifest"

verify_manifest() {
  [ -f "$MANIFEST" ] || return 1
  local m_kb m_n
  m_kb=$(grep '^data_size_kb=' "$MANIFEST" | cut -d= -f2)
  m_n=$(grep '^nfiles=' "$MANIFEST" | cut -d= -f2)
  [ "$m_kb" = "$KB" ] || { echo "  manifest payload mismatch: $m_kb != $KB"; return 1; }
  [ "$m_n" = "$NFILES" ] || { echo "  manifest count mismatch: $m_n != $NFILES"; return 1; }
  # Spot-check that a real file still has the exact expected byte size.
  local f sz
  f=$(find "$B" -name "${PREFIX}-bench-model@*.data" | head -1)
  [ -n "$f" ] || { echo "  manifest present but no objects found"; return 1; }
  sz=$(stat -c %s "$f")
  [ "$sz" = "$(( KB * 1024 ))" ] || { echo "  object size $sz != $(( KB * 1024 ))"; return 1; }
  return 0
}

prepop() {
  local have; have=$(find "$B" -name "${PREFIX}-bench-model@*.data" | wc -l)
  if [ "$have" -ge "$NFILES" ] && verify_manifest; then
    echo "--- [prepop] $have files + validated manifest, reusing (read-only) ---"
    return
  fi
  if [ "$have" -ge "$NFILES" ]; then
    echo "ABORT: $have objects exist under prefix '$PREFIX' but the manifest is"
    echo "       missing or does not match this geometry. Refusing to reuse an"
    echo "       unverified corpus, and refusing to delete it. Use a new PREFIX."
    exit 1
  fi

  # Capacity for the MISSING objects only, so read-only reuse never trips this.
  local missing=$(( NFILES - have ))
  local avail need
  avail=$(df -BG --output=avail /mnt/lmcache-stage2 | tail -1 | tr -dc 0-9)
  need=$(( missing * MIB / 1024 + 20 ))
  echo "capacity: ${avail}G avail, need ~${need}G for $missing missing objects"
  [ "$avail" -lt "$need" ] && { echo "ABORT: insufficient capacity"; exit 1; }
  if [ "$have" -gt 0 ]; then
    echo "ABORT: partial corpus ($have/$NFILES) under '$PREFIX' with no valid"
    echo "       manifest. A resumed store would not reproduce one geometry."
    echo "       Use a new PREFIX; this script never deletes existing data."
    exit 1
  fi

  local R=$(( NFILES / 64 ))
  echo "--- [prepop] writing $NFILES x ${MIB} MiB (${GIB} GiB), in_flight=64 rounds=$R ---"
  echo "    WRITE WEAR: one-time ${GIB} GiB; all later load cells are read-only."
  PYTHONUNBUFFERED=1 "$VENV" bench l2 --l2-adapter "$ADP" --only store \
    --key-prefix "$PREFIX" --num-keys $NK --data-size-kb "$KB" --in-flight 64 \
    --l1-align-bytes 4096 --warmup-rounds 0 --rounds $R \
    --output "$OUT/${PREFIX}_prepop.json" --format json \
    > "$OUT/${PREFIX}_prepop.log" 2>&1
  local rc=$?
  tail -3 "$OUT/${PREFIX}_prepop.log" | grep -E "keys|MB/s" || true
  [ $rc -ne 0 ] && { echo "PREPOP FAILED rc=$rc — STOPPING"; exit 1; }
  have=$(find "$B" -name "${PREFIX}-bench-model@*.data" | wc -l)
  echo "  [prepop] $have files"
  [ "$have" -lt "$NFILES" ] && { echo "ABORT: corpus short ($have < $NFILES)"; exit 1; }

  # Written only now: rc==0 AND the full object count is on disk. Presence of
  # this file is the durable "this corpus is complete and of this geometry"
  # claim that later read-only reuse validates against.
  {
    echo "prefix=$PREFIX"
    echo "data_size_kb=$KB"
    echo "nfiles=$NFILES"
    echo "object_bytes=$(( KB * 1024 ))"
    echo "num_keys=$NK"
    echo "store_in_flight=64"
    echo "store_rounds=$R"
    echo "warmup_rounds=0"
    echo "commit=$(cd /root/lmcache-stage2 && git rev-parse --short HEAD 2>/dev/null)"
  } > "$MANIFEST"
  echo "  [prepop] manifest: $MANIFEST"
}

# ---------------------------------------------------------------- one load cell
cell() {
  local INF=$1 TAG=$2
  local R=$(( NFILES / INF ))   # exact wrap over the whole corpus
  local RUN="${PREFIX}_inf${INF}${TAG}"
  echo "--- [cell] in_flight=$INF rounds=$R (wraps exactly $NFILES keys) ---"

  sync; echo 3 > /proc/sys/vm/drop_caches; sleep 2
  local POLLF="$OUT/${RUN}_poll.txt"; : > "$POLLF"
  ( while :; do echo "$(date +%s.%N) $(cat $H/InRdmaWrites)"; sleep $POLL; done ) >> "$POLLF" 2>/dev/null &
  local PP=$!

  local p_w p_rt p_nak p_rto p_rnr p_oo p_pe
  p_w=$(cat "$H/InRdmaWrites"); p_rt=$(cat "$H/RetransSegs")
  p_nak=$(cat "$H/Nak Sequence Error"); p_rto=$(cat "$H/RTO")
  p_rnr=$(cat "$H/RNR received"); p_oo=$(cat "$H/Rcvd Out of order packets")
  p_pe=$(cat "$H/InProtoErrors")

  PYTHONUNBUFFERED=1 "$VENV" bench l2 --l2-adapter "$ADP" --only load \
    --key-prefix "$PREFIX" --num-keys $NK --data-size-kb "$KB" --in-flight $INF \
    --l1-align-bytes 4096 --warmup-rounds 0 --rounds $R \
    --duration-sec "$DUR" --warmup-sec "$WARM" \
    --serve-metrics "$METRICS_PORT" --metrics-bind-address "$METRICS_BIND_ADDRESS" \
    --output "$OUT/${RUN}_load.json" --format json 2>&1 \
    | python3 -u -c 'import sys,time
for l in sys.stdin: sys.stdout.write("%.3f %s" % (time.time(), l))' \
    > "$OUT/${RUN}_load.log"
  local rc=${PIPESTATUS[0]}
  sleep $(( SETTLE + 3 )); kill $PP 2>/dev/null

  local d_w=$(( $(cat "$H/InRdmaWrites") - p_w ))
  local d_rt=$(( $(cat "$H/RetransSegs") - p_rt ))
  local d_nak=$(( $(cat "$H/Nak Sequence Error") - p_nak ))
  local d_rto=$(( $(cat "$H/RTO") - p_rto ))
  local d_rnr=$(( $(cat "$H/RNR received") - p_rnr ))
  local d_oo=$(( $(cat "$H/Rcvd Out of order packets") - p_oo ))
  local d_pe=$(( $(cat "$H/InProtoErrors") - p_pe ))
  local cn; cn=$(ls "$B" | wc -l)

  [ $rc -ne 0 ] && { echo "LOAD FAILED rc=$rc"; tail -15 "$OUT/${RUN}_load.log"; exit 1; }

  local wd; wd=$(python3 "$SD/interior_rate.py" "$OUT/${RUN}_load.log" "$POLLF" "$OUT/${RUN}_load.json" $TRIM)
  echo "    interior-rate delta: $wd  (whole-process: $d_w)"
  # NA must REJECT, never fall back. The whole-process delta is known biased
  # high (it includes the discarded warmup's wire traffic), so substituting it
  # would let a cell pass on a number the method has already rejected.
  if [ "$wd" = "NA" ]; then
    echo "    interior estimator returned NA (no window marker, <8 interior"
    echo "    samples, or degenerate time base) — the cell is unmeasured."
    echo ">>> CELL REJECTED — STOPPING SWEEP <<<"
    exit 1
  fi
  local USE=$wd

  python3 "$SD/sustained_report.py" "$OUT/${RUN}_load.json" \
    "$USE" "$d_rt" "$d_nak" "$d_rto" "$d_rnr" "$d_oo" "$d_pe" "$cn" "$cn" "$RUN" "$KB"
  local grc=$?
  # Stop on fabric errors or counter/app mismatch, per instruction.
  [ $grc -ne 0 ] && { echo ">>> CELL REJECTED — STOPPING SWEEP <<<"; exit 1; }
}

MODE=${1:-all}
case "$MODE" in
  --prepop-only) gate; prepop ;;
  --sweep-only)  for i in 4 16 64; do cell $i ""; done ;;
  *)             gate; prepop; for i in 4 16 64; do cell $i ""; done ;;
esac
echo "=== done: payload ${MIB} MiB, artifacts in $OUT/${PREFIX}_* ==="
