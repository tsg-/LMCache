#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Sweep NVMe-oF I/O queue pairs per controller for the fixed MMG topology.
#
# A QP count changes controller state: this helper deliberately owns only the
# four MMGT controllers behind md0, checks their identity before every change,
# and restores 128 I/O QPs per controller if the sweep stops partway through.
set -euo pipefail

readonly BENCH_MOUNT=${BENCH_MOUNT:-/mnt/lmcache}
readonly BENCH_ROOT="$BENCH_MOUNT/bench-l2"
readonly NQN_PREFIX=${NQN_PREFIX:-nqn.2026-08.lab.mmgt}
readonly QP_COUNTS=${QP_COUNTS:-"1 2 4 8 16 32 64 128"}
readonly RESTORE_QPS=${RESTORE_QPS:-128}
readonly PROFILE=${PROFILE:-scripts/ipu-poc/models/minimax_m3_bf16_tp8_odirect.yaml}
readonly IN_FLIGHT=${IN_FLIGHT:-8}
readonly NUM_WORKERS=${NUM_WORKERS:-32}
readonly WARMUP_SEC=${WARMUP_SEC:-60}
readonly DURATION_SEC=${DURATION_SEC:-60}
readonly REPETITIONS=${REPETITIONS:-3}
readonly CONNECT_TIMEOUT_SEC=${CONNECT_TIMEOUT_SEC:-90}
readonly FIO_BLOCK_SIZE=256k
readonly FIO_NUMJOBS=32
readonly FIO_IODEPTH=8
readonly FIO_IOENGINE=libaio
readonly FIO_MATRIX_DEPTHS=${FIO_MATRIX_DEPTHS:-"1 2 4 8 16 32"}
# The active tunnel map is 19110→mmgi0:9101, 19120→mmgi1:9102,
# 19130→mmgi2:9103, and 19140→mmgi3:9104. The remote ports below, not the
# Prometheus-side ports, are passed to the one bench process on each host.
readonly MMG_METRICS_PORT_MAP=${MMG_METRICS_PORT_MAP:-"mmgi0:9101 mmgi1:9102 mmgi2:9103 mmgi3:9104"}
readonly FLEET_CORPUS_RUN_ID=${FLEET_CORPUS_RUN_ID:-minimaxqp-20260924-0142}
readonly FLEET_QP_COUNTS=${FLEET_QP_COUNTS:-"2 32 128"}
readonly FLEET_IN_FLIGHTS=${FLEET_IN_FLIGHTS:-"1 2 4"}
readonly FLEET_WARMUP_SEC=${FLEET_WARMUP_SEC:-90}
readonly FLEET_DURATION_SEC=${FLEET_DURATION_SEC:-60}
readonly FLEET_REPETITIONS=${FLEET_REPETITIONS:-3}

MODE=${1:-help}
if [ "$#" -gt 0 ]; then
  shift
fi

usage() {
  cat <<'EOF'
Usage:
  run_qp_scale.sh show INVENTORY
  run_qp_scale.sh show-fio INVENTORY
  run_qp_scale.sh preflight INVENTORY
  APPLY=1 RUN_ID=NAME run_qp_scale.sh sweep INVENTORY
  APPLY=1 RUN_ID=NAME run_qp_scale.sh sweep-fio INVENTORY
  APPLY=1 RUN_ID=NAME run_qp_scale.sh sweep-if-fleet INVENTORY
  APPLY=1 RUN_ID=NAME run_qp_scale.sh reconnect-fleet QPS INVENTORY

The inventory is Bash syntax and must contain only:
  INITIATOR_HOSTS=(host1 host2 host3 host4)
  TARGET_HOST=host

show is storage-free. preflight reads the target and initiators without making
changes. sweep creates one fresh MiniMax corpus at 128 I/O QPs/controller,
then measures load-only windows at every QP_COUNTS setting. It disconnects and
reconnects only the four MMGT NVMe-oF controllers behind md0 on one initiator
at a time. On an interrupted sweep it attempts to restore 128 I/O
QPs/controller before returning.

Required for sweep:
  APPLY=1          explicit acknowledgement that controller reconfiguration
                   unmounts and rebuilds md0 on every initiator
  RUN_ID=NAME      a new run namespace; letters, digits, dot, dash, underscore

sweep-fio uses the same controller lifecycle but runs fixed read-only FIO
controls on /dev/md0: 256 KiB random reads, 32 jobs, depth 8, with
completion-latency percentiles through p99.9. It never writes the mounted RAID
or benchmark corpus.

sweep-if-fleet reuses an existing read-only MiniMax corpus and records one
fleet counter snapshot pair for each QP/in-flight/repetition cell. Its default
axis is QPs/controller 2,32,128 x in_flight 1,2,4, three repetitions.
The coordinator must be installed on mmgi1 at /tmp/measure_mmg_fleet.py.
The loaded nvme_rdma driver caps each controller's -i count at the host's
online CPU count; preflight rejects higher anchors before changing storage.

reconnect-fleet applies one QP setting with the same guarded reconnect path,
then verifies queue_count on all 16 controllers. It is for the detached
overnight controller, which owns restoration.

Optional sweep variables:
  QP_COUNTS        I/O QPs/controller (default: "1 2 4 8 16 32 64 128")
  RESTORE_QPS      I/O QPs/controller to restore (default: 128)
  PROFILE          MiniMax profile path in each LMCache checkout
  IN_FLIGHT        bench-l2 submits in flight (default: 8)
  NUM_WORKERS      fs_native workers (default: 32)
  WARMUP_SEC       load warmup seconds (default: 60)
  DURATION_SEC     measured load seconds (default: 60)
  REPETITIONS      load windows per QP point (default: 3)
  CONNECT_TIMEOUT_SEC
                  seconds to wait for all reconnected controllers (default: 90)
  MMG_METRICS_PORT_MAP
                  host:remote-port tunnel map (default: mmgi0:9101 through
                  mmgi3:9104)
  BENCH_MOUNT      mounted md0 path (default: /mnt/lmcache)
  NQN_PREFIX       MMGT NQN prefix (default: nqn.2026-08.lab.mmgt)
  FLEET_CORPUS_RUN_ID, FLEET_QP_COUNTS, FLEET_IN_FLIGHTS,
  FLEET_WARMUP_SEC, FLEET_DURATION_SEC, FLEET_REPETITIONS
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
  local expected_initiators=${2:-4}
  [ -f "$inventory" ] || die "inventory not found: $inventory"
  lint_inventory "$inventory"

  unset INITIATOR_HOSTS TARGET_HOST
  # shellcheck disable=SC1090
  source "$inventory"
  declare -p INITIATOR_HOSTS >/dev/null 2>&1 ||
    die "inventory must define INITIATOR_HOSTS=(...)"
  [ "${#INITIATOR_HOSTS[@]}" -eq "$expected_initiators" ] ||
    die "QP scaling requires exactly $expected_initiators initiators"
  [ -n "${TARGET_HOST:-}" ] || die "inventory must define TARGET_HOST"

  local host
  for host in "${INITIATOR_HOSTS[@]}" "$TARGET_HOST"; do
    validate_hostname "$host"
  done
}

