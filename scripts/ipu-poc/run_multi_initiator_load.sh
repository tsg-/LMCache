#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Local multi-process fs_native read functional test for the existing MkP path.
#
# This intentionally does NOT create controllers or fresh RC QPs, use an
# LMCache server/MP coordinator, write into a shared namespace, or model 4x400
# hardware. It launches 2 or 4 independent read-only bench processes on mkp1
# against one prepopulated immutable fs_native corpus and records the observed
# window-start skew along with aggregate app and RDMA-counter rates.
#
# Usage on mkp1:
#   INITIATORS=2 bash /root/ipu-poc/run_multi_initiator_load.sh
#   INITIATORS=4 bash /root/ipu-poc/run_multi_initiator_load.sh
set -uo pipefail

H=/sys/class/infiniband/rocep69s0f0/ports/1/hw_counters
VENV=/root/lmcache-stage2/.venv/bin/lmcache
B=/mnt/lmcache-stage2/kvcache
OUT=/root/mkp1-sustained
SD=$(dirname "$0")

INITIATORS=${INITIATORS:-2}
KB=${KB:-28672}
PREFIX=${PREFIX:-ds28m}
WORKERS_PER_INITIATOR=${WORKERS_PER_INITIATOR:-4}
IN_FLIGHT_PER_INITIATOR=${IN_FLIGHT_PER_INITIATOR:-4}
DUR=${DUR:-120}
WARM=${WARM:-10}
POLL=${POLL:-0.25}
TRIM=${TRIM:-3}
MAX_START_SKEW_SEC=${MAX_START_SKEW_SEC:-2}
METRICS_BASE_PORT=${METRICS_BASE_PORT:-0}
CAP=${CAP:-4000}
RUN=${RUN_ID:-multi$(date +%s)}

case "$INITIATORS" in
  2|4) ;;
  *) echo "ABORT: INITIATORS must be 2 or 4"; exit 2 ;;
esac
case "$KB" in
  28672) NFILES=11072 ;;
  57344) NFILES=5568 ;;
  *) echo "ABORT: KB must be 28672 or 57344"; exit 2 ;;
esac
if [ $((NFILES % IN_FLIGHT_PER_INITIATOR)) -ne 0 ]; then
  echo "ABORT: corpus size $NFILES must divide in-flight $IN_FLIGHT_PER_INITIATOR"
  exit 2
fi
ROUNDS=$((NFILES / IN_FLIGHT_PER_INITIATOR))
case "$METRICS_BASE_PORT" in
  0|*[!0-9]*) [ "$METRICS_BASE_PORT" = 0 ] || {
    echo "ABORT: METRICS_BASE_PORT must be 0 or a TCP port"; exit 2; } ;;
esac

ADP="{\"type\":\"fs_native\",\"base_path\":\"$B\",\"num_workers\":$WORKERS_PER_INITIATOR,\"use_odirect\":true,\"max_capacity_gb\":$CAP}"
RUN_DIR="$OUT/$RUN"
MANIFEST="$OUT/${PREFIX}_corpus.manifest"
mkdir -p "$OUT"
if [ -e "$RUN_DIR" ]; then
  echo "ABORT: run directory exists: $RUN_DIR"
  exit 2
fi
mkdir "$RUN_DIR"

for flag in --key-prefix --duration-sec --warmup-sec; do
  if ! "$VENV" bench l2 --help 2>&1 | grep -q -- "$flag"; then
    echo "ABORT: '$VENV bench l2' does not accept $flag."
    echo "       Install the sustained-bench branch before running this driver."
    exit 1
  fi
done
if [ "$METRICS_BASE_PORT" -ne 0 ] &&
  ! "$VENV" bench l2 --help 2>&1 | grep -q -- "--serve-metrics"; then
  echo "ABORT: metrics requested but this bench does not support --serve-metrics"
  exit 1
fi

if [ ! -f "$MANIFEST" ]; then
  echo "ABORT: missing corpus manifest $MANIFEST"
  exit 1
fi
manifest_kb=$(grep '^data_size_kb=' "$MANIFEST" | cut -d= -f2)
manifest_nfiles=$(grep '^nfiles=' "$MANIFEST" | cut -d= -f2)
if [ "$manifest_kb" != "$KB" ] || [ "$manifest_nfiles" != "$NFILES" ]; then
  echo "ABORT: corpus manifest does not match KB=$KB NFILES=$NFILES"
  exit 1
fi
have=$(find "$B" -name "${PREFIX}-bench-model@*.data" | wc -l)
if [ "$have" -ne "$NFILES" ]; then
  echo "ABORT: corpus count $have does not equal expected $NFILES"
  exit 1
fi

echo "=== $RUN: $INITIATORS local read-only processes against prefix $PREFIX"
echo "=== per-process workers=$WORKERS_PER_INITIATOR in_flight=$IN_FLIGHT_PER_INITIATOR"
echo "=== aggregate workers=$((INITIATORS * WORKERS_PER_INITIATOR)) aggregate in_flight=$((INITIATORS * IN_FLIGHT_PER_INITIATOR))"
echo "=== existing-controller multi-process evidence only; no MP/controller/QP/R2 claim"

sync
echo 3 > /proc/sys/vm/drop_caches
sleep 2

