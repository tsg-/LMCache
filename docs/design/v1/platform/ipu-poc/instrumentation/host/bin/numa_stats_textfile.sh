#!/usr/bin/env bash
# Export node-local memory and NUMA allocation counters for node_exporter.

set -euo pipefail

OUT_DIR="${OUT_DIR:-/var/lib/node_exporter/textfile}"
OUT_FILE="$OUT_DIR/numa_stats.prom"
TMP_FILE="$(mktemp "$OUT_DIR/numa_stats.prom.XXXXXX")"
BODY_FILE="$(mktemp /tmp/numa_stats.XXXXXX.prom)"
SYS_NODE_DIR="${SYS_NODE_DIR:-/sys/devices/system/node}"
PROC_STAT="${PROC_STAT:-/proc/stat}"
CLK_TCK="${CLK_TCK:-$(getconf CLK_TCK)}"

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
    echo "# HELP numa_node_cpu_seconds_total CPU time summed over CPUs assigned to one NUMA node"
    echo "# TYPE numa_node_cpu_seconds_total counter"
    for node_path in "${nodes[@]}"; do
        node="${node_path##*node}"
        meminfo="$node_path/meminfo"
        numastat="$node_path/numastat"
        cpulist="$node_path/cpulist"
        [ -r "$meminfo" ] && [ -r "$numastat" ] && [ -r "$cpulist" ] || {
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

        awk -v node="$node" -v cpulist="$(<"$cpulist")" -v hz="$CLK_TCK" '
            BEGIN {
                if (hz !~ /^[1-9][0-9]*$/) {
                    exit 1
                }
                split(cpulist, ranges, ",")
                for (range_index in ranges) {
                    split(ranges[range_index], bounds, "-")
                    if (length(bounds) == 1) {
                        cpus[bounds[1]] = 1
                    } else {
                        for (cpu = bounds[1]; cpu <= bounds[2]; cpu++) {
                            cpus[cpu] = 1
                        }
                    }
                }
                split("user nice system idle iowait irq softirq steal guest guest_nice",
                      modes, " ")
                for (i = 1; i <= 10; i++) {
                    totals[modes[i]] = 0
                }
            }
            $1 ~ /^cpu[0-9]+$/ {
                cpu = substr($1, 4)
                if (!(cpu in cpus)) {
                    next
                }
                found = 1
                for (i = 2; i <= NF && i <= 11; i++) {
                    totals[modes[i - 1]] += $i
                }
            }
            END {
                if (!found) {
                    exit 1
                }
                for (i = 1; i <= 10; i++) {
                    mode = modes[i]
                    printf "numa_node_cpu_seconds_total{node=\"%s\",mode=\"%s\"} %.6f\n", node, mode, totals[mode] / hz
                }
            }
        ' "$PROC_STAT" || return 1
    done
}

nodes=("$SYS_NODE_DIR"/node[0-9]*)
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
