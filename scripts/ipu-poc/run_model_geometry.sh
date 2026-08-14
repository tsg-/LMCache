#!/bin/bash
# DeepSeek-V3 MODEL-PAGE geometry sweep — 61 x 144 KiB objects per retrieval.
#
# Falcon-offloaded kernel NVMe-oF, existing-controller, single-process fs_native
# sustained load. The MEV IPU provides the Falcon/irdma transport beneath kernel
# nvme_rdma / nvmet_rdma, so this IS a Falcon-offloaded measurement — but it
# does NOT quantify offload benefit: there is no unoffloaded control. NOT 64-QP,
# NOT R2, NOT physical multi-initiator, NOT 400 GbE. --in-flight is user-space
# submission concurrency; it does not create QPs, and nothing here validates
# fresh-QP scale.
#
# READ-ONLY SCOPE. Every timed cell is 100% read (--only load). The only writes
# this driver performs are the one-time corpus prepopulation, the short write
# calibration, and the integrity gate — all under their own fresh prefixes. It
# runs no mixed-ratio cell; --read-write-ratio is deliberately not used.
#
# WHAT THIS IS, AND IS NOT, RELATIVE TO run_payload_sweep.sh
# ----------------------------------------------------------
# run_payload_sweep.sh is the historical 28 MiB driver and is NOT touched by
# this file. It answers "can this storage path saturate at large I/O sizes?"
# and its corpus deliberately exceeds DRAM (303 GiB vs 251 GiB).
#
# This driver answers a different question: "how does the path behave at the
# real model page geometry?" A 256-token DeepSeek-V3 retrieval is 61 objects of
# 147456 bytes, not one 28 MiB object. It is an O_DIRECT model-page geometry
# test over a RE-READ corpus:
#   - The corpus is ~40 GiB, well under the 251 GiB DRAM, so it does NOT
#     reproduce the earlier >DRAM large-object saturation methodology.
#   - O_DIRECT (use_odirect: true) is what removes the data-page-cache
#     concern, not corpus size. md0 and XFS are on the INITIATOR; nvmet-rdma
#     exports raw block devices on the target and operates directly on them, so
#     the initiator-side XFS page cache that O_DIRECT bypasses is the only host
#     page cache in the path. Caches are NOT dropped between cells: that would
#     make every cell artificially cold and confuse the steady-state question
#     this driver exists to answer. A full-corpus direct-read calibration runs
#     before the first timed cell, so no timed result sits immediately after
#     prepopulation. SSD/controller on-device cache remains a caveat, but it is
#     far smaller than the corpus and is not a reason to drop caches.
#   - At ~96 Gbps a 120 s window re-reads the 40 GiB corpus roughly 33 times.
#     Report that caveat with every number from this driver.
#
# HARD CONSTRAINTS honoured here:
#   - No perftest, no new NVMe controller, no NVMe-oF reconnect, no queue-count
#     change, no fresh RC QP. Uses ONLY the already-established controllers.
#   - Never deletes an existing corpus. Every prefix used here is fresh.
#   - max_capacity_gb far exceeds anything written, so eviction stays inert and
#     the pre-existing 28 MiB corpora cannot be evicted.
#
# Usage: bash run_model_geometry.sh [gate|calib|prepop|calibr|sweep|wsweep|all]
set -uo pipefail

H=/sys/class/infiniband/rocep69s0f0/ports/1/hw_counters
# The geometry flags exist only on the bench-geometry worktree; the venv
# interpreter is shared, the code is not.
GEOM=/root/lmcache-geom
PY=/root/lmcache-stage2/.venv/bin/python
LM=("$PY" -m lmcache.cli.main)
B=/mnt/lmcache-stage2/kvcache
# Overridable so two runs can be kept side by side. The default is also what
# run_geom_multi.sh reads as GEOM_OUT for the corpus manifest and calibration,
# so a non-default OUT here must be passed to that driver as GEOM_OUT.
OUT=${OUT:-/root/mkp1-geom}
SD=$(dirname "$0")
PROFILE=${PROFILE:-$SD/models/deepseek_v3_fp8.yaml}
[ -f "$PROFILE" ] || { echo "ABORT: profile not found: $PROFILE"; exit 1; }
PROFILE_SHA=$(sha256sum "$PROFILE" | cut -d' ' -f1)

export PYTHONPATH=$GEOM

