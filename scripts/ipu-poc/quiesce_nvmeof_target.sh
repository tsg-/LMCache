#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
# Quiesce or restore the mkp1 side of the NVMe-oF path, so the local_raw fio
# rung can be measured on mkp2's own media without the exported namespaces being
# served concurrently.
#
# Order matters in one direction only: the RAID0 sits on the imported namespaces
# and XFS sits on the RAID0, so a disconnect underneath a mounted md0 produces
# I/O errors rather than a clean detach. Unmount, stop the array, THEN disconnect.
# Restore runs the same three steps in reverse.
#
# The reconnect is by explicit NQN, matching /root/mkp1-wire/run_wire.sh. The
# target exposes no discovery subsystem -- `nvme discover` against it hangs -- so
# do not substitute a discovery-driven connect here.
#
# Usage (on mkp1, as root):
#   ./quiesce_nvmeof_target.sh down    # unmount, stop md0, disconnect
#   ./quiesce_nvmeof_target.sh up      # connect, assemble md0, mount, verify
#   ./quiesce_nvmeof_target.sh status
set -uo pipefail

TARGET=${TARGET:-200.0.0.37}
PORT=${PORT:-4420}
NQN1=${NQN1:-mkp2-nvme1}
NQN2=${NQN2:-mkp2-nvme2}
# The default (128) exhausts this rig's irdma resources when both
# controllers attach. Keep the known working count explicit and overridable.
IO_QUEUES=${IO_QUEUES:-16}
MD=${MD:-/dev/md0}
MOUNT=${MOUNT:-/mnt/lmcache-stage2}
CORPUS=${CORPUS:-$MOUNT/kvcache}
# The corpus file count is the restore gate: the point of the window is that the
# bench l2 read corpus survives it untouched. Recorded 2026-08-11 before the
# window; override only if the corpus is intentionally regenerated.
EXPECT_CORPUS=${EXPECT_CORPUS:-874592}

corpus_count() { find "$CORPUS" -name '*.data' 2>/dev/null | wc -l; }

status() {
  echo "mount:      $(mount | grep -c " $MOUNT ") entry/entries"
  echo "md:         $(grep -c '^md0 ' /proc/mdstat) active"
  echo "namespaces: $(nvme list-subsys 2>/dev/null | grep -cE "NQN=($NQN1|$NQN2)\$") connected"
  if mount | grep -q " $MOUNT "; then
    echo "corpus:     $(corpus_count) files (expect $EXPECT_CORPUS)"
  fi
}

down() {
  if fuser -m "$MOUNT" >/dev/null 2>&1; then
    echo "ABORT: processes are using $MOUNT; stop them first"
    fuser -mv "$MOUNT"
    exit 1
  fi
  if mount | grep -q " $MOUNT "; then
    umount "$MOUNT" || { echo "ABORT: umount $MOUNT failed"; exit 1; }
    echo "unmounted $MOUNT"
  fi
  if [ -e "$MD" ]; then
    mdadm --stop "$MD" || { echo "ABORT: mdadm --stop $MD failed"; exit 1; }
    echo "stopped $MD"
  fi
  for nqn in "$NQN1" "$NQN2"; do
    nvme disconnect -n "$nqn" || { echo "ABORT: disconnect $nqn failed"; exit 1; }
  done
  # A disconnect that returns success but leaves a controller behind would let the
  # local_raw sweep run against a still-exported device, which is the exact
  # contamination the window exists to remove.
  sleep 2
  remaining=$(nvme list-subsys 2>/dev/null | grep -cE "NQN=($NQN1|$NQN2)\$")
  [ "$remaining" -eq 0 ] || { echo "ABORT: $remaining namespace(s) still connected"; exit 1; }
  echo "disconnected; mkp2 media is now idle"
}

up() {
  for nqn in "$NQN1" "$NQN2"; do
    nvme connect -t rdma -n "$nqn" -a "$TARGET" -s "$PORT" \
      --nr-io-queues="$IO_QUEUES" ||
      { echo "ABORT: connect $nqn failed"; exit 1; }
  done
  sleep 3
  connected=$(nvme list-subsys 2>/dev/null | grep -cE "NQN=($NQN1|$NQN2)\$")
  [ "$connected" -eq 2 ] || { echo "ABORT: expected 2 namespaces, got $connected"; exit 1; }

  # Assemble by UUID rather than by device name: the imported namespaces are not
  # guaranteed to come back as the same nvmeXnY they left as.
  mdadm --assemble --scan || true
  [ -e "$MD" ] || { echo "ABORT: $MD did not assemble"; cat /proc/mdstat; exit 1; }

  mount "$MD" "$MOUNT" || { echo "ABORT: mount $MD on $MOUNT failed"; exit 1; }
  found=$(corpus_count)
  [ "$found" -eq "$EXPECT_CORPUS" ] ||
    { echo "ABORT: corpus is $found files, expected $EXPECT_CORPUS"; exit 1; }
  echo "restored: $MD mounted on $MOUNT, corpus $found files intact"
  for c in $(ls -1 /sys/class/nvme 2>/dev/null); do
    nqn=$(cat "/sys/class/nvme/$c/subsysnqn" 2>/dev/null)
    case $nqn in
      "$NQN1"|"$NQN2")
        echo "  $c queues=$(cat "/sys/class/nvme/$c/queue_count" 2>/dev/null) $nqn" ;;
    esac
  done
}

case ${1:-} in
  down) down ;;
  up) up ;;
  status) status ;;
  *) echo "usage: $0 down|up|status"; exit 2 ;;
esac
