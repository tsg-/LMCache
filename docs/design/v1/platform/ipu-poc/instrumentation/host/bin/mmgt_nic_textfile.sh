#!/usr/bin/env bash
set -euo pipefail

OUT_DIR="/var/lib/node_exporter/textfile"
OUT_FILE="$OUT_DIR/mmgt_nic.prom"
TMP_FILE="$(mktemp "$OUT_DIR/mmgt_nic.prom.XXXXXX")"
IFACES="${MMGT_NIC_IFACES:-enp79s0f0 enp45s0f0}"

trap 'rm -f "$TMP_FILE"' EXIT

{
    echo "# HELP mmgt_nic_stat Ethtool counter for an mmgt target data-plane NIC"
    echo "# TYPE mmgt_nic_stat counter"
    for iface in $IFACES; do
        ethtool -S "$iface" 2>/dev/null \
            | awk -v iface="$iface" '
                /^ +[a-zA-Z_]/ {
                    gsub(/:$/, "", $1)
                    name = $1
                    value = $NF
                    if (value ~ /^[0-9]+$/) {
                        gsub(/[^a-zA-Z0-9_]/, "_", name)
                        printf("mmgt_nic_stat{iface=\"%s\",name=\"%s\"} %s\n",
                               iface, name, value)
                    }
                }
            '
    done
} > "$TMP_FILE"

chmod 0644 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
trap - EXIT