PREFIX=${PREFIX:-dsgeom144}
# fs_native fans one submit out into min(num_workers, num_keys) tiles, each
# handled by one worker thread that opens, reads, and closes its files
# serially. So num_workers -- not --in-flight -- is what caps concurrent disk
# reads. At 28 MiB objects 16 workers was ample queue depth; at 144 KiB it is
# only ~2.3 MiB of outstanding I/O, so this is the binding constraint here.
W=${W:-16}
# Optional NUMA binding for the load cell, as a single-variable falsifier for
# "the pool collapses past 32 workers because it spills across sockets". Empty
# means run exactly as every prior cell did -- unpinned. Set e.g. BIND=0 to
# confine the process and its memory to node 0, which is where the NIC lives.
BIND=${BIND:-}
# Sample per-thread CPU placement during the cell. Off by default so the
# measured cells stay byte-identical to the ones already reported.
NUMA_SAMPLE=${NUMA_SAMPLE:-0}
DUR=${DUR:-120}; WARM=${WARM:-10}
SETTLE=2; POLL=0.25; TRIM=3
CAP=4000
METRICS_PORT=${METRICS_PORT:-9101}
METRICS_BIND_ADDRESS=${METRICS_BIND_ADDRESS:-127.0.0.1}
MIN_FREE_GB=${MIN_FREE_GB:-600}

# Submits in the corpus. Divisible by every in-flight value swept below
# (1,2,4,8) and by the prepop in-flight (64), so each cell wraps the corpus a
# whole number of times instead of stopping mid-lap.
SUBMITS=${SUBMITS:-4800}
SWEEP_INF=${SWEEP_INF:-"1 2 4 8"}

ADP="{\"type\":\"fs_native\",\"base_path\":\"$B\",\"num_workers\":$W,\"use_odirect\":true,\"max_capacity_gb\":$CAP}"
mkdir -p "$OUT"

# ------------------------------------------------------- geometry from profile
# Never hardcode 61 x 144: the whole point of this run is that the resolver
# derived them. Read them back from the same code path the bench will use, so a
# profile edit cannot silently desynchronise the corpus arithmetic from the run.
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
[ "$NK" -gt 0 ] && [ "$PAGE_B" -gt 0 ] ||
  { echo "ABORT: resolver returned a non-positive geometry"; exit 1; }
# Every cell divides SUBMITS by its in-flight value and expects a whole number
# of laps; a remainder would leave the last lap partial and the key universe
# misaligned with the corpus.
for i in $SWEEP_INF 64; do
  [ $(( SUBMITS % i )) -eq 0 ] ||
    { echo "ABORT: SUBMITS=$SUBMITS is not divisible by in-flight $i"; exit 1; }
done

NFILES=$(( SUBMITS * NK ))
GIB=$("$PY" -c "print(f'{$NFILES*$PAGE_B/2**30:.1f}')")
SUBMIT_MIB=$("$PY" -c "print(f'{$NK*$PAGE_B/2**20:.2f}')")

echo "############################################################"
echo "# model            : $MODEL  ($TOK tokens/chunk)"
echo "# profile          : $PROFILE"
echo "# profile sha256   : $PROFILE_SHA"
echo "# page             : $PAGE_KB KiB ($PAGE_B B) x $NK objects/submit"
echo "# submit burst      : $SUBMIT_MIB MiB"
echo "# corpus           : $SUBMITS submits = $NFILES objects = $GIB GiB"
echo "# prefix           : $PREFIX"
echo "# O_DIRECT model-page geometry test over a RE-READ corpus."
echo "# 40 GiB < 251 GiB DRAM, so this is NOT the >DRAM methodology."
echo "############################################################"

for flag in --kvcache-shape-profile --key-prefix --duration-sec --warmup-rounds \
            --serve-metrics --metrics-bind-address; do
  if ! "${LM[@]}" bench l2 --help 2>&1 | grep -q -- "$flag"; then
    echo "ABORT: 'bench l2' from $GEOM does not accept $flag."
    exit 1
  fi
done
echo "preflight: bench l2 accepts the geometry, sustained, and metrics options"

