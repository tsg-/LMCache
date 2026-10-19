#!/usr/bin/env bash
# rdma_nic.sh — export Mellanox RDMA fabric counters as node_exporter textfile.
#
# Run from cron @ 15s. Emits atomic write via mv(1) into
# /var/lib/node_exporter/textfile/rdma_nic.prom.
#
# IMPORTANT: bind ONLY to the RDMA fabric interface, never the mgmt interface.
#   mkp1 fabric iface: ens2f0 (200.0.0.35)
#   mkp2 fabric iface: ens2f0 (200.0.0.37)
#   mgmt (scrape plane): ens101f0 (10.166.97.x) — DO NOT read counters from this here

set -euo pipefail

IFACE="${RDMA_FABRIC_IFACE:-ens2f0}"
OUT_DIR="/var/lib/node_exporter/textfile"
OUT_FILE="$OUT_DIR/rdma_nic.prom"
TMP_FILE="$(mktemp "$OUT_DIR/rdma_nic.prom.XXXXXX")"

trap 'rm -f "$TMP_FILE"' EXIT

{
    echo "# HELP rdma_nic_stat Ethtool -S counter for RDMA fabric NIC"
    echo "# TYPE rdma_nic_stat counter"
    ethtool -S "$IFACE" 2>/dev/null \
        | awk -v iface="$IFACE" '
            /^ +[a-zA-Z_]/ {
                gsub(/:$/, "", $1)
                name = $1
                val = $NF
                if (val ~ /^[0-9]+$/) {
                    gsub(/[^a-zA-Z0-9_]/, "_", name)
                    printf("rdma_nic_stat{iface=\"%s\",name=\"%s\"} %s\n", iface, name, val)
                }
            }
        '
} > "$TMP_FILE"

chmod 0644 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
trap - EXIT
