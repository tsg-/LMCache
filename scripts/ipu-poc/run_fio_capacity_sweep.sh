#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
#
# Matched read-capacity + mixed-ratio FIO sweep for the MkP NVMe-oF path.
#
# Answers one question the existing ladder cannot: the three capacity rungs on
# record were each measured with a DIFFERENT job shape (local 15 s/ramp 2/qd16-256
# on mkp2; remote raw 60 s/ramp 5/qd32 only; remote XFS 60 s/ramp 5/qd16-256), so
# the gaps between them confound surface with runtime and depth. Every cell here
# uses one shape, so a difference between rungs is a difference in the surface.
#
# Surfaces (one per invocation, run ON the host that owns the surface):
#   local_raw   mkp2, raw exported PM9A3 devices. READS ONLY -- see the guard in
#               build_job(). Requires the target quiesced: with the namespaces
#               live, initiator traffic lands on the same media and the "local
#               hardware ceiling" is measuring both loads at once.
#   remote_raw  mkp1, the imported NVMe-oF namespaces, no filesystem.
#   remote_xfs  mkp1, XFS on md0 over both namespaces -- the surface `fs_native`
#               actually uses, so this is the only rung directly comparable to
#               `bench l2`.
#
# Read matrix: BS x QD32 x 3 reps, plus one QD16 cell per BS. Three reps because a
# single 60 s cell cannot distinguish a real 4% gap from run-to-run spread, which
# is roughly the size of the gaps being interpreted. The reps sit at QD32 rather
# than QD16 because QD16 was measured NOT to saturate this path -- see the QD_MAIN
# comment for the depth curve.
#
# Mixed matrix (remote_xfs only): randrw at two ratios, same depth allocation.
# 83% read is the bytes mix matching `bench l2 --read-write-ratio 5:1`
# (5/6 = 83.3%), so it discriminates whether the mixed L2 shortfall is the storage
# path or the adapter. 90% read is a second point on the duplex curve with no L2
# comparator -- storage-path characterisation only, do not chart it against an L2
# bar. Note fio applies the mix per-IO, so the ACHIEVED ratio must be read from
# the artifact, not assumed; the reporter records it (82.9% observed for a
# requested 83%).
#
# NOTE ON SMALL BLOCK SIZES: 4 KiB and 16 KiB cells are latency/IOPS
# measurements, NOT capacity measurements, and quoting them in Gb/s next to a
# 512 KiB rung compares two different limits. Measured 2026-08-11 on remote_xfs:
# 4 KiB reaches 1.97 Gb/s at QD16 and 9.12 at QD32; 16 KiB reaches 18.61 and
# 32.33 -- an order of magnitude under the 95.6 Gb/s large-block ceiling, because
# the limit is per-request cost. They are worth running anyway: they give the
# per-request floor and the IOPS ceiling, and they bound how much of the path's
# capacity a small-page model geometry could ever reach. The reporter tags every
# sub-saturation cell so they cannot be charted as a surface ceiling by accident.
#
# Usage (as root, on the surface's host):
#   ./run_fio_capacity_sweep.sh --surface remote_xfs
#   ./run_fio_capacity_sweep.sh --surface remote_xfs --mixed-only
#   ./run_fio_capacity_sweep.sh --surface local_raw --confirm-quiesced
set -uo pipefail

SURFACE=""
MIXED_ONLY=0
READ_ONLY_MATRIX=0
CONFIRM_QUIESCED=0
DRY_RUN=0

while [ $# -gt 0 ]; do
  case $1 in
    --surface) SURFACE=$2; shift 2 ;;
    --mixed-only) MIXED_ONLY=1; shift ;;
    --read-only) READ_ONLY_MATRIX=1; shift ;;
    --confirm-quiesced) CONFIRM_QUIESCED=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    *) echo "ABORT: unknown argument $1"; exit 2 ;;
  esac
done