# 291k objects at 144 KiB is an inode-count question, not only a byte-count
# question, and df -BG will happily report terabytes free on a full-inode fs.
free_gb=$(df -BG --output=avail /mnt/lmcache-stage2 | tail -1 | tr -dc 0-9)
# --output=iavail already selects inode counts; combining it with -i is an
# error on this coreutils.
free_inodes=$(df --output=iavail /mnt/lmcache-stage2 | tail -1 | tr -dc 0-9)
need_inodes=$(( NFILES * 2 ))   # corpus + calibration/mixed prefixes, w/ slack
echo "preflight: ${free_gb}G free, ${free_inodes} free inodes (need ~${need_inodes})"
[ "$free_gb" -ge "$MIN_FREE_GB" ] || { echo "ABORT: free capacity below ${MIN_FREE_GB}G"; exit 1; }
[ "$free_inodes" -ge "$need_inodes" ] || { echo "ABORT: insufficient free inodes"; exit 1; }

count_objects() { find "$B" -name "${1}-bench-model@*.data" | wc -l; }

# ------------------------------------------------------------------- counters
declare -A PRE
snap_pre() {
  for c in InRdmaWrites InRdmaReads RetransSegs "Nak Sequence Error" RTO \
           "RNR received" "Rcvd Out of order packets" InProtoErrors; do
    PRE[$c]=$(cat "$H/$c")
  done
}
delta() { echo $(( $(cat "$H/$1") - ${PRE[$1]} )); }

# ---------------------------------------------------------------- integrity
gate() {
  local G="gate144$(date +%s)"
  echo "--- [gate] combined store+load --no-skip-verify at $NK x $PAGE_KB KiB ---"
  PYTHONUNBUFFERED=1 "${LM[@]}" bench l2 --l2-adapter "$ADP" \
    --kvcache-shape-profile "$PROFILE" \
    --key-prefix "$G" --in-flight 4 \
    --l1-align-bytes 4096 --warmup-rounds 0 --rounds 4 --no-skip-verify \
    > "$OUT/${G}.log" 2>&1
  local rc=$?
  grep -iE "verif|All .* keys|Keys / round" "$OUT/${G}.log" | head -5
  if [ $rc -ne 0 ]; then echo "GATE FAILED rc=$rc — STOPPING"; tail -20 "$OUT/${G}.log"; exit 1; fi
  echo "  [gate] PASSED"
}

# ------------------------------------------------------------ corpus readback
# The integrity gate above verifies a THROWAWAY gate144* namespace, not the
# corpus the timed cells actually read. Prepopulation and every timed cell use
# --only, which the CLI cannot byte-verify: a corrupt payload in the real corpus
# would still produce completion bitmaps and counter agreement, and so an
# ACCEPTED cell. This readback closes that hole by re-deriving the deterministic
# fill pattern the store pass wrote and comparing it byte-for-byte.
#
# Bounded on purpose: it reads the first and last submit slots plus a middle
# one, not all 4800. Verifying the whole 40 GiB corpus every time would cost a
# full read pass per invocation; three slots at the corpus edges and centre
# catch a truncated, mis-offset, or wrong-geometry store, which are the failure
# modes --only can hide. It is NOT a claim that every object was verified.
readback() {
  local WHEN=$1
  echo "--- [readback $WHEN] byte-verifying corpus slots under '$PREFIX' ---"
  "$PY" "$SD/geom_readback.py" \
    --base-path "$B" --key-prefix "$PREFIX" --profile "$PROFILE" \
    --submits "$SUBMITS" --slots 0,mid,last \
    | tee -a "$OUT/readback-${WHEN}.txt"
  local rc=${PIPESTATUS[0]}
  if [ "$rc" -ne 0 ]; then
    echo ">>> CORPUS READBACK FAILED ($WHEN) — STOPPING <<<"
    exit 1
  fi
}

