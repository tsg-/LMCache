#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Start several local profile-shaped L2 load or mixed processes.
#
# This is a local fan-out helper. It does not synchronize start times, create
# remote initiators, or validate aggregate fabric counters.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
MODE=${1:-help}
if [ "$#" -gt 0 ]; then
  shift
fi

usage() {
  cat <<'EOF'
Usage:
  run_geom_multi.sh load PROFILE
  run_geom_multi.sh mixed PROFILE

Required environment variables:
  BASE_PATH       fs_native storage directory
  PREFIX          existing read corpus namespace

Optional environment variables:
  INITIATORS      local processes to start (default: 2)
  WORKERS_TOTAL   split evenly across processes (default: 16)
  WORKERS_PER     overrides the split worker count
  OUT             artifact directory (default: ./geometry-runs/<timestamp>)
  All run_model_geometry.sh variables, including PYTHON, IN_FLIGHT,
  DURATION_SEC, READ_WRITE_RATIO, and L2_ADAPTER.

For mixed mode, this helper generates a distinct WRITE_PREFIX per process.
EOF
}

require_value() {
  local name=$1
  if [ -z "${!name:-}" ]; then
    echo "ABORT: $name must be set" >&2
    exit 2
  fi
}

run_fanout() {
  local mode=$1 profile=$2
  require_value BASE_PATH
  require_value PREFIX
  local initiators=${INITIATORS:-2}
  local workers_total=${WORKERS_TOTAL:-16}
  [[ "$initiators" =~ ^[1-9][0-9]*$ ]] ||
    { echo "ABORT: INITIATORS must be a positive integer" >&2; exit 2; }

  local workers_per
  if [ -n "${WORKERS_PER:-}" ]; then
    workers_per=$WORKERS_PER
  else
    [ $(( workers_total % initiators )) -eq 0 ] ||
      { echo "ABORT: WORKERS_TOTAL must divide evenly across INITIATORS" >&2; exit 2; }
    workers_per=$(( workers_total / initiators ))
  fi

  local out=${OUT:-"./geometry-runs/$(date +%Y%m%d-%H%M%S)"}
  [ ! -e "$out" ] || { echo "ABORT: output path already exists: $out" >&2; exit 2; }
  mkdir -p "$out"

  echo "profile: $profile"
  echo "processes: $initiators"
  echo "workers/process: $workers_per"
  echo "artifacts: $out"

  local single_mode=sustained-load
  if [ "$mode" = mixed ]; then
    single_mode=mixed
  fi
  local -a pids=()
  local id write_prefix
  for ((id = 0; id < initiators; id++)); do
    write_prefix=
    if [ "$mode" = mixed ]; then
      write_prefix="${PREFIX}-write-$(date +%s)-$id"
    fi
    (
      NUM_WORKERS="$workers_per" \
      OUTPUT="$out/initiator-$id.json" \
      WRITE_PREFIX="$write_prefix" \
      BASE_PATH="$BASE_PATH" \
      PREFIX="$PREFIX" \
      bash "$SCRIPT_DIR/run_model_geometry.sh" \
        "$single_mode" "$profile"
    ) > "$out/initiator-$id.log" 2>&1 &
    pids+=("$!")
  done

  local status=0
  for id in "${!pids[@]}"; do
    if ! wait "${pids[$id]}"; then
      echo "FAILED: initiator $id; see $out/initiator-$id.log" >&2
      status=1
    fi
  done
  [ "$status" -eq 0 ] || exit "$status"
  echo "complete: $initiators local $mode processes"
}

case "$MODE" in
  help|-h|--help)
    usage
    ;;
  load|mixed)
    [ "$#" -eq 1 ] || { usage >&2; exit 2; }
    run_fanout "$MODE" "$1"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
