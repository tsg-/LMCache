#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
#
# N-initiator fs_native sweep at DeepSeek-V3 model-page geometry (61 x 144 KiB).
#
# Runs 100%-read and 5:1 mixed cells at 1, 2 and 4 synchronized local
# initiator processes over the shared read corpus built by
# run_model_geometry.sh. Every initiator reads the SAME keys and writes to its
# OWN prefix, so reads exercise the shared path while writes cannot collide.
#
# WORKER BUDGET IS HELD CONSTANT, NOT PER-PROCESS. Read throughput at this
# geometry peaks at 32 fs_native worker threads and collapses past it (32 ->
# 95.4, 48 -> 83.4, 64 -> 55.9 Gbps, measured at pinned single-node
# placement). Giving each of 4 initiators 32 workers would put 128 threads on
# the path and re-measure that collapse instead of the initiator count. So the
# default splits one 32-thread budget across the initiators -- 1x32, 2x16,
# 4x8 -- and the sweep varies process count at constant total concurrency.
# Set WORKERS_TOTAL to change the budget, or WORKERS_PER to override the split
# directly when the per-process view is what you want.
#
# Reporting label, verbatim: Falcon-offloaded kernel NVMe-oF,
# existing-controller, single-process fs_native sustained load -- extended here
# to local multi-process. This does NOT quantify Falcon offload benefit (there
# is no unoffloaded control) and is NOT fresh-QP/64-QP, R2, physical
# multi-initiator, or 400 GbE evidence.
#
# Usage:
#   bash run_geom_multi.sh read           # 1,2,4 initiators, 100% read
#   bash run_geom_multi.sh mixed          # 1,2,4 initiators, 5:1
#   bash run_geom_multi.sh all            # read then mixed
#   INITIATORS=2 bash run_geom_multi.sh one-read
set -uo pipefail

H=/sys/class/infiniband/rocep69s0f0/ports/1/hw_counters
GEOM=/root/lmcache-geom
PY=/root/lmcache-stage2/.venv/bin/python
LM=("$PY" -m lmcache.cli.main)
B=/mnt/lmcache-stage2/kvcache
OUT=${OUT:-/root/mkp1-geom-multi}
SD=$(dirname "$0")
PROFILE=${PROFILE:-$SD/models/deepseek_v3_fp8.yaml}
[ -f "$PROFILE" ] || { echo "ABORT: profile not found: $PROFILE"; exit 1; }
PROFILE_SHA=$(sha256sum "$PROFILE" | cut -d' ' -f1)

export PYTHONPATH=$GEOM

PREFIX=${PREFIX:-dsgeom144}
# Corpus and calibration are produced by run_model_geometry.sh; this driver
# consumes them read-only and never builds or modifies either.
GEOM_OUT=${GEOM_OUT:-/root/mkp1-geom}
CORPUS_MANIFEST=${CORPUS_MANIFEST:-$GEOM_OUT/${PREFIX}_corpus.manifest}
CALIB=${CALIB:-$GEOM_OUT/calibration.txt}
SUBMITS=${SUBMITS:-4800}
WORKERS_TOTAL=${WORKERS_TOTAL:-32}
# Read cells reuse the 120 s window of the single-process sweep so the 1x cell
# is directly comparable. Mixed cells use 60 s because their writes are
# monotonic and never wrap: at 5:1 a saturated 120 s window would land ~220 GiB
# and ~1.6M new files per cell, which both burns SSD and changes the directory
# size later cells read through.
DUR_READ=${DUR_READ:-120}
DUR_MIXED=${DUR_MIXED:-60}
WARM=${WARM:-10}
# Mixed mode REFUSES --warmup-sec: a timed warmup would issue stores that the
# mixed accounting cannot attribute, so the bench rejects the combination
# outright. Mixed cells therefore have no warmup and their window includes
# adapter ramp-up, which biases them slightly LOW relative to the read cells.
# Quote that asymmetry with any read-vs-mixed comparison.
WARM_MIXED=${WARM_MIXED:-0}
RATIO=${RATIO:-5:1}
SWEEP_N=${SWEEP_N:-"1 2 4"}
IN_FLIGHT=${IN_FLIGHT:-8}
SETTLE=2; POLL=0.25; TRIM=3
CAP=4000
METRICS_BASE_PORT=${METRICS_BASE_PORT:-9101}
MIN_FREE_GB=${MIN_FREE_GB:-600}
MAX_SKEW=${MAX_SKEW:-2}
# Each mixed cell's own write prefixes are removed after the cell is reported,
# so every cell sees a comparable directory. Only prefixes this script created
# in this run are ever touched; the shared read corpus is never deleted.
KEEP_WRITES=${KEEP_WRITES:-0}