# ------------------------------------------------------------- calibration
# The existing gate pins SEG = 52428 bytes per InRdmaWrite op, calibrated
# against 28 MiB objects. At 147456-byte objects a 0.2-op rounding residue per
# object is 7% of that object's ops, so the constant must be re-established
# here rather than inherited. Two lengths per direction: if the derived
# bytes-per-op agrees between a short and a long run, counter refresh lag is
# negligible at this geometry; if it drifts, the drift is the finding and no
# single constant may be quoted.
#
# Rounds mode, --warmup-rounds 0: application bytes are then exactly
# successes x page bytes with no discarded warmup traffic inside the bracket,
# so the whole-process delta is usable without the interior estimator (which
# needs a sustained-window marker that rounds mode never prints).
calib_cell() {
  local DIR=$1 INF=$2 R=$3 TAG=$4 KEYPREFIX=$5 COUNTER=$6
  local RUN="cal_${DIR}_${TAG}"
  local subs=$(( R * INF ))
  local bytes=$(( subs * NK * PAGE_B ))
  echo "--- [calib $DIR] $TAG: in_flight=$INF rounds=$R => $subs submits, $(( bytes / 2**20 )) MiB ---"

  snap_pre
  PYTHONUNBUFFERED=1 "${LM[@]}" bench l2 --l2-adapter "$ADP" --only "$DIR" \
    --kvcache-shape-profile "$PROFILE" \
    --key-prefix "$KEYPREFIX" --in-flight "$INF" \
    --l1-align-bytes 4096 --warmup-rounds 0 --rounds "$R" \
    --output "$OUT/${RUN}.json" --format json \
    > "$OUT/${RUN}.log" 2>&1
  local rc=$?
  [ $rc -ne 0 ] && { echo "CALIB FAILED rc=$rc"; tail -20 "$OUT/${RUN}.log"; exit 1; }

  local d_c; d_c=$(delta "$COUNTER")
  for c in RetransSegs "Nak Sequence Error" RTO "RNR received" \
           "Rcvd Out of order packets" InProtoErrors; do
    local dv; dv=$(delta "$c")
    [ "$dv" -ne 0 ] && { echo "ABORT: $c advanced by $dv during calibration"; exit 1; }
  done

  "$PY" "$SD/geom_calib.py" "$OUT/${RUN}.json" "$d_c" "$PAGE_KB" "$COUNTER" "$RUN" \
    "$PROFILE_SHA" "$NK" "$KEYPREFIX" >> "$OUT/calibration.txt" || exit 1
  tail -1 "$OUT/calibration.txt"
}

# Emit the read constant only if the short and long read calibrations agree AND
# both were measured at THIS profile SHA, page size, objects/submit, and read
# prefix. A single calibration cannot distinguish a real bytes-per-op ratio from
# counter-refresh lag that happens to be a fixed fraction of a short bracket;
# two lengths can. Disagreement is a finding, not something to average away.
# Scope matching matters just as much: 52428 B/op was correct at 28 MiB and is
# 13.5% wrong here, so an out-of-scope constant silently biases every cell.
read_seg() {
  "$PY" - "$OUT/calibration.txt" "$PROFILE_SHA" "$PAGE_KB" "$NK" "$PREFIX" <<'PYEOF' || return 1
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
        f"found {len(scoped)} (of {len(reads)} read calibrations on record). "
        "Run the 'calibr' mode first.\n")
    sys.exit(1)

vals = [float(b["read_segment_bytes"]) for b in scoped]
lo, hi = min(vals), max(vals)
if (hi - lo) / lo > 0.02:
    sys.stderr.write(
        f"ABORT: in-scope read calibrations disagree by {(hi-lo)/lo:.2%} "
        f"({lo:.1f} vs {hi:.1f} B/op). Counter refresh lag is not negligible "
        "at this geometry; no single constant may be quoted.\n")
    sys.exit(1)
# Longest bracket is recorded last, so it carries the smallest relative lag.
print(f"{vals[-1]:.1f}")
PYEOF
}

# ---------------------------------------------------------------- prepopulate
MANIFEST="$OUT/${PREFIX}_corpus.manifest"

