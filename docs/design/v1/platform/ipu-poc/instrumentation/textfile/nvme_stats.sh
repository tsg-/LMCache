#!/usr/bin/env bash
# nvme_stats.sh — export NVMe SMART + namespace stats as node_exporter textfile.
#
# Run from cron @ 30s. Devices auto-discovered via `nvme list`.

set -euo pipefail

OUT_DIR="/var/lib/node_exporter/textfile"
OUT_FILE="$OUT_DIR/nvme_stats.prom"
TMP_FILE="$(mktemp "$OUT_DIR/nvme_stats.prom.XXXXXX")"

trap 'rm -f "$TMP_FILE"' EXIT

devices=$(nvme list -o json 2>/dev/null | jq -r '.Devices[].DevicePath // empty')

{
    echo "# HELP nvme_smart_field NVMe SMART field per namespace"
    echo "# TYPE nvme_smart_field gauge"
    for dev in $devices; do
        ns=$(basename "$dev")
        nvme smart-log "$dev" -o json 2>/dev/null | jq -r --arg ns "$ns" '
            to_entries[]
            | select(.value | type == "number")
            | "nvme_smart_field{ns=\"" + $ns + "\",field=\"" + .key + "\"} " + (.value|tostring)
        '
    done
} > "$TMP_FILE"

mv "$TMP_FILE" "$OUT_FILE"
trap - EXIT
