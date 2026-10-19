#!/usr/bin/env bash
# A missing interface after a board swap must not hide the remaining NICs.
set -uo pipefail

OUT_DIR="/var/lib/node_exporter/textfile"
OUT_FILE="$OUT_DIR/mmgt_nic.prom"
find "$OUT_DIR" -maxdepth 1 -type f -name 'mmgt_nic.prom.??????' \
    -mmin +1 -delete
TMP_FILE="$(mktemp "$OUT_DIR/mmgt_nic.prom.XXXXXX")"
# Target-side Falcon NICs after the 2026-09-16 mmgt board swap.
IFACES="${MMGT_NIC_IFACES:-enp13s0f0 enp141s0f0 enp192s0f0 enp69s0f0}"

trap 'rm -f "$TMP_FILE"' EXIT

{
    echo "# HELP mmgt_nic_stat Ethtool counter for an mmgt target data-plane NIC"
    echo "# TYPE mmgt_nic_stat counter"
    for iface in $IFACES; do
        if ! ip link show "$iface" >/dev/null 2>&1; then
            echo "mmgt_nic_textfile: WARNING: interface $iface not present, skipping" >&2
            continue
        fi
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