verify_manifest() {
  [ -f "$MANIFEST" ] || { echo "  no manifest at $MANIFEST"; return 1; }
  local m_kb m_n m_nk m_sha m_sub
  m_kb=$(grep '^data_size_kb=' "$MANIFEST" | cut -d= -f2)
  m_n=$(grep '^nfiles=' "$MANIFEST" | cut -d= -f2)
  m_nk=$(grep '^num_keys=' "$MANIFEST" | cut -d= -f2)
  m_sub=$(grep '^submits=' "$MANIFEST" | cut -d= -f2)
  m_sha=$(grep '^profile_sha256=' "$MANIFEST" | cut -d= -f2)
  [ "$m_kb" = "$PAGE_KB" ] || { echo "  manifest page mismatch: $m_kb != $PAGE_KB"; return 1; }
  [ "$m_n" = "$NFILES" ]   || { echo "  manifest count mismatch: $m_n != $NFILES"; return 1; }
  [ "$m_nk" = "$NK" ]      || { echo "  manifest objects/submit mismatch: $m_nk != $NK"; return 1; }
  [ "$m_sub" = "$SUBMITS" ] || { echo "  manifest submits mismatch: $m_sub != $SUBMITS"; return 1; }
  # Recorded but previously unvalidated. Two profiles can agree on page size and
  # layer count while differing elsewhere, so a same-sized edit would otherwise
  # let an older corpus be reused and reported under the new profile's SHA.
  [ "$m_sha" = "$PROFILE_SHA" ] ||
    { echo "  manifest profile SHA mismatch: ${m_sha:-<absent>} != $PROFILE_SHA"; return 1; }
  local f sz
  f=$(find "$B" -name "${PREFIX}-bench-model@*.data" | head -1)
  [ -n "$f" ] || { echo "  manifest present but no objects found"; return 1; }
  sz=$(stat -c %s "$f")
  [ "$sz" = "$PAGE_B" ] || { echo "  object size $sz != $PAGE_B"; return 1; }
  return 0
}

# Every read-consuming mode must pass this. Previously calibr and sweep read the
# corpus with no manifest check at all, so a corpus of the right size but the
# wrong provenance would have been measured and reported as this profile's.
require_corpus() {
  local have; have=$(count_objects "$PREFIX")
  if [ "$have" -ne "$NFILES" ]; then
    echo "ABORT: corpus under '$PREFIX' has $have objects, need exactly $NFILES."
    echo "       Run '$0 prepop' first, or use a new PREFIX."
    exit 1
  fi
  if ! verify_manifest; then
    echo "ABORT: corpus under '$PREFIX' does not match this geometry/profile."
    echo "       Refusing to measure an unverified corpus, and refusing to"
    echo "       delete it. Use a new PREFIX."
    exit 1
  fi
  echo "corpus: $have objects, manifest validated against profile $PROFILE_SHA"
}

prepop() {
  local have; have=$(count_objects "$PREFIX")
  if [ "$have" -ge "$NFILES" ] && verify_manifest; then
    echo "--- [prepop] $have objects + validated manifest, reusing (read-only) ---"
    return
  fi
  if [ "$have" -ge "$NFILES" ]; then
    echo "ABORT: $have objects exist under '$PREFIX' but the manifest is missing"
    echo "       or does not match this geometry. Refusing to reuse an"
    echo "       unverified corpus, and refusing to delete it. Use a new PREFIX."
    exit 1
  fi
  if [ "$have" -gt 0 ]; then
    echo "ABORT: partial corpus ($have/$NFILES) under '$PREFIX' with no valid"
    echo "       manifest. A resumed store would not reproduce one geometry."
    echo "       Use a new PREFIX; this script never deletes existing data."
    exit 1
  fi

  local R=$(( SUBMITS / 64 ))
  echo "--- [prepop] writing $NFILES x $PAGE_KB KiB ($GIB GiB), in_flight=64 rounds=$R ---"
  echo "    WRITE WEAR: one-time $GIB GiB; every later load cell is read-only."
  echo "    This bracket doubles as the long write calibration."

  snap_pre
  PYTHONUNBUFFERED=1 "${LM[@]}" bench l2 --l2-adapter "$ADP" --only store \
    --kvcache-shape-profile "$PROFILE" \
    --key-prefix "$PREFIX" --in-flight 64 \
    --l1-align-bytes 4096 --warmup-rounds 0 --rounds "$R" \
    --output "$OUT/${PREFIX}_prepop.json" --format json \
    > "$OUT/${PREFIX}_prepop.log" 2>&1
  local rc=$?
  [ $rc -ne 0 ] && { echo "PREPOP FAILED rc=$rc — STOPPING"; tail -20 "$OUT/${PREFIX}_prepop.log"; exit 1; }

  # Same clean-fabric requirement the calibration cells and timed cells apply:
  # a constant derived across retransmits or NAKs is not a clean measurement.
  for c in RetransSegs "Nak Sequence Error" RTO "RNR received" \
           "Rcvd Out of order packets" InProtoErrors; do
    local dv; dv=$(delta "$c")
    [ "$dv" -ne 0 ] && { echo "ABORT: $c advanced by $dv during prepopulation"; exit 1; }
  done

  local d_c; d_c=$(delta InRdmaReads)
  "$PY" "$SD/geom_calib.py" "$OUT/${PREFIX}_prepop.json" "$d_c" "$PAGE_KB" \
    InRdmaReads "cal_store_long_prepop" "$PROFILE_SHA" "$NK" "$PREFIX" \
    >> "$OUT/calibration.txt" || exit 1
  tail -1 "$OUT/calibration.txt"

  have=$(count_objects "$PREFIX")
  echo "  [prepop] $have objects"
  [ "$have" -lt "$NFILES" ] && { echo "ABORT: corpus short ($have < $NFILES)"; exit 1; }

  {
    echo "prefix=$PREFIX"
    echo "model=$MODEL"
    echo "profile=$PROFILE"
    echo "profile_sha256=$(sha256sum "$PROFILE" | cut -d' ' -f1)"
    echo "tokens_per_chunk=$TOK"
    echo "data_size_kb=$PAGE_KB"
    echo "object_bytes=$PAGE_B"
    echo "num_keys=$NK"
    echo "submits=$SUBMITS"
    echo "nfiles=$NFILES"
    echo "store_in_flight=64"
    echo "store_rounds=$R"
    echo "warmup_rounds=0"
    echo "commit=$(cd "$GEOM" && git rev-parse --short HEAD 2>/dev/null)"
  } > "$MANIFEST"
  echo "  [prepop] manifest: $MANIFEST"
}