mkdir -p "$OUT"

# ------------------------------------------------------- geometry from profile
# Read back from the resolver rather than hardcoding 61 x 144, so a profile
# edit cannot silently desynchronise this driver from the corpus.
read -r NK PAGE_KB PAGE_B MODEL TOK < <("$PY" - "$PROFILE" <<'PYEOF'
import sys
from lmcache.cli.commands.bench.l2_adapter_bench.geometry import (
    resolve_geometry_profile,
)
g = resolve_geometry_profile(sys.argv[1])
print(g.objects_per_submit, g.data_size_kb, g.page_size_bytes,
      g.model_name, g.tokens_per_chunk)
PYEOF
)
# `read` masks the resolver's exit status, so validate the values themselves.
[[ "${NK:-}" =~ ^[0-9]+$ && "${PAGE_KB:-}" =~ ^[0-9]+$ && "${PAGE_B:-}" =~ ^[0-9]+$ ]] ||
  { echo "ABORT: could not resolve geometry from $PROFILE"; exit 1; }
CORPUS_OBJECTS=$(( SUBMITS * NK ))

echo "=== $MODEL: $NK objects x $PAGE_B B ($PAGE_KB KiB), $TOK tokens/chunk ==="
echo "    profile sha256 $PROFILE_SHA"
echo "    worker budget  $WORKERS_TOTAL total threads, split across initiators"

# ----------------------------------------------------------------- preflight
for flag in --kvcache-shape-profile --read-write-ratio --write-key-prefix \
            --duration-sec --serve-metrics; do
  if ! "${LM[@]}" bench l2 --help 2>&1 | grep -q -- "$flag"; then
    echo "ABORT: bench l2 lacks $flag"; exit 1
  fi
done

free_gb=$(df -BG --output=avail /mnt/lmcache-stage2 | tail -1 | tr -dc 0-9)
[ "${free_gb:-0}" -ge "$MIN_FREE_GB" ] ||
  { echo "ABORT: free capacity ${free_gb}G below ${MIN_FREE_GB}G"; exit 1; }
# --output=iavail already selects available inodes; -i cannot be combined here.
free_inodes=$(df --output=iavail /mnt/lmcache-stage2 | tail -1 | tr -dc 0-9)
echo "    free: ${free_gb}G, ${free_inodes} inodes"

count_objects() { find "$B" -name "$1-bench-model@*.data" | wc -l; }

