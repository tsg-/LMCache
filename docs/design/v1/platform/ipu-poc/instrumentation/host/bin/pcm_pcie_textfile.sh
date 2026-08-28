#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="/var/lib/node_exporter/textfile"
OUT_FILE="$OUT_DIR/pcm_pcie.prom"
TMP_FILE="$(mktemp "$OUT_DIR/pcm_pcie.prom.XXXXXX")"
CSV_FILE="$(mktemp /tmp/pcm_pcie.XXXXXX.csv)"
PCM_PCIE_BIN="${PCM_PCIE_BIN:-/usr/local/bin/pcm-pcie-20260811}"

trap 'rm -f "$TMP_FILE" "$CSV_FILE"' EXIT

publish_failure() {
    {
        echo "# HELP pcm_pcie_collector_success 1 when the latest PCM PCIe sample succeeded"
        echo "# TYPE pcm_pcie_collector_success gauge"
        echo "pcm_pcie_collector_success 0"
        echo "# HELP pcm_pcie_collector_timestamp_seconds Unix timestamp of the collector attempt"
        echo "# TYPE pcm_pcie_collector_timestamp_seconds gauge"
        printf 'pcm_pcie_collector_timestamp_seconds %s\n' "$(date +%s)"
    } > "$TMP_FILE"
    chmod 0644 "$TMP_FILE"
    mv "$TMP_FILE" "$OUT_FILE"
}

if [[ ! -x "$PCM_PCIE_BIN" ]] ||
    ! "$PCM_PCIE_BIN" 1 -B -e -csv="$CSV_FILE" -i=1 >/dev/null 2>&1; then
    publish_failure
    exit 1
fi

if ! samples="$(
    awk -F, '
        NR == 1 { next }
        NF != 10 { exit 1 }
        {
            socket = $1
            read_bytes = $9
            write_field = $10

            if (write_field !~ /\((Total|Miss|Hit)\)$/) {
                exit 2
            }
            classification = write_field
            sub(/^.*\(/, "", classification)
            sub(/\)$/, "", classification)
            classification = tolower(classification)
            sub(/\(.*/, "", write_field)

            if (socket !~ /^[0-9]+$/ ||
                read_bytes !~ /^[0-9]+([.][0-9]+)?$/ ||
                write_field !~ /^[0-9]+([.][0-9]+)?$/) {
                exit 3
            }

            printf "pcm_pcie_bandwidth_bytes_per_second{socket=\"%s\",direction=\"read\",classification=\"%s\"} %s\n",
                socket, classification, read_bytes
            printf "pcm_pcie_bandwidth_bytes_per_second{socket=\"%s\",direction=\"write\",classification=\"%s\"} %s\n",
                socket, classification, write_field
        }
    ' "$CSV_FILE"
)"; then
    publish_failure
    exit 1
fi

{
    echo "# HELP pcm_pcie_bandwidth_bytes_per_second PCM PCIe bandwidth class from a one-second sample"
    echo "# TYPE pcm_pcie_bandwidth_bytes_per_second gauge"
    printf '%s\n' "$samples"
    echo "# HELP pcm_pcie_collector_success 1 when the latest PCM PCIe sample succeeded"
    echo "# TYPE pcm_pcie_collector_success gauge"
    echo "pcm_pcie_collector_success 1"
    echo "# HELP pcm_pcie_collector_timestamp_seconds Unix timestamp of the latest PCM PCIe sample"
    echo "# TYPE pcm_pcie_collector_timestamp_seconds gauge"
    printf 'pcm_pcie_collector_timestamp_seconds %s\n' "$(date +%s)"
} > "$TMP_FILE"

chmod 0644 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