validate_options() {
  local qps
  [[ "$QP_COUNTS" =~ ^[1-9][0-9]*(\ [1-9][0-9]*)*$ ]] ||
    die "QP_COUNTS must be a space-separated list of positive integers"
  [[ "$RESTORE_QPS" =~ ^[1-9][0-9]*$ ]] ||
    die "RESTORE_QPS must be a positive integer"
  [[ "$IN_FLIGHT" =~ ^[1-9][0-9]*$ ]] ||
    die "IN_FLIGHT must be a positive integer"
  [[ "$NUM_WORKERS" =~ ^[1-9][0-9]*$ ]] ||
    die "NUM_WORKERS must be a positive integer"
  [[ "$WARMUP_SEC" =~ ^[0-9]+([.][0-9]+)?$ ]] ||
    die "WARMUP_SEC must be a non-negative number of seconds"
  [[ "$DURATION_SEC" =~ ^[1-9][0-9]*([.][0-9]+)?$ ]] ||
    die "DURATION_SEC must be a positive number of seconds"
  [[ "$REPETITIONS" =~ ^[1-9][0-9]*$ ]] ||
    die "REPETITIONS must be a positive integer"
  [[ "$CONNECT_TIMEOUT_SEC" =~ ^[1-9][0-9]*$ ]] ||
    die "CONNECT_TIMEOUT_SEC must be a positive integer"
  for qps in $QP_COUNTS "$RESTORE_QPS"; do
    [ "$qps" -le 1024 ] ||
      die "I/O QPs/controller must not exceed 1024"
  done
}

aggregate_qps() {
  local qps
  local -a aggregate=()
  for qps in $QP_COUNTS; do
    aggregate+=("$((qps * ${#INITIATOR_HOSTS[@]}))")
  done
  printf '%s' "${aggregate[*]}"
}

metrics_port_for_host() {
  local host=$1
  local entry
  for entry in $MMG_METRICS_PORT_MAP; do
    if [ "${entry%%:*}" = "$host" ]; then
      [[ "${entry##*:}" =~ ^[1-9][0-9]*$ ]] &&
        [ "${entry##*:}" -le 65535 ] ||
        die "invalid metrics port in MMG_METRICS_PORT_MAP: $entry"
      printf '%s\n' "${entry##*:}"
      return
    fi
  done
  die "MMG_METRICS_PORT_MAP has no remote port for $host"
}

show_inventory() {
  echo "initiators: ${INITIATOR_HOSTS[*]}"
  echo "target: $TARGET_HOST"
  echo "controllers/initiator: 4"
  echo "I/O QPs/controller: $QP_COUNTS"
  echo "aggregate I/O QPs: $(aggregate_qps)"
  echo "profile: $PROFILE"
  echo "load: in_flight=$IN_FLIGHT workers=$NUM_WORKERS warmup=${WARMUP_SEC}s duration=${DURATION_SEC}s repetitions=$REPETITIONS"
  echo "execution: dry-run (set APPLY=1 for storage changes)"
}

show_fio_inventory() {
  echo "initiators: ${INITIATOR_HOSTS[*]}"
  echo "target: $TARGET_HOST"
  echo "controllers/initiator: 4"
  echo "I/O QPs/controller: $QP_COUNTS"
  echo "aggregate I/O QPs: $(aggregate_qps)"
  echo "FIO control: rw=randread bs=$FIO_BLOCK_SIZE numjobs=$FIO_NUMJOBS iodepth=$FIO_IODEPTH"
  echo "latency: completion p50/p90/p95/p99/p99.9"
  echo "load: warmup=${WARMUP_SEC}s duration=${DURATION_SEC}s repetitions=$REPETITIONS"
  echo "execution: dry-run (set APPLY=1 for storage changes)"
}

