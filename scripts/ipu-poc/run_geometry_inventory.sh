#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Run or preflight a profile-shaped L2 sweep from a host-only inventory.
#
# The inventory deliberately identifies machines only. The benchmark directory
# sits beneath the remote NVMe-oF mount; BENCH_MOUNT may name a different mount
# per rig, but preflight is what keeps a run off the OS filesystem -- it refuses
# any mount that is not a real mount point backed by a controller the target
# serves.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
readonly BENCH_MOUNT=${BENCH_MOUNT:-/mnt/lmcache}
readonly BENCH_ROOT="$BENCH_MOUNT/bench-l2"
readonly IN_FLIGHTS=${IN_FLIGHTS:-"1"}
readonly WARMUP_SEC=${WARMUP_SEC:-0}
readonly DURATION_SEC=${DURATION_SEC:-60}
readonly INCLUDE_MINIMAX=${INCLUDE_MINIMAX:-0}
MODE=${1:-help}
if [ "$#" -gt 0 ]; then
  shift
fi

usage() {
  cat <<'EOF'
Usage:
  run_geometry_inventory.sh show INVENTORY
  run_geometry_inventory.sh preflight INVENTORY
  run_geometry_inventory.sh verify INVENTORY
  run_geometry_inventory.sh sweep INVENTORY

The inventory is Bash syntax and must contain only:
  INITIATOR_HOSTS=(host1 host2 ...)
  TARGET_HOST=host

The coordinator uses $BENCH_MOUNT/bench-l2 on every initiator, where
BENCH_MOUNT defaults to /mnt/lmcache. It refuses to run unless that mount
exists and contains an NVMe-oF controller served by TARGET_HOST. Python and
the LMCache checkout are discovered on each initiator; no interpreter or
filesystem paths belong in the inventory.

preflight checks the environment without writing. verify runs the small
profile-identity gate on each initiator. sweep runs the timed five-profile
read benchmark.

Sweep environment variables:
  IN_FLIGHTS       space-separated submit counts (default: "1")
  WARMUP_SEC       warmup seconds before each sustained load (default: 0)
  DURATION_SEC     measured seconds for each sustained load (default: 60)
  INCLUDE_MINIMAX  set to 1 to append the O_DIRECT-padded MiniMax-M3 profile
EOF
}

die() {
  echo "ABORT: $*" >&2
  exit 2
}

validate_hostname() {
  local name=$1
  [[ "$name" =~ ^[[:alnum:]][[:alnum:].-]*$ ]] ||
    die "invalid hostname in inventory: $name"
}

