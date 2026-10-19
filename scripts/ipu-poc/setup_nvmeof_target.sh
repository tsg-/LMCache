#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
#
# Build (or tear down) the mkp2 NVMe-oF target export over Falcon RDMA.
#
# This exists because the export lives entirely in configfs, which does NOT
# survive a reboot. It was hand-built twice (2026-08-02 and 2026-08-12) and lost
# both times, costing a bring-up session each time. Run this instead.
#
# Two invariants this script enforces that a by-hand rebuild kept getting wrong:
#
# 1. NAMESPACES ARE BOUND BY SERIAL, NOT BY DEVICE NAME. The PM9A3 kernel names
#    are not stable across reboots -- on 2026-08-12 the pair came back as
#    nvme1n1/nvme2n1 having been nvme0n1/nvme1n1 before, with the serials in the
#    OPPOSITE order. Binding mkp2-nvme1 to "/dev/nvme0n1" therefore silently
#    exports a different physical drive after a reboot. The RAID0 superblock
#    carries its own device roles so the corpus survives a swap, but the two
#    subsystems would no longer mean what the results docs say they mean.
#
# 2. NO LOCAL md ARRAY MAY HOLD THESE DRIVES. The RAID0 that sits on the imported
#    namespaces was created ON MKP1, so its superblock lives on mkp2's physical
#    media. With no mdadm.conf here, mkp2's boot-time scan finds that superblock
#    and assembles it locally (as /dev/md127). If nvmet then exports the same
#    drives, they have two independent owners -- mkp2's md layer and mkp1's
#    imported md0 -- which is a corpus-corruption path. Observed on both reboots.
#
# Usage (on the target host, as root). The drive serials and the initiator's
# host NQN identify one specific chassis, so both are inputs, not defaults:
#   INITIATOR_NQN=$(ssh <initiator> cat /etc/nvme/hostnqn) \
#   SUBSYS_SERIALS="target-nvme1=<serialA> target-nvme2=<serialB>" \
#   TRADDR=<fabric-ip> ./setup_nvmeof_target.sh up
#   ./setup_nvmeof_target.sh down     # unexport (leaves modules loaded)
#   ./setup_nvmeof_target.sh status
# down and status walk the same subsystems, so they need the same two inputs.
#
# Bring the INITIATOR up separately, from mkp1: quiesce_nvmeof_target.sh up
set -uo pipefail

NVMET=/sys/kernel/config/nvmet
TRADDR=${TRADDR:-200.0.0.37}
PORT_ID=${PORT_ID:-1}
TRSVCID=${TRSVCID:-4420}

# The initiator's host NQN. attr_allow_any_host stays 0 and this is the only
# entry, so an unexpected initiator is refused rather than silently served.
# Read it on the initiator with `cat /etc/nvme/hostnqn`.
INITIATOR_NQN=${INITIATOR_NQN:-}
[ -n "$INITIATOR_NQN" ] ||
  { echo "ABORT: set INITIATOR_NQN to the initiator's /etc/nvme/hostnqn"; exit 1; }

# subsystem name -> drive serial, from SUBSYS_SERIALS as a space-separated list
# of name=serial pairs. Kept out of this file because the serials identify one
# specific chassis; the binding contract is by serial (invariant 1), not the
# particular serials. Read them with `nvme list -o json`.
#   SUBSYS_SERIALS="target-nvme1=<serialA> target-nvme2=<serialB>"
# Order is the naming contract the results docs are written against, so keep a
# host's list stable once a corpus exists on it.
declare -A SUBSYS_SERIAL=()
for pair in ${SUBSYS_SERIALS:-}; do
  case $pair in
    *=*) SUBSYS_SERIAL[${pair%%=*}]=${pair#*=} ;;
    *) echo "ABORT: SUBSYS_SERIALS entry is not name=serial: $pair"; exit 1 ;;
  esac
done
[ "${#SUBSYS_SERIAL[@]}" -gt 0 ] ||
  { echo "ABORT: set SUBSYS_SERIALS=\"name=serial [name=serial ...]\""; exit 1; }