# ---------------------------------------------------------------- one load cell
cell() {
  local INF=$1 TAG=$2
  local R=$(( SUBMITS / INF ))   # exact wrap over the whole corpus
  local RUN="${PREFIX}_inf${INF}${TAG}"
  echo "--- [cell] in_flight=$INF rounds=$R (key universe = $SUBMITS submits) ---"

  # Unpinned by default, so a BIND-less cell is the same command line as the
  # cells already reported. numactl must exist before it is claimed to have
  # bound anything -- silently running unpinned would make the falsifier lie.
  local WRAP=()
  if [ -n "$BIND" ]; then
    command -v numactl >/dev/null || { echo "ABORT: BIND=$BIND but numactl not found"; exit 1; }
    WRAP=(numactl --cpunodebind="$BIND" --membind="$BIND")
    RUN="${RUN}_bind${BIND}"
    echo "    NUMA bind: cpunodebind=$BIND membind=$BIND"
  fi

  # Caches are deliberately NOT dropped: O_DIRECT already bypasses the data
  # page cache, and a cold start every cell would measure first-touch, not
  # steady state.
  local POLLF="$OUT/${RUN}_poll.txt"; : > "$POLLF"
  ( while :; do echo "$(date +%s.%N) $(cat $H/InRdmaWrites)"; sleep $POLL; done ) >> "$POLLF" 2>/dev/null &
  local PP=$!

  snap_pre
  local before; before=$(count_objects "$PREFIX")

  # numastat before/after brackets the cell's own allocation and hit/miss
  # behaviour, which is the memory-side half of the placement question.
  local NSF="$OUT/${RUN}_numastat.txt"
  { echo "=== before ==="; cat /sys/devices/system/node/node*/numastat; } > "$NSF"
  local SAMP=
  if [ "$NUMA_SAMPLE" = 1 ]; then
    # Start sampling after warmup so the distribution reflects the timed
    # window, not thread-pool startup. Match on the module path, which is
    # stable across cells and distinct from this driver's own command line.
    ( sleep $(( WARM + 5 ))
      "$PY" "$SD/numa_placement.py" --name-contains lmcache.cli.main \
        --samples 60 --interval 0.5 ) > "$OUT/${RUN}_placement.txt" 2>&1 &
    SAMP=$!
  fi

  PYTHONUNBUFFERED=1 "${WRAP[@]}" "${LM[@]}" bench l2 --l2-adapter "$ADP" --only load \
    --kvcache-shape-profile "$PROFILE" \
    --key-prefix "$PREFIX" --in-flight "$INF" \
    --l1-align-bytes 4096 --warmup-rounds 0 --rounds "$R" \
    --duration-sec "$DUR" --warmup-sec "$WARM" \
    --serve-metrics "$METRICS_PORT" --metrics-bind-address "$METRICS_BIND_ADDRESS" \
    --output "$OUT/${RUN}_load.json" --format json 2>&1 \
    | "$PY" -u -c 'import sys,time
for l in sys.stdin: sys.stdout.write("%.3f %s" % (time.time(), l))' \
    > "$OUT/${RUN}_load.log"
  local rc=${PIPESTATUS[0]}
  sleep $(( SETTLE + 3 )); kill $PP 2>/dev/null
  [ -n "$SAMP" ] && wait $SAMP 2>/dev/null
  { echo "=== after ==="; cat /sys/devices/system/node/node*/numastat; } >> "$NSF"

  local d_rt d_nak d_rto d_rnr d_oo d_pe
  d_rt=$(delta RetransSegs); d_nak=$(delta "Nak Sequence Error")
  d_rto=$(delta RTO); d_rnr=$(delta "RNR received")
  d_oo=$(delta "Rcvd Out of order packets"); d_pe=$(delta InProtoErrors)
  local after; after=$(count_objects "$PREFIX")

  [ $rc -ne 0 ] && { echo "LOAD FAILED rc=$rc"; tail -15 "$OUT/${RUN}_load.log"; exit 1; }

  local wd; wd=$("$PY" "$SD/interior_rate.py" "$OUT/${RUN}_load.log" "$POLLF" "$OUT/${RUN}_load.json" $TRIM)
  echo "    interior-rate delta: $wd  (whole-process: $(delta InRdmaWrites))"
  # NA must REJECT, never fall back. The whole-process delta is known biased
  # high (it includes the discarded warmup's wire traffic), so substituting it
  # would let a cell pass on a number the method has already rejected.
  if [ "$wd" = "NA" ]; then
    echo "    interior estimator returned NA (no window marker, <8 interior"
    echo "    samples, or degenerate time base) — the cell is unmeasured."
    echo ">>> CELL REJECTED — STOPPING SWEEP <<<"
    exit 1
  fi

  local SEG; SEG=$(read_seg) || exit 1

  "$PY" "$SD/geom_report.py" "$OUT/${RUN}_load.json" \
    "$wd" "$d_rt" "$d_nak" "$d_rto" "$d_rnr" "$d_oo" "$d_pe" \
    "$before" "$after" "$RUN" "$PAGE_KB" "$SEG" "$NK"
  local grc=$?
  [ $grc -ne 0 ] && { echo ">>> CELL REJECTED — STOPPING SWEEP <<<"; exit 1; }
}