poll_file="$RUN_DIR/rdma-poll.txt"
: > "$poll_file"
( while :; do
    echo "$(date +%s.%N) $(cat "$H/InRdmaWrites")"
    sleep "$POLL"
  done ) >> "$poll_file" 2>/dev/null &
poll_pid=$!
declare -a pids

cleanup() {
  kill "$poll_pid" 2>/dev/null || true
  for pid in "${pids[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  for fifo in "$RUN_DIR"/release-*; do
    [ -p "$fifo" ] && rm -f "$fifo"
  done
}
trap cleanup EXIT INT TERM

pre_w=$(cat "$H/InRdmaWrites")
pre_rt=$(cat "$H/RetransSegs")
pre_nak=$(cat "$H/Nak Sequence Error")
pre_rto=$(cat "$H/RTO")
pre_rnr=$(cat "$H/RNR received")
pre_oo=$(cat "$H/Rcvd Out of order packets")
pre_pe=$(cat "$H/InProtoErrors")
corpus_before=$(find "$B" -name "${PREFIX}-bench-model@*.data" | wc -l)

for ((id = 0; id < INITIATORS; id++)); do
  fifo="$RUN_DIR/release-$id"
  mkfifo "$fifo"
  (
    printf '%s\n' "$(date +%s.%N)" > "$RUN_DIR/initiator-$id.ready"
    if ! IFS= read -r _release < "$fifo"; then
      echo 1 > "$RUN_DIR/initiator-$id.status"
      exit 1
    fi
    metrics_args=()
    if [ "$METRICS_BASE_PORT" -ne 0 ]; then
      metrics_args=(
        --serve-metrics "$((METRICS_BASE_PORT + id))"
        --metrics-bind-address 127.0.0.1
      )
    fi
    PYTHONUNBUFFERED=1 "$VENV" bench l2 --l2-adapter "$ADP" --only load \
      --key-prefix "$PREFIX" --num-keys 1 --data-size-kb "$KB" \
      --in-flight "$IN_FLIGHT_PER_INITIATOR" --l1-align-bytes 4096 \
      --warmup-rounds 0 --rounds "$ROUNDS" --duration-sec "$DUR" \
      --warmup-sec "$WARM" "${metrics_args[@]}" \
      --output "$RUN_DIR/initiator-$id.json" --format json 2>&1 \
      | python3 -u -c 'import sys,time
for line in sys.stdin: sys.stdout.write("%.3f %s" % (time.time(), line))' \
      > "$RUN_DIR/initiator-$id.log"
    rc=${PIPESTATUS[0]}
    echo "$rc" > "$RUN_DIR/initiator-$id.status"
    exit "$rc"
  ) &
  pids[$id]=$!
done

deadline=$((SECONDS + 60))
while [ "$(find "$RUN_DIR" -name 'initiator-*.ready' | wc -l)" -ne "$INITIATORS" ]; do
  if [ "$SECONDS" -ge "$deadline" ]; then
    echo "ABORT: workers did not reach the local release barrier"
    exit 1
  fi
  sleep 0.05
done

release_unix_sec=$(date +%s.%N)
echo "$release_unix_sec" > "$RUN_DIR/release-unix-sec.txt"
echo "=== releasing $INITIATORS workers at $release_unix_sec"
declare -a release_pids
for ((id = 0; id < INITIATORS; id++)); do
  printf 'go\n' > "$RUN_DIR/release-$id" &
  release_pids[$id]=$!
done
for pid in "${release_pids[@]}"; do
  wait "$pid"
done

worker_failure=0
for pid in "${pids[@]}"; do
  wait "$pid" || worker_failure=1
done
sleep 3
kill "$poll_pid" 2>/dev/null || true

d_w=$(( $(cat "$H/InRdmaWrites") - pre_w ))
d_rt=$(( $(cat "$H/RetransSegs") - pre_rt ))
d_nak=$(( $(cat "$H/Nak Sequence Error") - pre_nak ))
d_rto=$(( $(cat "$H/RTO") - pre_rto ))
d_rnr=$(( $(cat "$H/RNR received") - pre_rnr ))
d_oo=$(( $(cat "$H/Rcvd Out of order packets") - pre_oo ))
d_pe=$(( $(cat "$H/InProtoErrors") - pre_pe ))
corpus_after=$(find "$B" -name "${PREFIX}-bench-model@*.data" | wc -l)

python3 "$SD/multi_initiator_report.py" \
  --run-dir "$RUN_DIR" --output "$RUN_DIR/aggregate.json" \
  --initiators "$INITIATORS" --data-size-kb "$KB" --poll "$poll_file" \
  --trim-sec "$TRIM" --max-start-skew-sec "$MAX_START_SKEW_SEC" \
  --corpus-before "$corpus_before" --corpus-after "$corpus_after" \
  --counter "InRdmaWrites=$d_w" --counter "RetransSegs=$d_rt" \
  --counter "Nak Sequence Error=$d_nak" --counter "RTO=$d_rto" \
  --counter "RNR received=$d_rnr" --counter "Rcvd Out of order packets=$d_oo" \
  --counter "InProtoErrors=$d_pe"
report_rc=$?

echo "=== artifacts: $RUN_DIR"
if [ "$worker_failure" -ne 0 ]; then
  echo "RESULT: REJECTED — at least one worker failed"
  exit 1
fi
exit "$report_rc"
