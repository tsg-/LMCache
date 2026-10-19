#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Local multi-process fs_native mixed read/write test for the existing MkP path.
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
IN_FLIGHT_PER_INITIATOR=${IN_FLIGHT_PER_INITIATOR:-8}
DUR=${DUR:-120}
# READ:WRITE payload ratio. 5:1 is the geometry the mixed chart pairs against the
# fio randrw rwmixread=83 cell (5/6 = 83.3% of bytes read); 9:1 pairs against
# rwmixread=90. Both sides express the mix as a byte fraction, so the two are
# directly comparable without converting either.
RATIO=${RATIO:-5:1}
WARM=${WARM:-0}
POLL=${POLL:-0.25}
TRIM=${TRIM:-3}
MAX_START_SKEW_SEC=${MAX_START_SKEW_SEC:-2}
METRICS_BASE_PORT=${METRICS_BASE_PORT:-9101}
MIN_FREE_GB=${MIN_FREE_GB:-600}
RUN=${RUN_ID:-mimix$(date +%s)}

[ "$INITIATORS" = 2 ] || { echo "ABORT: this bounded driver requires INITIATORS=2"; exit 2; }
[ "$KB" = 28672 ] || { echo "ABORT: this bounded driver requires KB=28672"; exit 2; }
[ $((11072 % IN_FLIGHT_PER_INITIATOR)) -eq 0 ] ||
  { echo "ABORT: in-flight must divide 11072"; exit 2; }
case $RATIO in
  *[!0-9:]*|:*|*:|*:*:*|"") echo "ABORT: RATIO must be READ:WRITE, got '$RATIO'"; exit 2 ;;
  *:*) ;;
  *) echo "ABORT: RATIO must be READ:WRITE, got '$RATIO'"; exit 2 ;;
esac

ROUNDS=$((11072 / IN_FLIGHT_PER_INITIATOR))
ADP="{\"type\":\"fs_native\",\"base_path\":\"$B\",\"num_workers\":$WORKERS_PER_INITIATOR,\"use_odirect\":true,\"max_capacity_gb\":4000}"
RUN_DIR="$OUT/$RUN"
MANIFEST="$OUT/${PREFIX}_corpus.manifest"

for flag in --read-write-ratio --write-key-prefix --duration-sec --serve-metrics; do
  "$VENV" bench l2 --help 2>&1 | grep -q -- "$flag" ||
    { echo "ABORT: bench l2 lacks $flag"; exit 1; }
done
[ -f "$MANIFEST" ] || { echo "ABORT: missing read corpus manifest"; exit 1; }
[ "$(grep '^data_size_kb=' "$MANIFEST" | cut -d= -f2)" = "$KB" ] ||
  { echo "ABORT: read corpus geometry mismatch"; exit 1; }
[ "$(find "$B" -name "${PREFIX}-bench-model@*.data" | wc -l)" -eq 11072 ] ||
  { echo "ABORT: read corpus count mismatch"; exit 1; }
[ "$(df -BG --output=avail /mnt/lmcache-stage2 | tail -1 | tr -dc 0-9)" -ge "$MIN_FREE_GB" ] ||
  { echo "ABORT: free capacity is below ${MIN_FREE_GB}G"; exit 1; }

mkdir -p "$OUT"
[ ! -e "$RUN_DIR" ] || { echo "ABORT: run directory exists"; exit 1; }
for id in 0 1; do
  [ "$(find "$B" -name "${RUN}w${id}-bench-model@*.data" | wc -l)" -eq 0 ] ||
    { echo "ABORT: write prefix ${RUN}w${id} is not fresh"; exit 1; }
done
mkdir "$RUN_DIR"

sync
echo 3 > /proc/sys/vm/drop_caches
sleep 2

poll_file="$RUN_DIR/rdma-poll.txt"
: > "$poll_file"
( while :; do
    echo "$(date +%s.%N) $(cat "$H/InRdmaWrites") $(cat "$H/InRdmaReads")"
    sleep "$POLL"
  done ) >> "$poll_file" 2>/dev/null &
poll_pid=$!
declare -a pids

cleanup() {
  kill "$poll_pid" 2>/dev/null || true
  for pid in "${pids[@]}"; do kill "$pid" 2>/dev/null || true; done
  rm -f "$RUN_DIR"/release-* 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for counter in InRdmaWrites InRdmaReads RetransSegs "Nak Sequence Error" RTO \
  "RNR received" "Rcvd Out of order packets" InProtoErrors; do
  printf '%s=%s\n' "$counter" "$(cat "$H/$counter")" >> "$RUN_DIR/pre-counters.txt"
done
corpus_before=$(find "$B" -name "${PREFIX}-bench-model@*.data" | wc -l)

for ((id = 0; id < INITIATORS; id++)); do
  fifo="$RUN_DIR/release-$id"
  mkfifo "$fifo"
  (
    printf '%s\n' "$(date +%s.%N)" > "$RUN_DIR/initiator-$id.ready"
    IFS= read -r _ < "$fifo" || exit 1
    PYTHONUNBUFFERED=1 "$VENV" bench l2 --l2-adapter "$ADP" \
      --key-prefix "$PREFIX" --write-key-prefix "${RUN}w${id}" \
      --read-write-ratio "$RATIO" --num-keys 1 --data-size-kb "$KB" \
      --in-flight "$IN_FLIGHT_PER_INITIATOR" --l1-align-bytes 4096 \
      --warmup-rounds 0 --rounds "$ROUNDS" --duration-sec "$DUR" \
      --warmup-sec "$WARM" --no-skip-verify \
      --serve-metrics "$((METRICS_BASE_PORT + id))" \
      --metrics-bind-address 127.0.0.1 \
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
  [ "$SECONDS" -lt "$deadline" ] || { echo "ABORT: workers missed barrier"; exit 1; }
  sleep 0.05
done
declare -a release_pids
for ((id = 0; id < INITIATORS; id++)); do
  printf 'go\n' > "$RUN_DIR/release-$id" &
  release_pids[$id]=$!
done
for pid in "${release_pids[@]}"; do wait "$pid"; done
worker_failure=0
for pid in "${pids[@]}"; do wait "$pid" || worker_failure=1; done
sleep 3
kill "$poll_pid" 2>/dev/null || true

for counter in InRdmaWrites InRdmaReads RetransSegs "Nak Sequence Error" RTO \
  "RNR received" "Rcvd Out of order packets" InProtoErrors; do
  before=$(grep "^$counter=" "$RUN_DIR/pre-counters.txt" | cut -d= -f2)
  after=$(cat "$H/$counter")
  printf '%s=%s\n' "$counter" "$((after - before))" >> "$RUN_DIR/delta-counters.txt"
done
corpus_after=$(find "$B" -name "${PREFIX}-bench-model@*.data" | wc -l)

args=()
while IFS= read -r counter; do args+=(--counter "$counter"); done < "$RUN_DIR/delta-counters.txt"
python3 "$SD/multi_initiator_mixed_report.py" \
  --run-dir "$RUN_DIR" --output "$RUN_DIR/aggregate.json" \
  --initiators "$INITIATORS" --data-size-kb "$KB" --poll "$poll_file" \
  --trim-sec "$TRIM" --max-start-skew-sec "$MAX_START_SKEW_SEC" \
  --requested-ratio "$RATIO" \
  --corpus-before "$corpus_before" --corpus-after "$corpus_after" "${args[@]}"
report_rc=$?

[ "$worker_failure" -eq 0 ] || { echo "RESULT: REJECTED — worker failure"; exit 1; }
exit "$report_rc"
