#!/usr/bin/env bash
# nvme_stats.sh — export NVMe SMART + namespace stats as node_exporter textfile.
#
# Discover namespaces without issuing identify commands to remote controllers.

set -euo pipefail

OUT_DIR="${OUT_DIR:-/var/lib/node_exporter/textfile}"
SYSFS_ROOT="${SYSFS_ROOT:-/sys}"
NVME_SMART_TIMEOUT_SECONDS="${NVME_SMART_TIMEOUT_SECONDS:-2}"
OUT_FILE="$OUT_DIR/nvme_stats.prom"
find "$OUT_DIR" -maxdepth 1 -type f -name 'nvme_stats.prom.??????' \
    -mmin +1 -delete
TMP_FILE="$(mktemp "$OUT_DIR/nvme_stats.prom.XXXXXX")"

trap 'rm -f "$TMP_FILE"' EXIT

devices=$(
    for path in "$SYSFS_ROOT"/class/block/nvme*n*; do
        [ -e "$path" ] || continue
        ns=$(basename "$path")
        if [[ "$ns" =~ ^nvme[0-9]+n[0-9]+$ ]]; then
            printf '/dev/%s\n' "$ns"
        fi
    done
)
exported_paths=$(
    for path in /sys/kernel/config/nvmet/subsystems/*/namespaces/*/device_path; do
        [ -r "$path" ] || continue
        cat "$path" 2>/dev/null || true
    done | sort -u
)

{
    success=1
    echo "# HELP nvme_smart_available 1 when SMART was read for this local namespace"
    echo "# TYPE nvme_smart_available gauge"
    echo "# HELP nvme_smart_field NVMe SMART field per namespace"
    echo "# TYPE nvme_smart_field gauge"
    for dev in $devices; do
        ns=$(basename "$dev")
        controller="${ns%n*}"
        transport=$(cat "$SYSFS_ROOT/class/nvme/$controller/transport" 2>/dev/null || true)
        # nvmet does not provide the physical drive's SMART log. A removed
        # import previously left nvme-cli spinning indefinitely in this unit.
        if [ "$transport" != pcie ]; then
            printf 'nvme_smart_available{ns="%s"} 0\n' "$ns"
            continue
        fi
        if ! smart=$(timeout --kill-after=1s "$NVME_SMART_TIMEOUT_SECONDS" \
            nvme smart-log "$dev" --timeout=2000 -o json 2>/dev/null); then
            success=0
            printf 'nvme_smart_available{ns="%s"} 0\n' "$ns"
            continue
        fi
        if ! fields=$(jq -er --arg ns "$ns" '
            to_entries[]
            | select(.value | type == "number")
            | "nvme_smart_field{ns=\"" + $ns + "\",field=\"" + .key + "\"} " + (.value|tostring)
        ' <<<"$smart"); then
            success=0
            printf 'nvme_smart_available{ns="%s"} 0\n' "$ns"
            continue
        fi
        printf 'nvme_smart_available{ns="%s"} 1\n%s\n' "$ns" "$fields"
    done

    # Namespace role, so dashboards can select "the imported NVMe-oF namespaces"
    # without hardcoding kernel names. Those names are NOT stable: across the
    # 2026-08-12 reboot mkp1's imports came back as nvme0n1/nvme3n1 having been
    # nvme2n1/nvme3n1, and the dashboard's hardcoded `nvme2n1|nvme3n2` regex
    # matched one stale name plus one device that has never existed -- so the
    # initiator panels silently plotted a local idle PM9A3 instead of the fabric.
    #
    # The target configfs export is authoritative after an NVMe re-enumeration:
    # device names can change, but its namespace device_path follows the live
    # backing device. Otherwise, controller transport=pcie means locally
    # attached; fabric controllers carry the target's subsystem NQN.
    echo "# HELP nvme_namespace_role 1 for each namespace, labelled by attachment"
    echo "# TYPE nvme_namespace_role gauge"
    for dev in $devices; do
        ns=$(basename "$dev")
        controller="${ns%n*}"
        transport=$(cat "$SYSFS_ROOT/class/nvme/$controller/transport" 2>/dev/null || true)
        nqn=$(cat "$SYSFS_ROOT/class/block/$ns/device/subsysnqn" 2>/dev/null || true)
        if grep -Fxq "$dev" <<<"$exported_paths"; then
            role=exported
        elif [ "$transport" = "pcie" ]; then
            role=local
        else
            role=imported
        fi
        printf 'nvme_namespace_role{ns="%s",role="%s",subsysnqn="%s"} 1\n' \
            "$ns" "$role" "$nqn"
    done
    echo "# HELP nvme_smart_collector_success 1 when all attempted local SMART reads succeeded"
    echo "# TYPE nvme_smart_collector_success gauge"
    printf 'nvme_smart_collector_success %s\n' "$success"
    echo "# HELP nvme_smart_collector_timestamp_seconds Unix timestamp of the collector attempt"
    echo "# TYPE nvme_smart_collector_timestamp_seconds gauge"
    printf 'nvme_smart_collector_timestamp_seconds %s\n' "$(date +%s)"
} > "$TMP_FILE"

chmod 0644 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
trap - EXIT