# Resolve a serial to its /dev/disk/by-id symlink. by-id is used rather than a
# bare /dev/nvmeXnY so the export survives a rename; the serial is in the link
# name, which is what makes the binding auditable after the fact.
by_id_for_serial() {
  local serial=$1
  local link
  for link in /dev/disk/by-id/nvme-*_"$serial"; do
    # The kernel publishes both "..._<serial>" and "..._<serial>_1" for the same
    # device. Either resolves identically; take the first and skip the _1 dupe.
    case $link in *_1) continue ;; esac
    [ -e "$link" ] || continue
    echo "$link"
    return 0
  done
  return 1
}

# A drive claimed by a local md array must not be exported -- see invariant 2.
assert_no_local_md() {
  local arrays
  arrays=$(grep -oE '^md[0-9]+' /proc/mdstat 2>/dev/null)
  [ -z "$arrays" ] && return 0
  echo "ABORT: local md array(s) are assembled on this host: $arrays"
  echo "These drives belong to mkp1's array; exporting them while mkp2 also owns"
  echo "them risks corrupting the corpus. Stop them first:"
  local md
  for md in $arrays; do echo "  mdadm --stop /dev/$md"; done
  exit 1
}

status() {
  echo -n "modules:    "
  # NOT `lsmod | grep -q`: under `set -o pipefail`, grep -q exits on its first
  # match while lsmod is still writing, lsmod takes SIGPIPE, and the pipeline
  # reports failure even though the module IS loaded. Count instead so the
  # producer always runs to completion.
  if [ "$(lsmod | grep -cE '^nvmet_rdma ')" -gt 0 ]; then
    echo "nvmet_rdma loaded"
  else
    echo "nvmet_rdma NOT loaded"
  fi
  if [ ! -d "$NVMET" ]; then
    echo "configfs:   $NVMET absent (nothing exported)"
    return 0
  fi
  local subsys
  for subsys in "${!SUBSYS_SERIAL[@]}"; do
    local dir=$NVMET/subsystems/$subsys
    if [ ! -d "$dir" ]; then
      echo "$subsys:  absent"
      continue
    fi
    printf '%s:  dev=%s enabled=%s allow_any=%s hosts=%s\n' \
      "$subsys" \
      "$(cat "$dir/namespaces/1/device_path" 2>/dev/null)" \
      "$(cat "$dir/namespaces/1/enable" 2>/dev/null)" \
      "$(cat "$dir/attr_allow_any_host" 2>/dev/null)" \
      "$(ls "$dir/allowed_hosts/" 2>/dev/null | tr '\n' ' ')"
  done
  local pdir=$NVMET/ports/$PORT_ID
  if [ -d "$pdir" ]; then
    printf 'port %s:     %s:%s trtype=%s adrfam=%s subsys=%s\n' \
      "$PORT_ID" \
      "$(cat "$pdir/addr_traddr" 2>/dev/null)" \
      "$(cat "$pdir/addr_trsvcid" 2>/dev/null)" \
      "$(cat "$pdir/addr_trtype" 2>/dev/null)" \
      "$(cat "$pdir/addr_adrfam" 2>/dev/null)" \
      "$(ls "$pdir/subsystems/" 2>/dev/null | tr '\n' ' ')"
  else
    echo "port $PORT_ID:     absent"
  fi
}