BS_LIST=${BS_LIST:-"4k 16k 144k 256k 512k"}
# The 3 reps go to QD32, not QD16, and this is a change from the original plan.
# Measured on remote_xfs at 144 KiB, 2026-08-11: qd8 53.5, qd16 82.6, qd24 93.6,
# qd32 95.6, qd48 95.7, qd64 95.7 Gb/s. QD16 is ~13 Gb/s short of the ceiling and
# its own run-to-run spread is 5.5% (78.85 / 82.70 / 83.20 over three reps) --
# larger than the inter-surface gaps the sweep exists to resolve. Repeating an
# unsaturated depth three times measures the generator, not the surface, so QD32
# carries the reps and QD16 is kept as a single point on the depth curve.
QD_MAIN=${QD_MAIN:-32}
QD_SECOND=${QD_SECOND:-16}
REPS=${REPS:-3}
RUNTIME=${RUNTIME:-60}
RAMP=${RAMP:-10}
MIX_BS=${MIX_BS:-144k}
MIX_RATIOS=${MIX_RATIOS:-"83 90"}
NUMJOBS=1
POLL=${POLL:-0.25}
MIN_FREE_GB=${MIN_FREE_GB:-400}
FIO_FILE_GB=${FIO_FILE_GB:-64}

# The XFS surface gets its own file tree. The `bench l2` corpus lives under
# kvcache/ on the same filesystem and is NOT to be read or written here: a
# randrw cell would corrupt it, and even a read cell would make the corpus
# state ambiguous for the next L2 run.
XFS_MOUNT=${XFS_MOUNT:-/mnt/lmcache-stage2}
XFS_DIR=${XFS_DIR:-"$XFS_MOUNT/fio-sweep"}
CORPUS_DIR="$XFS_MOUNT/kvcache"

RUN=${RUN_ID:-fiosweep$(date +%s)}
OUT=${OUT_BASE:-/root/mkp-fio-sweep}/$RUN
H=/sys/class/infiniband/rocep69s0f0/ports/1/hw_counters
COUNTERS=(InRdmaWrites InRdmaReads RetransSegs "Nak Sequence Error" RTO
  "RNR received" "Rcvd Out of order packets" InProtoErrors)

case $SURFACE in
  local_raw|remote_raw|remote_xfs) ;;
  *) echo "ABORT: --surface must be local_raw, remote_raw, or remote_xfs"; exit 2 ;;
esac
[ "$(id -u)" -eq 0 ] || { echo "ABORT: must run as root"; exit 2; }
command -v fio >/dev/null || { echo "ABORT: fio not found"; exit 2; }
command -v iostat >/dev/null || { echo "ABORT: iostat not found (sysstat)"; exit 2; }
command -v pidstat >/dev/null || { echo "ABORT: pidstat not found (sysstat)"; exit 2; }

# ---- surface -> devices --------------------------------------------------
# Resolved from the live system rather than hardcoded: mkp2's second namespace
# is nvme2n2, not the nvme2n1 the 2026-08-02 baseline runner used, and a stale
# name would silently benchmark the wrong device or fail mid-sweep.
resolve_local_devices() {
  local devs=()
  for d in /sys/block/nvme*n*; do
    local n; n=$(basename "$d")
    [ -e "/sys/block/$n/device/transport" ] || continue
    # Skip the per-controller alias paths (nvme2c2n1) the kernel also exports.
    case $n in *c*n*) continue ;; esac
    [ "$(cat "/sys/block/$n/device/transport" 2>/dev/null)" = "pcie" ] || continue
    grep -qi samsung "/sys/block/$n/device/model" 2>/dev/null || continue
    devs+=("/dev/$n")
  done
  printf '%s\n' "${devs[@]}"
}

resolve_remote_devices() {
  local devs=()
  for d in /sys/block/nvme*n*; do
    local n; n=$(basename "$d")
    case $n in *c*n*) continue ;; esac
    local subsys="/sys/block/$n/device/subsysnqn"
    [ -e "$subsys" ] || continue
    grep -q '^mkp2-nvme' "$subsys" 2>/dev/null && devs+=("/dev/$n")
  done
  printf '%s\n' "${devs[@]}"
}