# The shared read corpus must be exactly the one the manifest describes, and
# must have been built from THIS profile. A same-sized different profile would
# otherwise reuse an older corpus and report the new SHA.
require_corpus() {
  [ -f "$CORPUS_MANIFEST" ] ||
    { echo "ABORT: missing corpus manifest $CORPUS_MANIFEST"; exit 1; }
  local m_sha m_page m_nk
  m_sha=$(grep '^profile_sha256=' "$CORPUS_MANIFEST" | cut -d= -f2)
  m_page=$(grep '^data_size_kb=' "$CORPUS_MANIFEST" | cut -d= -f2)
  # The manifest spells this num_keys, matching the CLI flag it came from.
  m_nk=$(grep '^num_keys=' "$CORPUS_MANIFEST" | cut -d= -f2)
  [ "$m_sha" = "$PROFILE_SHA" ] ||
    { echo "ABORT: corpus built from profile $m_sha, not $PROFILE_SHA"; exit 1; }
  [ "$m_page" = "$PAGE_KB" ] ||
    { echo "ABORT: corpus page $m_page KiB != $PAGE_KB KiB"; exit 1; }
  [ "$m_nk" = "$NK" ] ||
    { echo "ABORT: corpus objects/submit $m_nk != $NK"; exit 1; }
  local have; have=$(count_objects "$PREFIX")
  [ "$have" -eq "$CORPUS_OBJECTS" ] ||
    { echo "ABORT: corpus '$PREFIX' has $have objects, need exactly $CORPUS_OBJECTS"
      echo "       refusing to modify it; build a new corpus under a new PREFIX"; exit 1; }
  echo "    corpus $PREFIX: $have objects, manifest validated against $PROFILE_SHA"
}

# Bytes-per-op must be the value calibrated AT THIS GEOMETRY. The 28 MiB
# constant (52428) is 13.5% wrong for 144 KiB reads, so a mismatched scope is
# an abort rather than a fallback. This is the same scope-matching reader
# run_model_geometry.sh uses, against the same multi-record calibration file:
# two independent brackets must agree within 2% before a constant is quoted.
read_seg() {
  "$PY" - "$CALIB" "$PROFILE_SHA" "$PAGE_KB" "$NK" "$PREFIX" <<'PYEOF' || return 1
import sys
path, want_sha, want_kb, want_ops, want_prefix = sys.argv[1:6]

blocks, cur = [], {}
try:
    handle = open(path)
except OSError as e:
    sys.stderr.write(f"ABORT: cannot read calibration record: {e}\n")
    sys.exit(1)
with handle:
    for line in handle:
        line = line.strip()
        if line.startswith("# ---"):
            if cur:
                blocks.append(cur)
            cur = {}
        elif "=" in line:
            k, v = line.split("=", 1)
            cur[k] = v
if cur:
    blocks.append(cur)

reads = [b for b in blocks if b.get("direction") == "read"]
scoped = [b for b in reads
          if b.get("profile_sha256") == want_sha
          and b.get("page_kb") == want_kb
          and b.get("objects_per_submit") == want_ops
          and b.get("key_prefix") == want_prefix]
if len(scoped) < 2:
    sys.stderr.write(
        f"ABORT: need >=2 read calibrations scoped to profile {want_sha[:12]}, "
        f"page {want_kb} KiB, {want_ops} objects/submit, prefix {want_prefix}; "
        f"found {len(scoped)}. Run run_model_geometry.sh 'calibr' first.\n")
    sys.exit(1)

vals = [float(b["read_segment_bytes"]) for b in scoped]
lo, hi = min(vals), max(vals)
if (hi - lo) / lo > 0.02:
    sys.stderr.write(
        f"ABORT: in-scope read calibrations disagree by {(hi-lo)/lo:.2%} "
        f"({lo:.1f} vs {hi:.1f} B/op).\n")
    sys.exit(1)
# Longest bracket is recorded last, so it carries the smallest relative lag.
print(f"{vals[-1]:.1f}")
PYEOF
}