MODE=${1:-all}
case "$MODE" in
  gate)   gate ;;
  # Short write calibration first, on a throwaway prefix, so the bytes-per-op
  # constant is established BEFORE committing to a 40 GiB corpus. The long
  # write calibration is the prepop bracket; both read calibrations need the
  # corpus and therefore run after it.
  calib)  calib_cell store 8 20 short "calw144$(date +%s)" InRdmaReads ;;
  prepop) prepop; readback post-prepop ;;
  calibr) require_corpus
          calib_cell load 8 150 short "$PREFIX" InRdmaWrites
          calib_cell load 8 600 long  "$PREFIX" InRdmaWrites ;;
  # Readback brackets the sweep: before, so no cell measures a corrupt corpus;
  # after, so a cell that silently damaged it cannot pass unnoticed.
  sweep)  require_corpus; readback pre-sweep
          for i in $SWEEP_INF; do cell "$i" ""; done
          readback post-sweep ;;
  # Worker sweep: hold --in-flight at its saturating value and vary the tile
  # worker pool, which is the actual disk-concurrency knob at this page size.
  # Each W needs its own invocation because num_workers is baked into the
  # adapter JSON at construction.
  wsweep) require_corpus; cell "${WSWEEP_INF:-8}" "_w${W}" ;;
  all)    gate
          calib_cell store 8 20 short "calw144$(date +%s)" InRdmaReads
          prepop
          readback post-prepop
          require_corpus
          calib_cell load 8 150 short "$PREFIX" InRdmaWrites
          calib_cell load 8 600 long  "$PREFIX" InRdmaWrites
          readback pre-sweep
          for i in $SWEEP_INF; do cell "$i" ""; done
          readback post-sweep ;;
  *) echo "usage: $0 [gate|calib|prepop|calibr|sweep|wsweep|all]"; exit 2 ;;
esac
echo "=== done: mode $MODE, artifacts in $OUT/ ==="