case $SURFACE in
  local_raw)
    mapfile -t DEVICES < <(resolve_local_devices)
    [ "${#DEVICES[@]}" -eq 2 ] ||
      { echo "ABORT: expected 2 local PM9A3 devices, found ${#DEVICES[@]}: ${DEVICES[*]-}"; exit 1; }
    ;;
  remote_raw)
    mapfile -t DEVICES < <(resolve_remote_devices)
    [ "${#DEVICES[@]}" -eq 2 ] ||
      { echo "ABORT: expected 2 imported namespaces, found ${#DEVICES[@]}: ${DEVICES[*]-}"; exit 1; }
    ;;
  remote_xfs)
    mountpoint -q "$XFS_MOUNT" ||
      { echo "ABORT: $XFS_MOUNT is not mounted"; exit 1; }
    DEVICES=("$XFS_DIR")
    ;;
esac

# ---- surface-specific preflight -----------------------------------------
# The local surface is the one that can destroy data and the one that is
# meaningless if the target is still serving, so it is gated hardest.
if [ "$SURFACE" = local_raw ]; then
  [ "$CONFIRM_QUIESCED" -eq 1 ] ||
    { echo "ABORT: local_raw requires --confirm-quiesced (disconnect initiators first)"; exit 2; }
  if command -v nvme >/dev/null && nvme list-subsys 2>/dev/null | grep -q 'live'; then
    if ls /sys/kernel/config/nvmet/subsystems/*/ >/dev/null 2>&1; then
      :  # a configured target is expected; what matters is no live initiator I/O
    fi
  fi
  # An exported namespace with in-flight I/O means the initiator is still
  # attached: measuring "local capacity" now would measure both loads together.
  for dev in "${DEVICES[@]}"; do
    n=$(basename "$dev")
    inflight=$(cat "/sys/block/$n/inflight" 2>/dev/null | tr -s ' ' | tr -d ' ')
    [ "${inflight:-0}" = "00" ] || [ -z "${inflight:-}" ] ||
      { echo "ABORT: $dev has I/O in flight ($inflight) -- target not quiesced"; exit 1; }
  done
fi

if [ "$SURFACE" = remote_xfs ]; then
  avail=$(df -BG --output=avail "$XFS_MOUNT" | tail -1 | tr -dc 0-9)
  [ "$avail" -ge "$MIN_FREE_GB" ] ||
    { echo "ABORT: free capacity ${avail}G is below ${MIN_FREE_GB}G"; exit 1; }
  # Guard the L2 corpus: the sweep must not be able to name it as a target.
  case "$XFS_DIR" in
    "$CORPUS_DIR"|"$CORPUS_DIR"/*)
      echo "ABORT: sweep directory would overlap the bench l2 corpus"; exit 1 ;;
  esac
  mkdir -p "$XFS_DIR"
fi

mkdir -p "$OUT"/{jobs,fio,iostat,pidstat,counters,smart}
echo "run=$RUN surface=$SURFACE devices=${DEVICES[*]}" | tee "$OUT/context.txt"
{
  echo "host=$(hostname)"
  echo "kernel=$(uname -r)"
  echo "fio=$(fio --version)"
  echo "surface=$SURFACE"
  echo "devices=${DEVICES[*]}"
  echo "bs_list=$BS_LIST"
  echo "qd_main=$QD_MAIN qd_second=$QD_SECOND reps=$REPS"
  echo "runtime=$RUNTIME ramp=$RAMP numjobs=$NUMJOBS"
  echo "mix_bs=$MIX_BS mix_ratios=$MIX_RATIOS"
  echo "date_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >> "$OUT/context.txt"

if [ "$SURFACE" = remote_xfs ]; then
  git -C "$(dirname "$0")" rev-parse --short HEAD 2>/dev/null \
    >> "$OUT/context.txt" || true
fi

# ---- pre-create the XFS working files -----------------------------------
# Must happen BEFORE the first timed cell. fio lays out any missing file inside
# the job it is running, and because the sweep brackets each cell with wall-clock
# timestamps, that layout lands inside the bracket: the first cell of a fresh run
# measured 51 s of elapsed time against a 16 s job, which in turn made the
# counter cross-check read 0.30 instead of ~0.99. The throughput number itself
# survives (fio's ramp/runtime accounting is internal), but every artifact keyed
# to the bracket is wrong, so this is not optional.
if [ "$SURFACE" = remote_xfs ] && [ "$DRY_RUN" -eq 0 ]; then
  layout_needed=0
  for filenum in 0 1; do
    f="$XFS_DIR/fio-sweep.0.$filenum"
    [ -f "$f" ] && [ "$(stat -c %s "$f")" -eq $((FIO_FILE_GB * 1024 * 1024 * 1024 / 2)) ] ||
      layout_needed=1
  done
  if [ "$layout_needed" -eq 1 ]; then
    echo "laying out ${FIO_FILE_GB}G of working files (once per run dir)..."
    layout_job="$OUT/jobs/_layout.fio"
    {
      echo "[global]"
      echo "ioengine=libaio"
      echo "direct=1"
      echo "rw=write"
      echo "bs=1m"
      echo "iodepth=32"
      echo "directory=$XFS_DIR"
      echo "size=${FIO_FILE_GB}G"
      echo "nrfiles=2"
      echo "filename_format=fio-sweep.\$jobnum.\$filenum"
      echo "fallocate=none"
      echo ""
      echo "[layout]"
    } > "$layout_job"
    fio "$layout_job" --output-format=json \
      --output="$OUT/fio/_layout.json" > "$OUT/fio/_layout.stdout" 2>&1 ||
      { echo "ABORT: working-file layout failed"; exit 1; }
  fi
  sync
  echo 3 > /proc/sys/vm/drop_caches
  sleep 2
fi

# ---- SMART snapshot (bracket, not a time series) ------------------------
smart_snapshot() {
  local when=$1
  command -v nvme >/dev/null || return 0
  local targets=()
  case $SURFACE in
    local_raw) targets=("${DEVICES[@]}") ;;
    *) mapfile -t targets < <(resolve_remote_devices) ;;
  esac
  for dev in "${targets[@]}"; do
    nvme smart-log "$dev" > "$OUT/smart/$(basename "$dev")-$when.txt" 2>/dev/null || true
  done
}

# ---- job file generation -------------------------------------------------
# fio is invoked with a job FILE, not a command line, so the artifact under
# jobs/ is byte-for-byte what ran. Repeating filename= inside one job spreads
# that single job across both devices, which is what the 2026-08-02 baseline did
# and is why its 14.28 GB/s figure is an aggregate over two drives.
build_job() {
  local path=$1 name=$2 rw=$3 bs=$4 qd=$5 rwmix=${6:-}

  # Hard guard: raw block surfaces never get a writing pattern. On local_raw
  # those devices hold the exported corpus; a randrw cell would overwrite it.
  case $SURFACE in
    local_raw|remote_raw)
      case $rw in
        *write*|randrw|rw|readwrite)
          echo "ABORT: refusing write pattern '$rw' on raw device surface $SURFACE"
          exit 1 ;;
      esac ;;
  esac

  {
    echo "[global]"
    echo "ioengine=libaio"
    echo "direct=1"
    echo "time_based=1"
    echo "runtime=$RUNTIME"
    echo "ramp_time=$RAMP"
    echo "group_reporting=1"
    echo "numjobs=$NUMJOBS"
    echo "iodepth=$qd"
    echo "bs=$bs"
    echo "rw=$rw"
    [ -n "$rwmix" ] && echo "rwmixread=$rwmix"
    if [ "$SURFACE" = remote_xfs ]; then
      echo "directory=$XFS_DIR"
      echo "size=${FIO_FILE_GB}G"
      # One file per device behind md0 so the stripe is exercised, and
      # pre-created so no cell pays first-write allocation cost.
      echo "nrfiles=2"
      echo "filename_format=fio-sweep.\$jobnum.\$filenum"
      echo "fallocate=none"
    fi
    echo ""
    echo "[$name]"
    if [ "$SURFACE" != remote_xfs ]; then
      for dev in "${DEVICES[@]}"; do echo "filename=$dev"; done
    fi
  } > "$path"
}

# ---- one cell ------------------------------------------------------------
run_cell() {
  local tag=$1 rw=$2 bs=$3 qd=$4 rep=$5 rwmix=${6:-}
  local cell="${tag}_bs${bs}_qd${qd}_rep${rep}"
  local job="$OUT/jobs/$cell.fio"
  local json="$OUT/fio/$cell.json"

  build_job "$job" "$cell" "$rw" "$bs" "$qd" "$rwmix"
  if [ "$DRY_RUN" -eq 1 ]; then echo "  DRY $cell"; return 0; fi

  printf '  %-34s ' "$cell"

  # Counters before. Only meaningful on the remote surfaces; recorded on
  # local_raw too so the file exists and shows ~zero fabric traffic, which is
  # itself the evidence that the target really was quiesced.
  : > "$OUT/counters/$cell.pre"
  for counter in "${COUNTERS[@]}"; do
    printf '%s=%s\n' "$counter" "$(cat "$H/$counter" 2>/dev/null || echo NA)" \
      >> "$OUT/counters/$cell.pre"
  done

  local poll="$OUT/counters/$cell.poll"
  : > "$poll"
  ( while :; do
      echo "$(date +%s.%N) $(cat "$H/InRdmaWrites" 2>/dev/null || echo 0)" \
        "$(cat "$H/InRdmaReads" 2>/dev/null || echo 0)"
      sleep "$POLL"
    done ) >> "$poll" 2>/dev/null &
  local poll_pid=$!

  # iostat covers ramp+measured; the report drops the ramp window by timestamp.
  iostat -x -t -y 1 $((RUNTIME + RAMP + 2)) \
    > "$OUT/iostat/$cell.txt" 2>/dev/null &
  local iostat_pid=$!

  local t_start; t_start=$(date +%s.%N)
  fio "$job" --output-format=json --output="$json" \
    > "$OUT/fio/$cell.stdout" 2>&1 &
  local fio_pid=$!

  # Process CPU for the fio process tree. Host CPU from the scrape is too coarse
  # for a CPU-per-GB claim; this attributes cycles to the benchmark itself.
  # -t is required: fio's submitting work lives in threads, and without it the
  # main PID reports 0.00% for a cell that is actually driving 12 GB/s.
  pidstat -h -u -t -p "$fio_pid" 1 $((RUNTIME + RAMP)) \
    > "$OUT/pidstat/$cell.txt" 2>/dev/null &
  local pidstat_pid=$!

  wait "$fio_pid"; local rc=$?
  local t_end; t_end=$(date +%s.%N)
  kill "$poll_pid" "$iostat_pid" "$pidstat_pid" 2>/dev/null || true
  wait "$iostat_pid" "$pidstat_pid" 2>/dev/null || true

  : > "$OUT/counters/$cell.delta"
  for counter in "${COUNTERS[@]}"; do
    local before after
    before=$(grep -F "$counter=" "$OUT/counters/$cell.pre" | cut -d= -f2)
    after=$(cat "$H/$counter" 2>/dev/null || echo NA)
    if [ "$before" = NA ] || [ "$after" = NA ]; then
      printf '%s=NA\n' "$counter" >> "$OUT/counters/$cell.delta"
    else
      printf '%s=%s\n' "$counter" "$((after - before))" >> "$OUT/counters/$cell.delta"
    fi
  done
  printf 'window_start=%s\nwindow_end=%s\nramp_sec=%s\nruntime_sec=%s\n' \
    "$t_start" "$t_end" "$RAMP" "$RUNTIME" >> "$OUT/counters/$cell.delta"

  if [ "$rc" -ne 0 ]; then echo "FAIL rc=$rc"; return 1; fi

  # Group-reported aggregate bandwidth, as the plan requires -- not a per-job
  # number and not summed by hand.
  python3 - "$json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
j = d["jobs"][0]
r, w = j["read"], j["write"]
gbps = (r["bw_bytes"] + w["bw_bytes"]) * 8 / 1e9
mix = ""
if w["bw_bytes"]:
    mix = f" mix_read={r['bw_bytes']/(r['bw_bytes']+w['bw_bytes'])*100:.2f}%"
devs = ",".join(sorted(u["name"] for u in d.get("disk_util", [])))
print(f"OK {gbps:.2f} Gb/s r={r['iops']:.0f} w={w['iops']:.0f} iops"
      f" rlat_p99={r['clat_ns'].get('percentile',{}).get('99.000000',0)/1e6:.3f}ms"
      f"{mix} devs=[{devs}]")
PY
  return 0
}

# ---- corpus fingerprint (XFS surface only) ------------------------------
corpus_count() {
  [ "$SURFACE" = remote_xfs ] && [ -d "$CORPUS_DIR" ] || { echo 0; return; }
  find "$CORPUS_DIR" -name '*.data' | wc -l
}

corpus_before=$(corpus_count)
smart_snapshot before

failures=0
total=0

if [ "$MIXED_ONLY" -eq 0 ]; then
  echo "=== read matrix: ${BS_LIST// /, } x qd$QD_MAIN x ${REPS} reps + qd$QD_SECOND x 1 ==="
  for bs in $BS_LIST; do
    for ((rep = 1; rep <= REPS; rep++)); do
      total=$((total + 1))
      run_cell "read_${SURFACE}" read "$bs" "$QD_MAIN" "$rep" || failures=$((failures + 1))
    done
    total=$((total + 1))
    run_cell "read_${SURFACE}" read "$bs" "$QD_SECOND" 1 || failures=$((failures + 1))
  done
fi

if [ "$SURFACE" = remote_xfs ] && [ "$READ_ONLY_MATRIX" -eq 0 ]; then
  echo "=== mixed matrix: $MIX_BS randrw, rwmixread in {${MIX_RATIOS// /, }} ==="
  # Reps at the saturated depth only, plus one cell at the second depth, mirroring
  # the read matrix. Three reps at both depths would double the mixed cell count
  # for a depth that is already known not to saturate.
  for ratio in $MIX_RATIOS; do
    for ((rep = 1; rep <= REPS; rep++)); do
      total=$((total + 1))
      run_cell "mixed${ratio}_${SURFACE}" randrw "$MIX_BS" "$QD_MAIN" "$rep" "$ratio" ||
        failures=$((failures + 1))
    done
    total=$((total + 1))
    run_cell "mixed${ratio}_${SURFACE}" randrw "$MIX_BS" "$QD_SECOND" 1 "$ratio" ||
      failures=$((failures + 1))
  done
fi

smart_snapshot after
corpus_after=$(corpus_count)

{
  echo "cells_attempted=$total"
  echo "cells_failed=$failures"
  echo "corpus_before=$corpus_before"
  echo "corpus_after=$corpus_after"
  echo "finished_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} >> "$OUT/context.txt"

echo
echo "artifacts: $OUT"
if [ "$corpus_before" -ne "$corpus_after" ]; then
  echo "RESULT: REJECTED — bench l2 corpus changed ($corpus_before -> $corpus_after)"
  exit 1
fi
[ "$failures" -eq 0 ] || { echo "RESULT: $failures/$total cells FAILED"; exit 1; }
echo "RESULT: $total/$total cells completed"
