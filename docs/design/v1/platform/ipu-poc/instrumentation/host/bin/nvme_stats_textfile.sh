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

    # Namespace role, so dashboards can select "the imported NVMe-oF namespaces"
    # without hardcoding kernel names. Those names are NOT stable: across the
    # 2026-08-12 reboot mkp1's imports came back as nvme0n1/nvme3n1 having been
    # nvme2n1/nvme3n1, and the dashboard's hardcoded `nvme2n1|nvme3n2` regex
    # matched one stale name plus one device that has never existed -- so the
    # initiator panels silently plotted a local idle PM9A3 instead of the fabric.
    #
    # transport=pcie means locally attached; an NVMe-oF import has no transport
    # file and carries the target's subsystem NQN instead.
    echo "# HELP nvme_namespace_role 1 for each namespace, labelled by attachment"
    echo "# TYPE nvme_namespace_role gauge"
    for dev in $devices; do
        ns=$(basename "$dev")
        transport=$(cat "/sys/block/$ns/device/transport" 2>/dev/null || echo "")
        nqn=$(cat "/sys/block/$ns/device/subsysnqn" 2>/dev/null || echo "")
        if [ "$transport" = "pcie" ]; then
            role=local
        else
            role=imported
        fi
        printf 'nvme_namespace_role{ns="%s",role="%s",subsysnqn="%s"} 1\n' \
            "$ns" "$role" "$nqn"
    done
} > "$TMP_FILE"

chmod 0644 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
trap - EXIT
