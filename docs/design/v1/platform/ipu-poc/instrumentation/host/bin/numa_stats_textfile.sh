#!/usr/bin/env bash
# Export node-local memory and NUMA allocation counters for node_exporter.

set -euo pipefail

OUT_DIR="/var/lib/node_exporter/textfile"
OUT_FILE="$OUT_DIR/numa_stats.prom"
TMP_FILE="$(mktemp "$OUT_DIR/numa_stats.prom.XXXXXX")"
BODY_FILE="$(mktemp /tmp/numa_stats.XXXXXX.prom)"

trap 'rm -f "$TMP_FILE" "$BODY_FILE"' EXIT

publish_failure() {
    {
        echo "# HELP numa_collector_success 1 when the latest NUMA sample succeeded"
        echo "# TYPE numa_collector_success gauge"
        echo "numa_collector_success 0"
        echo "# HELP numa_collector_timestamp_seconds Unix timestamp of the collector attempt"
        echo "# TYPE numa_collector_timestamp_seconds gauge"
        printf 'numa_collector_timestamp_seconds %s\n' "$(date +%s)"
    } > "$TMP_FILE"
    chmod 0644 "$TMP_FILE"
    mv "$TMP_FILE" "$OUT_FILE"
}

collect() {
    echo "# HELP numa_node_memory_bytes Node-local memory reported by the kernel"
    echo "# TYPE numa_node_memory_bytes gauge"
    echo "# HELP numa_node_stat_total Node-local cumulative NUMA allocation counter"
    echo "# TYPE numa_node_stat_total counter"
    for node_path in "${nodes[@]}"; do
        node="${node_path##*node}"
        meminfo="$node_path/meminfo"
        numastat="$node_path/numastat"
        [ -r "$meminfo" ] && [ -r "$numastat" ] || {
            return 1
        }

        for pair in "MemTotal total" "MemFree free" "MemUsed used"; do
            read -r field state <<< "$pair"
            value="$(awk -v node="$node" -v field="$field:" \
                '$1 == "Node" && $2 == node && $3 == field { print $4; exit }' \
                "$meminfo")"
            case "$value" in
                ''|*[!0-9]*) return 1 ;;
            esac
            printf 'numa_node_memory_bytes{node="%s",state="%s"} %s\n' \
                "$node" "$state" "$((value * 1024))"
        done

        for counter in numa_hit numa_miss numa_foreign local_node other_node; do
            value="$(awk -v counter="$counter" '$1 == counter { print $2; exit }' \
                "$numastat")"
            case "$value" in
                ''|*[!0-9]*) return 1 ;;
            esac
            printf 'numa_node_stat_total{node="%s",counter="%s"} %s\n' \
                "$node" "$counter" "$value"
        done
    done
}

nodes=(/sys/devices/system/node/node[0-9]*)
if [ ! -d "${nodes[0]}" ] || ! collect > "$BODY_FILE"; then
    publish_failure
    exit 1
fi

{
    cat "$BODY_FILE"
    echo "# HELP numa_collector_success 1 when the latest NUMA sample succeeded"
    echo "# TYPE numa_collector_success gauge"
    echo "numa_collector_success 1"
    echo "# HELP numa_collector_timestamp_seconds Unix timestamp of the latest NUMA sample"
    echo "# TYPE numa_collector_timestamp_seconds gauge"
    printf 'numa_collector_timestamp_seconds %s\n' "$(date +%s)"
} > "$TMP_FILE"

chmod 0644 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