# Same contract as read_seg, minus the key-prefix scope. The two write brackets
# are recorded under DIFFERENT prefixes by construction: the short one runs on a
# throwaway prefix before the corpus exists (so the constant is known before
# committing 40 GiB), and the long one IS the prepop bracket under $PREFIX. A
# mixed cell's writes go to yet another prefix. Bytes-per-op is a function of the
# page geometry and the transport, not of the namespace, so prefix is dropped
# from the scope here deliberately -- profile SHA, page size, and objects/submit
# are the terms that actually change it.
write_seg() {
  "$PY" - "$CALIB" "$PROFILE_SHA" "$PAGE_KB" "$NK" <<'PYEOF' || return 1
import sys
path, want_sha, want_kb, want_ops = sys.argv[1:5]

blocks, cur = [], {}
try:
    handle = open(path)
except OSError as e:
    sys.stderr.write(f"ABORT: cannot read calibration record: {e}\n")
    sys.exit(1)
with handle:
    for line in handle:
        line = line.strip()
        if line.startswith("# ---"):
            if cur:
                blocks.append(cur)
            cur = {}
        elif "=" in line:
            k, v = line.split("=", 1)
            cur[k] = v
if cur:
    blocks.append(cur)

writes = [b for b in blocks if b.get("direction") == "write"]
scoped = [b for b in writes
          if b.get("profile_sha256") == want_sha
          and b.get("page_kb") == want_kb
          and b.get("objects_per_submit") == want_ops]
if len(scoped) < 2:
    sys.stderr.write(
        f"ABORT: need >=2 write calibrations scoped to profile {want_sha[:12]}, "
        f"page {want_kb} KiB, {want_ops} objects/submit; found {len(scoped)}. "
        "Run run_model_geometry.sh 'calib' and 'prepop' first.\n")
    sys.exit(1)

vals = [float(b["write_segment_bytes"]) for b in scoped]
lo, hi = min(vals), max(vals)
if (hi - lo) / lo > 0.02:
    sys.stderr.write(
        f"ABORT: in-scope write calibrations disagree by {(hi-lo)/lo:.2%} "
        f"({lo:.1f} vs {hi:.1f} B/op).\n")
    sys.exit(1)
print(f"{vals[-1]:.1f}")
PYEOF
}

snap() {
  local dest=$1; : > "$dest"
  for c in InRdmaWrites InRdmaReads RetransSegs "Nak Sequence Error" RTO \
           "RNR received" "Rcvd Out of order packets" InProtoErrors; do
    printf '%s=%s\n' "$c" "$(cat "$H/$c")" >> "$dest"
  done
}