show_fio_matrix_inventory() {
  echo "initiators: ${INITIATOR_HOSTS[*]}"
  echo "target: $TARGET_HOST"
  echo "controllers/initiator: 4"
  echo "I/O QPs/controller: 1 2 4 8 16 32"
  echo "FIO depths: $FIO_MATRIX_DEPTHS"
  echo "FIO control: rw=randread bs=$FIO_BLOCK_SIZE numjobs=$FIO_NUMJOBS"
  echo "latency: completion p50/p90/p95/p99/p99.9"
  echo "execution: dry-run (set APPLY=1 for storage changes)"
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
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" bash -s -- \
    "$BENCH_MOUNT" "$NQN_PREFIX" "$(tr '\n' ',' <<<"$addresses")" <<'REMOTE'
set -euo pipefail

mount=$1
nqn_prefix=$2
addresses=$3
[ "$(findmnt -rn -o TARGET -T "$mount")" = "$mount" ] ||
  { echo "ABORT: $mount is not a mount point" >&2; exit 2; }
command -v nvme >/dev/null ||
  { echo "ABORT: nvme CLI is required" >&2; exit 2; }
command -v mdadm >/dev/null ||
  { echo "ABORT: mdadm is required" >&2; exit 2; }

controller_namespace_device() {
  local controller=$1
  local controller_name controller_id child child_name

  controller_name=$(basename "$controller")
  controller_id=${controller_name#nvme}
  for child in "$controller"/nvme*c"${controller_id}"n*; do
    [ -e "$child" ] || continue
    child_name=$(basename "$child")
    if [[ "$child_name" =~ ^nvme([0-9]+)c${controller_id}n([0-9]+)$ ]]; then
      printf '/dev/nvme%sn%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
      return 0
    fi
  done
  return 1
}

matched=0
while IFS= read -r controller; do
  [ -r "$controller/subsysnqn" ] || continue
  nqn=$(<"$controller/subsysnqn")
  [ "${nqn#"$nqn_prefix"}" != "$nqn" ] || continue
  address=$(<"$controller/address")
  traddr=${address#*traddr=}
  traddr=${traddr%%,*}
  grep -Fqx "$traddr" <(tr ',' '\n' <<<"$addresses") ||
    { echo "ABORT: MMGT controller $controller uses unexpected address $traddr" >&2; exit 2; }
  [ "$(<"$controller/state")" = live ] ||
    { echo "ABORT: MMGT controller $controller is not live" >&2; exit 2; }
  namespace_device=$(controller_namespace_device "$controller") ||
    { echo "ABORT: MMGT controller $controller has no namespace device" >&2; exit 2; }
  [ -b "$namespace_device" ] ||
    { echo "ABORT: MMGT namespace is not ready: $namespace_device" >&2; exit 2; }
  matched=$((matched + 1))
done < <(find /sys/class/nvme -maxdepth 1 -name 'nvme[0-9]*' | sort)
[ "$matched" -eq 4 ] ||
  { echo "ABORT: expected four live MMGT NVMe-oF controllers; found $matched" >&2; exit 2; }

for repo in /root/anthropic/LMCache "$HOME/LMCache" /root/LMCache; do
  [ -x "$repo/scripts/ipu-poc/run_model_geometry.sh" ] && {
    printf '%s\n' "$repo"
    exit 0
  }
done
echo "ABORT: LMCache geometry checkout not found under HOME or /root" >&2
exit 2
REMOTE
}

preflight() {
  local addresses host repo
  addresses=$(target_addresses)
  [ -n "$addresses" ] ||
    die "$TARGET_HOST has no configured NVMe-oF target address"
  for host in "${INITIATOR_HOSTS[@]}"; do
    repo=$(preflight_initiator "$host" "$addresses") ||
      die "preflight failed on $host"
    echo "preflight: $host repo=$repo mount=$BENCH_MOUNT"
  done
}

preflight_fio_initiator() {
  local host=$1
  local addresses=$2

  preflight_initiator "$host" "$addresses" >/dev/null
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" bash -s -- \
    "$BENCH_MOUNT" "$FIO_IOENGINE" <<'REMOTE'
set -euo pipefail

mount=$1
fio_ioengine=$2
command -v fio >/dev/null ||
  { echo "ABORT: fio is required" >&2; exit 2; }
fio --enghelp="$fio_ioengine" >/dev/null 2>&1 ||
  { echo "ABORT: fio engine $fio_ioengine is unavailable" >&2; exit 2; }
[ -b /dev/md0 ] ||
  { echo "ABORT: /dev/md0 is not a block device" >&2; exit 2; }
[ "$(findmnt -rn -T "$mount" -o SOURCE)" = /dev/md0 ] ||
  { echo "ABORT: $mount is not mounted from /dev/md0" >&2; exit 2; }
REMOTE
}

preflight_fio() {
  local addresses host
  addresses=$(target_addresses)
  [ -n "$addresses" ] ||
    die "$TARGET_HOST has no configured NVMe-oF target address"
  for host in "${INITIATOR_HOSTS[@]}"; do
    preflight_fio_initiator "$host" "$addresses" ||
      die "FIO preflight failed on $host"
    echo "preflight-fio: $host device=/dev/md0 engine=$FIO_IOENGINE"
  done
}

require_restore_qps() {
  local host=$1
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" bash -s -- \
    "$NQN_PREFIX" "$RESTORE_QPS" <<'REMOTE'
set -euo pipefail
nqn_prefix=$1
expected=$(( $2 + 1 ))
matched=0
while IFS= read -r controller; do
  [ -r "$controller/subsysnqn" ] || continue
  nqn=$(<"$controller/subsysnqn")
  [ "${nqn#"$nqn_prefix"}" != "$nqn" ] || continue
  [ "$(<"$controller/queue_count")" -eq "$expected" ] ||
    { echo "ABORT: $controller queue_count is not $expected" >&2; exit 2; }
  matched=$((matched + 1))
done < <(find /sys/class/nvme -maxdepth 1 -name 'nvme[0-9]*' | sort)
[ "$matched" -eq 4 ] ||
  { echo "ABORT: expected four MMGT controllers; found $matched" >&2; exit 2; }
REMOTE
}

require_baseline_qps() {
  local host
  for host in "${INITIATOR_HOSTS[@]}"; do
    require_restore_qps "$host" ||
      die "$host is not at the required ${RESTORE_QPS} I/O QPs/controller baseline"
  done
}

reconfigure_initiator() {
  local host=$1
  local qps=$2
  local addresses=$3
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" sudo -n bash -s -- \
    "$BENCH_MOUNT" "$NQN_PREFIX" "$qps" "$(tr '\n' ',' <<<"$addresses")" \
    "$RUN_ID" "$CONNECT_TIMEOUT_SEC" <<'REMOTE'
set -euo pipefail

mount=$1
nqn_prefix=$2
qps=$3
addresses=$4
run_id=$5
connect_timeout_sec=$6
expected_queue_count=$((qps + 1))
manifest="/run/lmcache-qp-scale-${run_id}.controllers"
declare -a nqns=()
declare -a traddrs=()
declare -a trsvcids=()

controller_namespace_device() {
  local controller=$1
  local controller_name controller_id child child_name

  controller_name=$(basename "$controller")
  controller_id=${controller_name#nvme}
  for child in "$controller"/nvme*c"${controller_id}"n*; do
    [ -e "$child" ] || continue
    child_name=$(basename "$child")
    if [[ "$child_name" =~ ^nvme([0-9]+)c${controller_id}n([0-9]+)$ ]]; then
      printf '/dev/nvme%sn%s\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
      return 0
    fi
  done
  return 1
}

md0_has_namespace_members() {
  local expected actual
  expected=$(printf '%s\n' "$@" | sed 's!.*/!!' | sort)
  actual=$(find /sys/block/md0/slaves -mindepth 1 -maxdepth 1 -printf '%f\n' \
    2>/dev/null | sort)
  [ "$expected" = "$actual" ] && [ "$(printf '%s\n' "$actual" | sed '/^$/d' | wc -l)" -eq 4 ]
}

md_device_with_namespace_members() {
  local expected actual md_device
  expected=$(printf '%s\n' "$@" | sed 's!.*/!!' | sort)
  for md_device in /sys/block/md*; do
    [ -d "$md_device/slaves" ] || continue
    actual=$(find "$md_device/slaves" -mindepth 1 -maxdepth 1 \
      -printf '%f\n' | sort)
    if [ "$expected" = "$actual" ] &&
      [ "$(printf '%s\n' "$actual" | sed '/^$/d' | wc -l)" -eq 4 ]; then
      printf '/dev/%s\n' "${md_device##*/}"
      return 0
    fi
  done
  return 1
}

wait_for_md0_namespace_members() {
  local attempt
  for attempt in $(seq 1 10); do
    md0_has_namespace_members "$@" && return 0
    sleep 1
  done
  return 1
}

assemble_md0_from_namespace_members() {
  local attempt assembled_device
  for attempt in $(seq 1 10); do
    md0_has_namespace_members "$@" && return 0
    if assembled_device=$(md_device_with_namespace_members "$@"); then
    timeout -k 5 90 mdadm --stop "$assembled_device"
  elif [ -e /dev/md0 ]; then
    timeout -k 5 90 mdadm --stop /dev/md0
  fi
    timeout -k 5 90 mdadm --assemble /dev/md0 "$@" >/dev/null 2>&1 || true
    sleep 1
  done
  wait_for_md0_namespace_members "$@"
}

if [ -f "$manifest" ]; then
  while read -r nqn traddr trsvcid extra; do
    [ -n "$nqn" ] && [ -n "$traddr" ] && [ -n "$trsvcid" ] &&
      [ -z "${extra:-}" ] ||
      { echo "ABORT: invalid controller recovery manifest $manifest" >&2; exit 2; }
    nqns+=("$nqn")
    traddrs+=("$traddr")
    trsvcids+=("$trsvcid")
  done <"$manifest"
else
  while IFS= read -r controller; do
    [ -r "$controller/subsysnqn" ] || continue
    nqn=$(<"$controller/subsysnqn")
    [ "${nqn#"$nqn_prefix"}" != "$nqn" ] || continue
    address=$(<"$controller/address")
    traddr=${address#*traddr=}
    traddr=${traddr%%,*}
    trsvcid=${address#*trsvcid=}
    trsvcid=${trsvcid%%,*}
    grep -Fqx "$traddr" <(tr ',' '\n' <<<"$addresses") ||
      { echo "ABORT: refusing unexpected MMGT address $traddr" >&2; exit 2; }
    [ -n "$trsvcid" ] && [ "$trsvcid" != "$address" ] ||
      { echo "ABORT: missing trsvcid for $nqn" >&2; exit 2; }
    nqns+=("$nqn")
    traddrs+=("$traddr")
    trsvcids+=("$trsvcid")
  done < <(find /sys/class/nvme -maxdepth 1 -name 'nvme[0-9]*' | sort)
fi
[ "${#nqns[@]}" -eq 4 ] ||
  { echo "ABORT: expected exactly four MMGT controllers; found ${#nqns[@]}" >&2; exit 2; }
for index in "${!nqns[@]}"; do
  [ "${nqns[$index]#"$nqn_prefix"}" != "${nqns[$index]}" ] ||
    { echo "ABORT: recovery manifest contains unexpected NQN" >&2; exit 2; }
  grep -Fqx "${traddrs[$index]}" <(tr ',' '\n' <<<"$addresses") ||
    { echo "ABORT: recovery manifest contains unexpected address" >&2; exit 2; }
done
[ -f "$manifest" ] || {
  umask 077
  for index in "${!nqns[@]}"; do
    printf '%s %s %s\n' "${nqns[$index]}" "${traddrs[$index]}" \
      "${trsvcids[$index]}"
  done >"$manifest"
}

declare -a current_namespace_devices=()
while IFS= read -r controller; do
  [ -r "$controller/subsysnqn" ] || continue
  nqn=$(<"$controller/subsysnqn")
  [ "${nqn#"$nqn_prefix"}" != "$nqn" ] || continue
  namespace_device=$(controller_namespace_device "$controller") ||
    { echo "ABORT: MMGT controller has no namespace before reconfiguration: $controller" >&2; exit 2; }
  current_namespace_devices+=("$namespace_device")
done < <(find /sys/class/nvme -maxdepth 1 -name 'nvme[0-9]*' | sort)
[ "${#current_namespace_devices[@]}" -eq 4 ] ||
  { echo "ABORT: expected four MMGT namespace devices before reconfiguration" >&2; exit 2; }

if findmnt -rn -M "$mount" >/dev/null; then
  if fuser -m "$mount" >/dev/null 2>&1; then
    echo "ABORT: refusing to unmount active $mount" >&2
    exit 2
  fi
  timeout -k 5 90 umount "$mount"
fi
if assembled_device=$(md_device_with_namespace_members "${current_namespace_devices[@]}"); then
  timeout -k 5 90 mdadm --stop "$assembled_device"
elif [ -e /dev/md0 ]; then
  timeout -k 5 90 mdadm --stop /dev/md0
else
  echo "ABORT: no md array matches the four MMGT namespace devices" >&2
  exit 2
fi
for nqn in "${nqns[@]}"; do
  timeout -k 5 90 nvme disconnect -n "$nqn"
done

for index in "${!nqns[@]}"; do
  timeout -k 5 90 nvme connect -t rdma -n "${nqns[$index]}" -a "${traddrs[$index]}" \
    -s "${trsvcids[$index]}" --nr-io-queues "$qps"
done

declare -a namespace_devices=()
for _ in $(seq 1 "$connect_timeout_sec"); do
  matched=0
  valid=1
  namespace_devices=()
  while IFS= read -r controller; do
    [ -r "$controller/subsysnqn" ] || continue
    nqn=$(<"$controller/subsysnqn")
    [ "${nqn#"$nqn_prefix"}" != "$nqn" ] || continue
    matched=$((matched + 1))
    [ "$(<"$controller/state")" = live ] ||
      valid=0
    [ "$(<"$controller/queue_count")" -eq "$expected_queue_count" ] ||
      valid=0
    namespace_device=$(controller_namespace_device "$controller") || {
      valid=0
      continue
    }
    [ -b "$namespace_device" ] || {
      valid=0
      continue
    }
    namespace_devices+=("$namespace_device")
  done < <(find /sys/class/nvme -maxdepth 1 -name 'nvme[0-9]*' | sort)
  [ "$matched" -eq 4 ] && [ "$valid" -eq 1 ] &&
    [ "${#namespace_devices[@]}" -eq 4 ] && break
  sleep 1
done
[ "${matched:-0}" -eq 4 ] && [ "${valid:-0}" -eq 1 ] &&
  [ "${#namespace_devices[@]}" -eq 4 ] ||
  { echo "ABORT: controllers or namespaces did not return at queue_count $expected_queue_count" >&2; exit 2; }

# udev may reassemble md0 as namespaces appear. Its assembly is usable only
# when it contains the same four resolved namespace devices.
assemble_md0_from_namespace_members "${namespace_devices[@]}" ||
  { echo "ABORT: md0 members differ from the reconnected namespaces" >&2; exit 2; }
timeout -k 5 90 mount /dev/md0 "$mount"
[ "$(findmnt -rn -o TARGET -T "$mount")" = "$mount" ] ||
  { echo "ABORT: failed to remount md0 at $mount" >&2; exit 2; }
printf 'reconfigured: io_qps_per_controller=%s queue_count=%s\n' \
  "$qps" "$expected_queue_count"
REMOTE
}

reconfigure_all() {
  local qps=$1
  local addresses=$2
  local host
  for host in "${INITIATOR_HOSTS[@]}"; do
    echo "reconfigure: $host I/O QPs/controller=$qps"
    if ! reconfigure_initiator "$host" "$qps" "$addresses"; then
      echo "ABORT: controller reconfiguration failed on $host" >&2
      return 1
    fi
  done
}

remove_recovery_manifests() {
  local host
  for host in "${INITIATOR_HOSTS[@]}"; do
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" \
      "rm -f /run/lmcache-qp-scale-${RUN_ID}.controllers"
  done
}

prepare_corpus() {
  local run_id=$1
  local host
  for host in "${INITIATOR_HOSTS[@]}"; do
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" bash -s -- \
      "$run_id" "$BENCH_ROOT" "$PROFILE" "$IN_FLIGHT" "$NUM_WORKERS" <<'REMOTE'
set -euo pipefail

run_id=$1
bench_root=$2
profile=$3
in_flight=$4
num_workers=$5
for repo in /root/anthropic/LMCache "$HOME/LMCache" /root/LMCache; do
  [ -x "$repo/scripts/ipu-poc/run_model_geometry.sh" ] && break
done
[ -x "$repo/scripts/ipu-poc/run_model_geometry.sh" ] ||
  { echo "ABORT: LMCache geometry checkout not found" >&2; exit 2; }

host_name=$(hostname -s)
base_path="$bench_root/$run_id/$host_name"
[ ! -e "$base_path" ] ||
  { echo "ABORT: fresh RUN_ID required; already exists: $base_path" >&2; exit 2; }
mkdir -p "$base_path"
cd "$repo"
BASE_PATH="$base_path" PREFIX="$run_id-$host_name" \
  IN_FLIGHT="$in_flight" NUM_WORKERS="$num_workers" ROUNDS=2 \
  bash scripts/ipu-poc/run_model_geometry.sh store "$profile"
REMOTE
  done
}

run_load_window() {
  local run_id=$1
  local qps=$2
  local repetition=$3
  local host metrics_port
  local -a pids=()
  for host in "${INITIATOR_HOSTS[@]}"; do
    metrics_port=$(metrics_port_for_host "$host")
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" bash -s -- \
      "$run_id" "$BENCH_ROOT" "$PROFILE" "$IN_FLIGHT" "$NUM_WORKERS" \
      "$WARMUP_SEC" "$DURATION_SEC" "$qps" "$repetition" "$metrics_port" <<'REMOTE' &
set -euo pipefail

run_id=$1
bench_root=$2
profile=$3
in_flight=$4
num_workers=$5
warmup_sec=$6
duration_sec=$7
qps=$8
repetition=$9
metrics_port=${10}
for repo in /root/anthropic/LMCache "$HOME/LMCache" /root/LMCache; do
  [ -x "$repo/scripts/ipu-poc/run_model_geometry.sh" ] && break
done
[ -x "$repo/scripts/ipu-poc/run_model_geometry.sh" ] ||
  { echo "ABORT: LMCache geometry checkout not found" >&2; exit 2; }

host_name=$(hostname -s)
base_path="$bench_root/$run_id/$host_name"
output="$repo/results/qp-scale-${run_id}-${host_name}-qps${qps}-rep${repetition}.json"
cd "$repo"
BASE_PATH="$base_path" PREFIX="$run_id-$host_name" OUTPUT="$output" \
  IN_FLIGHT="$in_flight" NUM_WORKERS="$num_workers" \
  WARMUP_SEC="$warmup_sec" DURATION_SEC="$duration_sec" METRICS_PORT="$metrics_port" \
  bash scripts/ipu-poc/run_model_geometry.sh sustained-load "$profile"
REMOTE
    pids+=("$!")
  done

  local pid status=0
  for pid in "${pids[@]}"; do
    wait "$pid" || status=1
  done
  [ "$status" -eq 0 ] || die "load window failed at $qps I/O QPs/controller"
}

preflight_fleet_corpus() {
  local host
  ssh -o BatchMode=yes -o ConnectTimeout=10 mmgi1 \
    'test -r /tmp/measure_mmg_fleet.py && test -r /tmp/annotate_mmg_phase.py' ||
    die "fleet coordinator or phase annotator missing on mmgi1"
  for host in "${INITIATOR_HOSTS[@]}"; do
    if ! ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" bash -s -- \
      "$BENCH_ROOT" "$FLEET_CORPUS_RUN_ID" <<'REMOTE'
set -euo pipefail
base="$1/$2/$(hostname -s)"
[ -d "$base" ] && [ -n "$(find "$base" -maxdepth 1 -name '*.data' -print -quit)" ]
REMOTE
    then
      die "$host is missing the MiniMax corpus"
    fi
  done
}

require_fleet_qps_below_cpu_cap() {
  local host online qps
  for host in "${INITIATOR_HOSTS[@]}"; do
    online=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" \
      getconf _NPROCESSORS_ONLN) ||
      die "could not read online CPU count from $host"
    [[ "$online" =~ ^[1-9][0-9]*$ ]] ||
      die "$host returned an invalid online CPU count"
    for qps in $FLEET_QP_COUNTS; do
      [ "$qps" -le "$online" ] ||
        die "$host nvme_rdma limits -i $qps to $online online CPUs"
    done
  done
}

run_fleet_window() {
  local cell_id=$1
  local in_flight=$2
  local host metrics_port pid status=0
  local -a pids=()
  local fleet_output="/root/${cell_id}-if${in_flight}-fleet.json"

  ssh -o BatchMode=yes -o ConnectTimeout=10 mmgi1 \
    "python3 /tmp/measure_mmg_fleet.py --run-id '$cell_id' --in-flight '$in_flight' --duration-sec '$FLEET_DURATION_SEC' --output '$fleet_output'" \
    >"/tmp/${cell_id}-if${in_flight}-fleet.log" 2>&1 &
  local coordinator_pid=$!
  for host in "${INITIATOR_HOSTS[@]}"; do
    metrics_port=$(metrics_port_for_host "$host")
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" bash -s -- \
      "$cell_id" "$FLEET_CORPUS_RUN_ID" "$BENCH_ROOT" "$PROFILE" \
      "$in_flight" "$NUM_WORKERS" "$FLEET_WARMUP_SEC" \
      "$FLEET_DURATION_SEC" "$metrics_port" "$host" <<'REMOTE' \
      >"/tmp/${cell_id}-if${in_flight}-${host}.log" 2>&1 &
set -euo pipefail
cell_id=$1
corpus_id=$2
bench_root=$3
profile=$4
in_flight=$5
num_workers=$6
warmup=$7
duration=$8
metrics_port=$9
host=${10}
for repo in /root/anthropic/LMCache "$HOME/LMCache" /root/LMCache; do
  [ -x "$repo/scripts/ipu-poc/run_model_geometry.sh" ] && break
done
[ -x "$repo/scripts/ipu-poc/run_model_geometry.sh" ]
base_path="$bench_root/$corpus_id/$(hostname -s)"
output="/root/${cell_id}-if${in_flight}-${host}.json"
[ -d "$base_path" ] && [ ! -e "$output" ]
cd "$repo"
BASE_PATH="$base_path" PREFIX="$corpus_id" OUTPUT="$output" \
  IN_FLIGHT="$in_flight" NUM_WORKERS="$num_workers" \
  WARMUP_SEC="$warmup" DURATION_SEC="$duration" \
  METRICS_PORT="$metrics_port" \
  bash scripts/ipu-poc/run_model_geometry.sh sustained-load "$profile"
REMOTE
    pids+=("$!")
  done
  for pid in "${pids[@]}"; do
    wait "$pid" || status=1
  done
  wait "$coordinator_pid" || status=1
  [ "$status" -eq 0 ] ||
    die "fleet window failed: $cell_id in_flight=$in_flight; inspect /tmp logs"
  ssh -o BatchMode=yes -o ConnectTimeout=10 mmgi1 \
    "python3 -c 'import json; assert json.load(open(\"$fleet_output\"))[\"valid\"]'" ||
    die "fleet result invalid: $fleet_output"
  ssh -o BatchMode=yes -o ConnectTimeout=10 mmgi1 \
    "python3 /tmp/annotate_mmg_phase.py '$fleet_output'" ||
    echo "WARNING: measured region not published for $fleet_output" >&2
  echo "fleet-result: mmgi1:$fleet_output"
}

run_fio_window() {
  local run_id=$1
  local qps=$2
  local repetition=$3
  local iodepth=${4:-$FIO_IODEPTH}
  local output_suffix=${5:-}
  local host
  local -a pids=()

  for host in "${INITIATOR_HOSTS[@]}"; do
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" bash -s -- \
      "$run_id" "$WARMUP_SEC" "$DURATION_SEC" "$qps" "$repetition" \
      "$FIO_BLOCK_SIZE" "$FIO_NUMJOBS" "$iodepth" "$FIO_IOENGINE" "$output_suffix" <<'REMOTE' &
set -euo pipefail

run_id=$1
warmup_sec=$2
duration_sec=$3
qps=$4
repetition=$5
block_size=$6
numjobs=$7
iodepth=$8
ioengine=$9
output_suffix=${10}
for repo in /root/anthropic/LMCache "$HOME/LMCache" /root/LMCache; do
  [ -d "$repo/results" ] && break
done
[ -d "$repo/results" ] ||
  { echo "ABORT: LMCache results directory not found" >&2; exit 2; }

host_name=$(hostname -s)
job="/tmp/lmcache-qp-fio-${run_id}-${host_name}-qps${qps}-rep${repetition}.fio"
output="$repo/results/qp-fio-scale-${run_id}-${host_name}-qps${qps}${output_suffix}-rep${repetition}.json"
[ ! -e "$output" ] ||
  { echo "ABORT: fresh RUN_ID required; result already exists: $output" >&2; exit 2; }
trap 'rm -f "$job"' EXIT
{
  printf '[global]\n'
  printf 'ioengine=%s\n' "$ioengine"
  printf 'direct=1\n'
  printf 'time_based=1\n'
  printf 'runtime=%s\n' "$duration_sec"
  printf 'ramp_time=%s\n' "$warmup_sec"
  printf 'group_reporting=1\n'
  printf 'thread=1\n'
  printf 'numjobs=%s\n' "$numjobs"
  printf 'iodepth=%s\n' "$iodepth"
  printf 'rw=randread\n'
  printf 'bs=%s\n' "$block_size"
  printf 'norandommap=1\n'
  printf 'randrepeat=0\n'
  printf 'clat_percentiles=1\n'
  printf 'percentile_list=50:90:95:99:99.9\n'
  printf '\n[load]\n'
  printf 'filename=/dev/md0\n'
} >"$job"
fio --output-format=json --output="$output" "$job"
REMOTE
    pids+=("$!")
  done

  local pid status=0
  for pid in "${pids[@]}"; do
    wait "$pid" || status=1
  done
  [ "$status" -eq 0 ] || die "FIO window failed at $qps I/O QPs/controller"
}

restore_needed=0
restore_addresses=

stop_fio_windows() {
  local host

  for host in "${INITIATOR_HOSTS[@]}"; do
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" bash -s -- "$RUN_ID" <<'REMOTE'
set -euo pipefail

run_id=$1
pattern="/tmp/lmcache-qp-fio-${run_id}-"
mapfile -t pids < <(pgrep -f "$pattern" || true)
[ "${#pids[@]}" -eq 0 ] && exit 0
kill -TERM "${pids[@]}" 2>/dev/null || true
for _ in $(seq 1 30); do
  pgrep -f "$pattern" >/dev/null || exit 0
  sleep 1
done
pkill -KILL -f "$pattern" 2>/dev/null || true
REMOTE
  done
}

restore_baseline() {
  [ "$restore_needed" -eq 1 ] || return 0
  echo "recovery: restoring ${RESTORE_QPS} I/O QPs/controller" >&2
  reconfigure_all "$RESTORE_QPS" "$restore_addresses" || return 1
  restore_needed=0
}

cleanup() {
  local status=$?
  trap - EXIT
  stop_fio_windows || {
    echo "ABORT: could not stop one or more remote FIO jobs" >&2
    status=1
  }
  if ! restore_baseline; then
    echo "ABORT: automatic restoration failed; verify all initiators manually" >&2
    status=1
  else
    remove_recovery_manifests || {
      echo "ABORT: restored controllers but could not remove recovery manifests" >&2
      status=1
    }
  fi
  exit "$status"
}

run_sweep() {
  [ "${APPLY:-}" = 1 ] ||
    die "sweep changes controller state; rerun with APPLY=1"
  [[ "${RUN_ID:-}" =~ ^[[:alnum:]_.-]+$ ]] ||
    die "RUN_ID must contain only letters, digits, dot, dash, or underscore"
  validate_options
  preflight
  require_baseline_qps

  local addresses qps repetition
  addresses=$(target_addresses)
  [ -n "$addresses" ] ||
    die "$TARGET_HOST has no configured NVMe-oF target address"
  restore_addresses=$addresses
  trap 'cleanup' EXIT INT TERM

  echo "prepare: creating fresh MiniMax corpus for RUN_ID=$RUN_ID"
  prepare_corpus "$RUN_ID"
  for qps in $QP_COUNTS; do
    restore_needed=1
    reconfigure_all "$qps" "$addresses"
    if [ "$qps" -eq "$RESTORE_QPS" ]; then
      restore_needed=0
    fi
    for repetition in $(seq 1 "$REPETITIONS"); do
      echo "measure: qps=$qps repetition=$repetition/$REPETITIONS"
      run_load_window "$RUN_ID" "$qps" "$repetition"
    done
  done
  remove_recovery_manifests
  echo "complete: RUN_ID=$RUN_ID"
}

run_if_fleet_sweep() {
  [ "${APPLY:-}" = 1 ] ||
    die "sweep-if-fleet changes controller state; rerun with APPLY=1"
  [[ "${RUN_ID:-}" =~ ^[[:alnum:]-]+$ ]] ||
    die "RUN_ID must contain only letters, digits, and hyphens"
  [[ "$FLEET_CORPUS_RUN_ID" =~ ^[[:alnum:]-]+$ ]] ||
    die "FLEET_CORPUS_RUN_ID must contain only letters, digits, and hyphens"
  [[ "$FLEET_QP_COUNTS" =~ ^[1-9][0-9]*(\ [1-9][0-9]*)*$ ]] ||
    die "FLEET_QP_COUNTS must be positive integers"
  [[ "$FLEET_IN_FLIGHTS" =~ ^[1-9][0-9]*(\ [1-9][0-9]*)*$ ]] ||
    die "FLEET_IN_FLIGHTS must be positive integers"
  [[ "$FLEET_WARMUP_SEC" =~ ^[1-9][0-9]*$ ]] ||
    die "FLEET_WARMUP_SEC must be a positive integer"
  [[ "$FLEET_DURATION_SEC" =~ ^[1-9][0-9]*$ ]] ||
    die "FLEET_DURATION_SEC must be a positive integer"
  [[ "$FLEET_REPETITIONS" =~ ^[1-9][0-9]*$ ]] ||
    die "FLEET_REPETITIONS must be a positive integer"
  require_fleet_qps_below_cpu_cap
  validate_options
  preflight
  require_baseline_qps
  preflight_fleet_corpus

  local addresses qps in_flight repetition cell_id
  addresses=$(target_addresses)
  restore_addresses=$addresses
  trap 'cleanup' EXIT INT TERM
  for qps in $FLEET_QP_COUNTS; do
    restore_needed=1
    reconfigure_all "$qps" "$addresses"
    if [ "$qps" -eq "$RESTORE_QPS" ]; then
      restore_needed=0
    fi
    for in_flight in $FLEET_IN_FLIGHTS; do
      for repetition in $(seq 1 "$FLEET_REPETITIONS"); do
        cell_id="${RUN_ID}-qps${qps}-rep${repetition}"
        echo "measure-fleet: qps=$qps in_flight=$in_flight rep=$repetition"
        run_fleet_window "$cell_id" "$in_flight"
      done
    done
  done
  echo "complete-fleet: RUN_ID=$RUN_ID"
}

run_reconnect_fleet() {
  local qps=$1 addresses host online
  [ "${APPLY:-}" = 1 ] ||
    die "reconnect-fleet changes controller state; rerun with APPLY=1"
  [[ "${RUN_ID:-}" =~ ^[[:alnum:]-]+$ ]] ||
    die "RUN_ID must contain only letters, digits, and hyphens"
  [[ "$qps" =~ ^[1-9][0-9]*$ ]] ||
    die "QPS must be a positive integer"
  ssh() { command ssh -o StrictHostKeyChecking=yes "$@"; }
  validate_options
  for host in "${INITIATOR_HOSTS[@]}"; do
    online=$(ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" \
      getconf _NPROCESSORS_ONLN) ||
      die "could not read online CPU count from $host"
    [ "$qps" -le "$online" ] ||
      die "$host nvme_rdma limits -i $qps to $online online CPUs"
  done
  addresses=$(target_addresses)
  [ -n "$addresses" ] ||
    die "$TARGET_HOST has no configured NVMe-oF target address"
  reconfigure_all "$qps" "$addresses"
  for host in "${INITIATOR_HOSTS[@]}"; do
    ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" bash -s -- \
      "$NQN_PREFIX" "$((qps + 1))" <<'REMOTE'
set -euo pipefail
prefix=$1
expected=$2
count=0
for controller in /sys/class/nvme/nvme[0-9]*; do
  [ -r "$controller/subsysnqn" ] || continue
  nqn=$(<"$controller/subsysnqn")
  [ "${nqn#"$prefix"}" != "$nqn" ] || continue
  [ "$(<"$controller/state")" = live ] &&
    [ "$(<"$controller/queue_count")" -eq "$expected" ] ||
    { echo "ABORT: $controller did not return live at queue_count=$expected" >&2; exit 2; }
  printf '%s queue_count=%s state=live\n' "$controller" "$expected"
  count=$((count + 1))
done
[ "$count" -eq 4 ]
REMOTE
  done
  echo "complete-reconnect: RUN_ID=$RUN_ID QPS=$qps"
}

run_fio_sweep() {
  [ "${APPLY:-}" = 1 ] ||
    die "sweep-fio changes controller state; rerun with APPLY=1"
  [[ "${RUN_ID:-}" =~ ^[[:alnum:]_.-]+$ ]] ||
    die "RUN_ID must contain only letters, digits, dot, dash, or underscore"
  validate_options
  preflight_fio
  require_baseline_qps

  local addresses qps repetition
  addresses=$(target_addresses)
  [ -n "$addresses" ] ||
    die "$TARGET_HOST has no configured NVMe-oF target address"
  restore_addresses=$addresses
  trap 'cleanup' EXIT INT TERM

  for qps in $QP_COUNTS; do
    restore_needed=1
    reconfigure_all "$qps" "$addresses"
    if [ "$qps" -eq "$RESTORE_QPS" ]; then
      restore_needed=0
    fi
    for repetition in $(seq 1 "$REPETITIONS"); do
      echo "measure-fio: qps=$qps repetition=$repetition/$REPETITIONS"
      run_fio_window "$RUN_ID" "$qps" "$repetition"
    done
  done
  remove_recovery_manifests
  echo "complete-fio: RUN_ID=$RUN_ID"
}

run_fio_matrix() {
  [ "${APPLY:-}" = 1 ] ||
    die "sweep-fio-matrix changes controller state; rerun with APPLY=1"
  [[ "${RUN_ID:-}" =~ ^[[:alnum:]_.-]+$ ]] ||
    die "RUN_ID must contain only letters, digits, dot, dash, or underscore"
  validate_options
  preflight_fio
  require_baseline_qps

  local addresses qps depth repetition
  addresses=$(target_addresses)
  restore_addresses=$addresses
  trap 'cleanup' EXIT INT TERM
  for qps in 1 2 4 8 16 32; do
    restore_needed=1
    reconfigure_all "$qps" "$addresses"
    for depth in $FIO_MATRIX_DEPTHS; do
      for repetition in $(seq 1 "$REPETITIONS"); do
        run_fio_window "$RUN_ID" "$qps" "$repetition" "$depth" "-qd${depth}"
      done
    done
  done
}

case "$MODE" in
  help|-h|--help)
    usage
    ;;
  show)
    [ "$#" -eq 1 ] || { usage >&2; exit 2; }
    load_inventory "$1"
    validate_options
    show_inventory
    ;;
  show-fio)
    [ "$#" -eq 1 ] || { usage >&2; exit 2; }
    load_inventory "$1"
    validate_options
    show_fio_inventory
    ;;
  show-fio-matrix)
    [ "$#" -eq 1 ] || { usage >&2; exit 2; }
    load_inventory "$1" 1
    show_fio_matrix_inventory
    ;;
  preflight)
    [ "$#" -eq 1 ] || { usage >&2; exit 2; }
    load_inventory "$1"
    validate_options
    preflight
    ;;
  sweep)
    [ "$#" -eq 1 ] || { usage >&2; exit 2; }
    load_inventory "$1"
    run_sweep
    ;;
  sweep-if-fleet)
    [ "$#" -eq 1 ] || { usage >&2; exit 2; }
    load_inventory "$1"
    run_if_fleet_sweep
    ;;
  reconnect-fleet)
    [ "$#" -eq 2 ] || { usage >&2; exit 2; }
    load_inventory "$2"
    run_reconnect_fleet "$1"
    ;;
  sweep-fio)
    [ "$#" -eq 1 ] || { usage >&2; exit 2; }
    load_inventory "$1"
    run_fio_sweep
    ;;
  sweep-fio-matrix)
    [ "$#" -eq 1 ] || { usage >&2; exit 2; }
    load_inventory "$1" 1
    run_fio_matrix
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