# The inventory is sourced, so it is executable bash. Vet it line by line
# first: a check that runs after `source` enforces the hostnames-only contract
# only after whatever else was in the file has already run.
lint_inventory() {
  local inventory=$1
  local line lineno=0
  local target_host_pattern
  target_host_pattern="^[[:space:]]*TARGET_HOST=([[:alnum:]][[:alnum:].-]*|\"[[:alnum:]][[:alnum:].-]*\"|'[[:alnum:]][[:alnum:].-]*')[[:space:]]*$"
  while IFS= read -r line || [ -n "$line" ]; do
    lineno=$((lineno + 1))
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    [[ "$line" =~ ^[[:space:]]*INITIATOR_HOSTS=\([^()\;\&\|\$\`\<\>]*\)[[:space:]]*$ ]] &&
      continue
    [[ "$line" =~ $target_host_pattern ]] && continue
    die "inventory must contain hostnames only; line $lineno is not a host" \
      "assignment: $line"
  done <"$inventory"
}

load_inventory() {
  local inventory=$1
  [ -f "$inventory" ] || die "inventory not found: $inventory"
  lint_inventory "$inventory"

  unset INITIATOR_HOSTS TARGET_HOST
  # shellcheck disable=SC1090
  source "$inventory"

  declare -p INITIATOR_HOSTS >/dev/null 2>&1 ||
    die "inventory must define INITIATOR_HOSTS=(...)"
  [ "${#INITIATOR_HOSTS[@]}" -gt 0 ] ||
    die "INITIATOR_HOSTS must not be empty"
  [ -n "${TARGET_HOST:-}" ] || die "inventory must define TARGET_HOST"

  local host
  for host in "${INITIATOR_HOSTS[@]}" "$TARGET_HOST"; do
    validate_hostname "$host"
  done
}

show_inventory() {
  echo "initiators: ${INITIATOR_HOSTS[*]}"
  echo "target: $TARGET_HOST"
  echo "benchmark root: $BENCH_ROOT"
}

target_addresses() {
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$TARGET_HOST" '
    for port in /sys/kernel/config/nvmet/ports/*; do
      [ -d "$port" ] || continue
      cat "$port/addr_traddr"
    done
  ' | sort -u
}

preflight_initiator() {
  local host=$1
  local addresses=$2
  # ssh joins its arguments into a single remote shell command, so a newline
  # inside an argument is read as a command separator. Send the address list
  # comma-joined and split it again on the far side.
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" \
    bash -s -- "$BENCH_MOUNT" "$(tr '\n' ',' <<<"$addresses")" <<'REMOTE'
set -euo pipefail

mount=$1
addresses=$2
[ "$(findmnt -rn -o TARGET -T "$mount")" = "$mount" ] ||
  { echo "ABORT: $mount is not a mount point" >&2; exit 2; }
command -v nvme >/dev/null ||
  { echo "ABORT: nvme CLI is required to verify the remote mount" >&2; exit 2; }

subsystems=$(nvme list-subsys 2>/dev/null)
matched=0
while IFS= read -r address; do
  [ -n "$address" ] || continue
  if grep -Fq "rdma traddr=$address" <<<"$subsystems"; then
    matched=1
    break
  fi
done <<<"$(tr ',' '\n' <<<"$addresses")"
[ "$matched" -eq 1 ] ||
  { echo "ABORT: no NVMe-oF controller matches the target export" >&2; exit 2; }

for repo in "$HOME/LMCache" /root/LMCache; do
  if [ -x "$repo/scripts/ipu-poc/run_model_geometry.sh" ]; then
    printf '%s\n' "$repo"
    exit 0
  fi
done
echo "ABORT: LMCache geometry checkout not found under HOME or /root" >&2
exit 2
REMOTE
}

preflight() {
  local addresses
  addresses=$(target_addresses)
  [ -n "$addresses" ] ||
    die "$TARGET_HOST has no configured NVMe-oF target address"

  local host repo
  for host in "${INITIATOR_HOSTS[@]}"; do
    repo=$(preflight_initiator "$host" "$addresses") ||
      die "preflight failed on $host"
    echo "preflight: $host repo=$repo mount=$BENCH_MOUNT"
  done
}

verify_identity() {
  preflight

  local run_id=${RUN_ID:-"verify-$(date +%Y%m%d-%H%M%S)"}
  local host
  for host in "${INITIATOR_HOSTS[@]}"; do
    ssh -o BatchMode=yes "$host" bash -s -- "$run_id" "$BENCH_ROOT" <<'REMOTE'
set -euo pipefail

run_id=$1
bench_root=$2
for repo in "$HOME/LMCache" /root/LMCache; do
  [ -x "$repo/scripts/ipu-poc/verify_geometry_corpus.sh" ] && break
done
[ -x "$repo/scripts/ipu-poc/verify_geometry_corpus.sh" ] ||
  { echo "ABORT: LMCache geometry checkout not found" >&2; exit 2; }

host_name=$(hostname -s)
cd "$repo"
BASE_PATH="$bench_root/$run_id/$host_name" \
  bash scripts/ipu-poc/verify_geometry_corpus.sh
REMOTE
  done
}

validate_sweep_options() {
  local in_flight
  [[ "$IN_FLIGHTS" =~ ^[1-9][0-9]*(\ [1-9][0-9]*)*$ ]] ||
    die "IN_FLIGHTS must be a space-separated list of positive integers"
  for in_flight in $IN_FLIGHTS; do
    [ "$in_flight" -le 1024 ] ||
      die "IN_FLIGHTS entries must not exceed 1024"
  done
  [[ "$WARMUP_SEC" =~ ^[0-9]+([.][0-9]+)?$ ]] ||
    die "WARMUP_SEC must be a non-negative number of seconds"
  [[ "$DURATION_SEC" =~ ^[1-9][0-9]*([.][0-9]+)?$ ]] ||
    die "DURATION_SEC must be a positive number of seconds"
  [[ "$INCLUDE_MINIMAX" =~ ^[01]$ ]] ||
    die "INCLUDE_MINIMAX must be 0 or 1"
}

run_sweep() {
  preflight
  validate_sweep_options

  local run_id=${RUN_ID:-"geometry-$(date +%Y%m%d-%H%M%S)"}
  local host
  local -a pids=()
  for host in "${INITIATOR_HOSTS[@]}"; do
    ssh -o BatchMode=yes "$host" bash -s -- "$run_id" "$BENCH_ROOT" \
      "$IN_FLIGHTS" "$WARMUP_SEC" "$DURATION_SEC" "$INCLUDE_MINIMAX" <<'REMOTE' &
set -euo pipefail

run_id=$1
bench_root=$2
in_flights=$3
warmup_sec=$4
duration_sec=$5
include_minimax=$6
for repo in "$HOME/LMCache" /root/LMCache; do
  [ -x "$repo/scripts/ipu-poc/run_model_geometry.sh" ] && break
done
[ -x "$repo/scripts/ipu-poc/run_model_geometry.sh" ] ||
  { echo "ABORT: LMCache geometry checkout not found" >&2; exit 2; }

cd "$repo"
host_name=$(hostname -s)
base_path="$bench_root/$run_id/$host_name"
prefix="$run_id-$host_name"
mkdir -p results
unset L2_ADAPTER L1_ALIGN_BYTES
profiles=(
  scripts/ipu-poc/models/mixtral_8x22b_fp8_64k.yaml \
  scripts/ipu-poc/models/mixtral_8x22b_fp8_128k.yaml \
  scripts/ipu-poc/models/deepseek_v3_fp8.yaml \
  scripts/ipu-poc/models/mixtral_8x22b_fp8.yaml \
  scripts/ipu-poc/models/mixtral_8x22b_fp8_512k.yaml
)
[ "$include_minimax" -eq 0 ] ||
  profiles+=(scripts/ipu-poc/models/minimax_m3_bf16_tp8_odirect.yaml)
read -r -a in_flight_values <<<"$in_flights"

for in_flight in "${in_flight_values[@]}"; do
  for profile in "${profiles[@]}"; do
    model=$(basename "$profile" .yaml)
    common_env=(
      "BASE_PATH=$base_path"
      "PREFIX=$prefix-if${in_flight}"
      "IN_FLIGHT=$in_flight"
    )
    env ROUNDS=2 "${common_env[@]}" \
      bash scripts/ipu-poc/run_model_geometry.sh store "$profile"
    env OUTPUT="results/$run_id-$host_name-$model-if${in_flight}.json" \
      WARMUP_SEC="$warmup_sec" DURATION_SEC="$duration_sec" METRICS_PORT=9101 \
      "${common_env[@]}" \
      bash scripts/ipu-poc/run_model_geometry.sh sustained-load "$profile"
  done
done
REMOTE
    pids+=("$!")
  done

  local status=0
  for host in "${!pids[@]}"; do
    if ! wait "${pids[$host]}"; then
      echo "FAILED: sweep on ${INITIATOR_HOSTS[$host]}" >&2
      status=1
    fi
  done
  [ "$status" -eq 0 ] || exit "$status"
}

case "$MODE" in
  help|-h|--help)
    usage
    ;;
  show|preflight|verify|sweep)
    [ "$#" -eq 1 ] || { usage >&2; exit 2; }
    load_inventory "$1"
    case "$MODE" in
      show) show_inventory ;;
      preflight) preflight ;;
      verify) verify_identity ;;
      sweep) run_sweep ;;
    esac
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