# ------------------------------------------------------------------- one cell
# $1 = initiator count, $2 = read|mixed
cell() {
  local N=$1 CELLMODE=$2
  local WPER
  if [ -n "${WORKERS_PER:-}" ]; then
    WPER=$WORKERS_PER
  else
    [ $(( WORKERS_TOTAL % N )) -eq 0 ] ||
      { echo "ABORT: WORKERS_TOTAL=$WORKERS_TOTAL not divisible by $N initiators"; exit 1; }
    WPER=$(( WORKERS_TOTAL / N ))
  fi
  [ "$WPER" -ge 1 ] || { echo "ABORT: computed workers/initiator is $WPER"; exit 1; }

  local DUR R CELLWARM
  if [ "$CELLMODE" = mixed ]; then
    DUR=$DUR_MIXED; CELLWARM=$WARM_MIXED
  else
    DUR=$DUR_READ; CELLWARM=$WARM
  fi
  [ $(( SUBMITS % IN_FLIGHT )) -eq 0 ] ||
    { echo "ABORT: SUBMITS=$SUBMITS not divisible by in-flight $IN_FLIGHT"; exit 1; }
  R=$(( SUBMITS / IN_FLIGHT ))

  local RUN="${CELLMODE}_n${N}_w${WPER}_inf${IN_FLIGHT}"
  local RUN_DIR="$OUT/$RUN"
  [ ! -e "$RUN_DIR" ] || { echo "ABORT: run dir exists: $RUN_DIR"; exit 1; }

  echo
  echo "--- [cell] $CELLMODE, $N initiator(s), $WPER workers each"
  echo "           (total $(( WPER * N ))), in-flight $IN_FLIGHT, ${DUR}s ---"

  local ADP="{\"type\":\"fs_native\",\"base_path\":\"$B\",\"num_workers\":$WPER,\"use_odirect\":true,\"max_capacity_gb\":$CAP}"
  local -a WPFX=()
  local id
  # Write prefixes must be fresh: a leftover prefix would make monotonic store
  # keys collide with existing objects and turn writes into no-op successes.
  if [ "$CELLMODE" = mixed ]; then
    for ((id = 0; id < N; id++)); do
      WPFX[$id]="w$(date +%s)r${RUN//_/}i${id}"
      [ "$(count_objects "${WPFX[$id]}")" -eq 0 ] ||
        { echo "ABORT: write prefix ${WPFX[$id]} is not fresh"; exit 1; }
    done
  fi

  mkdir -p "$RUN_DIR"
  local POLLF="$RUN_DIR/rdma-poll.txt"; : > "$POLLF"
  # Caches are deliberately NOT dropped: O_DIRECT already bypasses the data
  # page cache, and a cold start would measure first-touch, not steady state.
  ( while :; do
      echo "$(date +%s.%N) $(cat "$H/InRdmaWrites") $(cat "$H/InRdmaReads")"
      sleep $POLL
    done ) >> "$POLLF" 2>/dev/null &
  local PP=$!
  declare -a pids=() release_pids=()

  cleanup_cell() {
    kill "$PP" 2>/dev/null || true
    for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done
    rm -f "$RUN_DIR"/release-* 2>/dev/null || true
  }
  trap cleanup_cell EXIT INT TERM

  snap "$RUN_DIR/pre-counters.txt"
  local before; before=$(count_objects "$PREFIX")

  for ((id = 0; id < N; id++)); do
    local fifo="$RUN_DIR/release-$id"
    mkfifo "$fifo"
    (
      # Each initiator signals readiness, then blocks on its fifo, so all N
      # processes enter their measured window together rather than staggered by
      # interpreter and adapter startup.
      printf '%s\n' "$(date +%s.%N)" > "$RUN_DIR/initiator-$id.ready"
      IFS= read -r _ < "$fifo" || exit 1
      # Plain assignment, not `local`: this is a subshell, not a function body.
      MODEARGS=()
      if [ "$CELLMODE" = mixed ]; then
        # --no-skip-verify is what enables the post-window write-prefix
        # readback. Without it a mixed cell proves completions, byte counts,
        # and counter correlation but NOT stored payload integrity, so a
        # corrupt write would still be reported as an accepted cell. Mixed is
        # the only sustained mode the CLI allows it in: a pure sustained
        # direction recycles buffers and has no stable source/destination pair.
        MODEARGS=(--read-write-ratio "$RATIO" --write-key-prefix "${WPFX[$id]}"
                  --no-skip-verify)
      else
        MODEARGS=(--only load)
      fi
      PYTHONUNBUFFERED=1 "${LM[@]}" bench l2 --l2-adapter "$ADP" \
        --kvcache-shape-profile "$PROFILE" \
        --key-prefix "$PREFIX" "${MODEARGS[@]}" \
        --in-flight "$IN_FLIGHT" --l1-align-bytes 4096 \
        --warmup-rounds 0 --rounds "$R" \
        --duration-sec "$DUR" --warmup-sec "$CELLWARM" \
        --serve-metrics "$(( METRICS_BASE_PORT + id ))" \
        --metrics-bind-address 127.0.0.1 \
        --output "$RUN_DIR/initiator-$id.json" --format json 2>&1 \
        | "$PY" -u -c 'import sys,time
for line in sys.stdin: sys.stdout.write("%.3f %s" % (time.time(), line))' \
        > "$RUN_DIR/initiator-$id.log"
      rc=${PIPESTATUS[0]}
      echo "$rc" > "$RUN_DIR/initiator-$id.status"
      exit "$rc"
    ) &
    pids[$id]=$!
  done

  local deadline=$(( SECONDS + 120 ))
  while [ "$(find "$RUN_DIR" -name 'initiator-*.ready' | wc -l)" -ne "$N" ]; do
    [ "$SECONDS" -lt "$deadline" ] ||
      { echo "ABORT: initiators missed the barrier"; exit 1; }
    sleep 0.05
  done
  for ((id = 0; id < N; id++)); do
    printf 'go\n' > "$RUN_DIR/release-$id" &
    release_pids[$id]=$!
  done
  for p in "${release_pids[@]}"; do wait "$p"; done

  local worker_failure=0
  for p in "${pids[@]}"; do wait "$p" || worker_failure=1; done
  sleep $(( SETTLE + 3 )); kill "$PP" 2>/dev/null || true
  trap - EXIT INT TERM

  local -a CARGS=()
  local c bfr aft
  for c in InRdmaWrites InRdmaReads RetransSegs "Nak Sequence Error" RTO \
           "RNR received" "Rcvd Out of order packets" InProtoErrors; do
    bfr=$(grep "^$c=" "$RUN_DIR/pre-counters.txt" | cut -d= -f2)
    aft=$(cat "$H/$c")
    printf '%s=%s\n' "$c" "$(( aft - bfr ))" >> "$RUN_DIR/delta-counters.txt"
    CARGS+=(--counter "$c=$(( aft - bfr ))")
  done
  local after; after=$(count_objects "$PREFIX")
  local SEG; SEG=$(read_seg) || exit 1
  # Only a mixed cell drives the write direction, so only it needs the write
  # constant. Passing none would silently take the reporter's 4096.0 default,
  # which is 0.30% off the value calibrated at this geometry (4083.9) -- small,
  # but it would mean quoting an uncalibrated constant in the report.
  local -a WSEGARG=()
  if [ "$CELLMODE" = mixed ]; then
    local WSEG; WSEG=$(write_seg) || exit 1
    WSEGARG=(--write-seg-bytes "$WSEG")
  fi

  "$PY" "$SD/geom_multi_report.py" \
    --run-dir "$RUN_DIR" --output "$RUN_DIR/aggregate.json" \
    --initiators "$N" --mode "$CELLMODE" --page-kb "$PAGE_KB" \
    --poll "$POLLF" --read-seg-bytes "$SEG" "${WSEGARG[@]}" \
    --expected-ratio "${RATIO%%:*}" \
    --expected-profile-sha256 "$PROFILE_SHA" \
    --expected-page-size-bytes "$PAGE_B" \
    --expected-objects-per-submit "$NK" \
    --trim-sec "$TRIM" --max-start-skew-sec "$MAX_SKEW" \
    --corpus-before "$before" --corpus-after "$after" "${CARGS[@]}"
  local grc=$?

  # Reclaim this cell's own writes so later cells read through a comparable
  # directory. Guarded three ways: mixed only, prefix generated in this run,
  # and never the shared read prefix.
  if [ "$CELLMODE" = mixed ] && [ "$KEEP_WRITES" != 1 ]; then
    local p n_del
    for ((id = 0; id < N; id++)); do
      p=${WPFX[$id]}
      [ -n "$p" ] && [ "$p" != "$PREFIX" ] || { echo "ABORT: refusing to delete '$p'"; exit 1; }
      n_del=$(count_objects "$p")
      find "$B" -name "$p-bench-model@*.data" -delete
      echo "    reclaimed $n_del objects from write prefix $p"
    done
  fi

  [ "$worker_failure" -eq 0 ] ||
    { echo ">>> CELL REJECTED — initiator process failure <<<"; exit 1; }
  [ $grc -eq 0 ] || { echo ">>> CELL REJECTED — STOPPING SWEEP <<<"; exit 1; }
}

MODE=${1:-all}
case "$MODE" in
  read)   require_corpus; for n in $SWEEP_N; do cell "$n" read; done ;;
  mixed)  require_corpus; for n in $SWEEP_N; do cell "$n" mixed; done ;;
  one-read)  require_corpus; cell "${INITIATORS:-2}" read ;;
  one-mixed) require_corpus; cell "${INITIATORS:-2}" mixed ;;
  all)    require_corpus
          for n in $SWEEP_N; do cell "$n" read; done
          for n in $SWEEP_N; do cell "$n" mixed; done ;;
  *) echo "usage: $0 {read|mixed|one-read|one-mixed|all}"; exit 2 ;;
esac
echo
echo "=== done: mode $MODE, artifacts in $OUT/ ==="
