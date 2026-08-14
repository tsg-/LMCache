#!/usr/bin/env bash
# Export one Intel PCM memory-bandwidth sample for node_exporter.
#
# pcm-memory programs uncore PMUs for the sample interval, then restores them.
# Keep this separate from NUMA kernel statistics so PCM availability never hides
# locality telemetry.

set -euo pipefail

OUT_DIR="/var/lib/node_exporter/textfile"
OUT_FILE="$OUT_DIR/pcm_memory.prom"
TMP_FILE="$(mktemp "$OUT_DIR/pcm_memory.prom.XXXXXX")"
CSV_FILE="$(mktemp /tmp/pcm_memory.XXXXXX.csv)"
PCM_MEMORY_BIN="${PCM_MEMORY_BIN:-/usr/sbin/pcm-memory}"

trap 'rm -f "$TMP_FILE" "$CSV_FILE"' EXIT

publish_failure() {
    {
        echo "# HELP pcm_memory_collector_success 1 when the latest PCM sample succeeded"
        echo "# TYPE pcm_memory_collector_success gauge"
        echo "pcm_memory_collector_success 0"
        echo "# HELP pcm_memory_collector_timestamp_seconds Unix timestamp of the collector attempt"
        echo "# TYPE pcm_memory_collector_timestamp_seconds gauge"
        printf 'pcm_memory_collector_timestamp_seconds %s\n' "$(date +%s)"
    } > "$TMP_FILE"
    chmod 0644 "$TMP_FILE"
    mv "$TMP_FILE" "$OUT_FILE"
}

if [ ! -x "$PCM_MEMORY_BIN" ]; then
    publish_failure
    exit 1
fi

# A one-second interval gives PCM enough time to form a real bandwidth sample.
if ! "$PCM_MEMORY_BIN" 1 -nc "-csv=$CSV_FILE" -i=1 >/dev/null 2>&1; then
    publish_failure
    exit 1
fi

if ! samples="$(
    awk -F, '
        function trim(value) {
            gsub(/^[[:space:]]+|[[:space:]]+$/, "", value)
            return value
        }
        NR == 1 {
            for (i = 1; i <= NF; i++) {
                socket[i] = trim($i)
            }
            next
        }
        NR == 2 {
            for (i = 1; i <= NF; i++) {
                metric[i] = trim($i)
                gsub(/[[:space:]]/, "", metric[i])
            }
            next
        }
        NR == 3 {
            count = 0
            for (i = 1; i <= NF; i++) {
                if (socket[i] !~ /^SKT[0-9]+$/) {
                    continue
                }
                if (metric[i] == "MemRead(MB/s)") {
                    direction = "read"
                } else if (metric[i] == "MemWrite(MB/s)") {
                    direction = "write"
                } else {
                    continue
                }
                value = trim($i)
                if (value !~ /^[0-9]+([.][0-9]+)?$/) {
                    exit 3
                }
                socket_id = socket[i]
                sub(/^SKT/, "", socket_id)
                printf "pcm_memory_bandwidth_megabytes_per_second{socket=\"%s\",direction=\"%s\"} %s\n", socket_id, direction, value
                count++
            }
            if (count == 0) {
                exit 4
            }
            next
        }
        END {
            if (NR < 3) {
                exit 5
            }
        }
    ' "$CSV_FILE"
)"; then
    publish_failure
    exit 1
fi

{
    echo "# HELP pcm_memory_bandwidth_megabytes_per_second PCM socket DRAM bandwidth"
    echo "# TYPE pcm_memory_bandwidth_megabytes_per_second gauge"
    printf '%s\n' "$samples"
    echo "# HELP pcm_memory_collector_success 1 when the latest PCM sample succeeded"
    echo "# TYPE pcm_memory_collector_success gauge"
    echo "pcm_memory_collector_success 1"
    echo "# HELP pcm_memory_collector_timestamp_seconds Unix timestamp of the latest PCM sample"
    echo "# TYPE pcm_memory_collector_timestamp_seconds gauge"
    printf 'pcm_memory_collector_timestamp_seconds %s\n' "$(date +%s)"
} > "$TMP_FILE"

chmod 0644 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
