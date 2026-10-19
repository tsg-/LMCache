#!/usr/bin/env bash
# Continuously mirror the ACC capture logs (acc_capture_runbook.sh output)
# from mmgt/mmgi0/mmgi1 down to this laptop, so a live local copy always
# exists -- doesn't depend on remembering to pull it after a crash, and
# survives even if the remote host reboots and loses /root/captures-*.
#
# Usage: ./sync_capture_bundle.sh [outdir] [interval-seconds]
set -euo pipefail

OUTDIR="${1:-$HOME/mkp-instrumentation/capture-mirror}"
INTERVAL="${2:-60}"
mkdir -p "$OUTDIR"

echo "Mirroring to $OUTDIR every ${INTERVAL}s. Ctrl-C to stop."

while true; do
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  for spec in \
      "mmgt:/root/captures-acc1/acc-console-live.log:mmgt-acc1-console-live.log" \
      "mmgt:/root/captures-acc2/acc-console-live.log:mmgt-acc2-console-live.log" \
      "mmgi0:/root/captures-acc/acc-console-live.log:mmgi0-acc-console-live.log" \
      "mmgi1:/root/captures-acc/acc-console-live.log:mmgi1-acc-console-live.log" \
  ; do
    host="${spec%%:*}"
    rest="${spec#*:}"
    remote_path="${rest%%:*}"
    local_name="${rest#*:}"
    scp -q "${host}:${remote_path}" "${OUTDIR}/${local_name}" 2>>"${OUTDIR}/sync-errors.log" \
      || echo "$ts  scp failed: $spec" >> "${OUTDIR}/sync-errors.log"
  done
  echo "$ts  synced" >> "${OUTDIR}/sync.log"
  sleep "$INTERVAL"
done