up() {
  # An RDMA device must already exist: nvmet_rdma binds the port to it, and a
  # bind against a Falcon stack whose rtcmd is not running fails at connect time
  # rather than here, which is a much more confusing failure. Checking now turns
  # a late -ECONNRESET into an early, explicit message.
  # Counted rather than `grep -q` for the SIGPIPE/pipefail reason described in
  # status(); here a false negative would abort a bring-up that should succeed.
  [ "$(ibv_devices 2>/dev/null | grep -c rocep)" -gt 0 ] ||
    { echo "ABORT: no RDMA device present. Falcon is not up (check rtcmd)."; exit 1; }

  assert_no_local_md

  modprobe nvmet || { echo "ABORT: modprobe nvmet failed"; exit 1; }
  modprobe nvmet_rdma || { echo "ABORT: modprobe nvmet_rdma failed"; exit 1; }
  [ -d "$NVMET" ] || { echo "ABORT: $NVMET missing after modprobe"; exit 1; }

  mkdir -p "$NVMET/hosts/$INITIATOR_NQN" ||
    { echo "ABORT: could not create host $INITIATOR_NQN"; exit 1; }

  local subsys serial dev dir
  for subsys in "${!SUBSYS_SERIAL[@]}"; do
    serial=${SUBSYS_SERIAL[$subsys]}
    dev=$(by_id_for_serial "$serial") ||
      { echo "ABORT: no device found for serial $serial"; exit 1; }
    dir=$NVMET/subsystems/$subsys

    mkdir -p "$dir" || { echo "ABORT: could not create subsystem $subsys"; exit 1; }
    echo 0 > "$dir/attr_allow_any_host"
    ln -sf "$NVMET/hosts/$INITIATOR_NQN" "$dir/allowed_hosts/$INITIATOR_NQN" 2>/dev/null

    mkdir -p "$dir/namespaces/1" ||
      { echo "ABORT: could not create namespace on $subsys"; exit 1; }
    # device_path is rejected while the namespace is enabled, so disable first to
    # make a re-run idempotent rather than a no-op that leaves a stale binding.
    echo 0 > "$dir/namespaces/1/enable" 2>/dev/null
    echo -n "$dev" > "$dir/namespaces/1/device_path" ||
      { echo "ABORT: could not bind $dev to $subsys"; exit 1; }
    echo 1 > "$dir/namespaces/1/enable" ||
      { echo "ABORT: could not enable namespace on $subsys"; exit 1; }
    echo "exported $subsys -> $dev (serial $serial)"
  done

  local pdir=$NVMET/ports/$PORT_ID
  mkdir -p "$pdir" || { echo "ABORT: could not create port $PORT_ID"; exit 1; }
  # Port address attributes are immutable once a subsystem is linked, so set them
  # before linking. On a re-run they are already correct and these are no-ops.
  echo -n ipv4 > "$pdir/addr_adrfam" 2>/dev/null
  echo -n rdma > "$pdir/addr_trtype" 2>/dev/null
  echo -n "$TRADDR" > "$pdir/addr_traddr" 2>/dev/null
  echo -n "$TRSVCID" > "$pdir/addr_trsvcid" 2>/dev/null
  for subsys in "${!SUBSYS_SERIAL[@]}"; do
    ln -sf "$NVMET/subsystems/$subsys" "$pdir/subsystems/$subsys" 2>/dev/null
  done

  # Verify rather than trust: a port whose attributes silently failed to take
  # would listen on the wrong address and the initiator would just time out.
  local got_traddr got_trtype
  got_traddr=$(cat "$pdir/addr_traddr" 2>/dev/null)
  got_trtype=$(cat "$pdir/addr_trtype" 2>/dev/null)
  [ "$got_traddr" = "$TRADDR" ] ||
    { echo "ABORT: port traddr is '$got_traddr', expected '$TRADDR'"; exit 1; }
  [ "$got_trtype" = rdma ] ||
    { echo "ABORT: port trtype is '$got_trtype', expected 'rdma'"; exit 1; }
  local linked
  linked=$(ls "$pdir/subsystems/" 2>/dev/null | wc -l)
  [ "$linked" -eq "${#SUBSYS_SERIAL[@]}" ] ||
    { echo "ABORT: $linked subsystem(s) linked to port, expected ${#SUBSYS_SERIAL[@]}"; exit 1; }

  echo "target up on $TRADDR:$TRSVCID (rdma), ${#SUBSYS_SERIAL[@]} subsystems"
}

down() {
  [ -d "$NVMET" ] || { echo "nothing to do: $NVMET absent"; return 0; }
  local pdir=$NVMET/ports/$PORT_ID
  local subsys
  if [ -d "$pdir" ]; then
    for subsys in "${!SUBSYS_SERIAL[@]}"; do
      rm -f "$pdir/subsystems/$subsys"
    done
    rmdir "$pdir" 2>/dev/null
  fi
  for subsys in "${!SUBSYS_SERIAL[@]}"; do
    local dir=$NVMET/subsystems/$subsys
    [ -d "$dir" ] || continue
    if [ -d "$dir/namespaces/1" ]; then
      echo 0 > "$dir/namespaces/1/enable" 2>/dev/null
      rmdir "$dir/namespaces/1" 2>/dev/null
    fi
    rm -f "$dir/allowed_hosts/$INITIATOR_NQN"
    rmdir "$dir" 2>/dev/null
  done
  rmdir "$NVMET/hosts/$INITIATOR_NQN" 2>/dev/null
  echo "target down"
}

case ${1:-} in
  up) up ;;
  down) down ;;
  status) status ;;
  *) echo "usage: $0 up|down|status"; exit 2 ;;
esac
