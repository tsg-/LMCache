#!/usr/bin/env bash
# rdma_hwcounters.sh — export irdma RDMA hw_counters as node_exporter textfile.
#
# WHY THIS EXISTS: node_exporter's built-in `infiniband` collector reads
# /sys/class/infiniband/<dev>/ports/<p>/counters/, which irdma DOES NOT
# EXPOSE (only hw_counters/). The collector therefore hard-fails with
# node_scrape_collector_success{collector="infiniband"} 0 and emits nothing.
#
# WHY ethtool IS NOT ENOUGH: rdma_nic_textfile.sh scrapes `ethtool -S`
# port_rx_bytes/port_tx_bytes, which do NOT account RDMA-offloaded traffic on
# this driver -- measured 2026-08-03, a 34 GB RDMA read moved port_rx_bytes by
# ~3.8 KB (control traffic only). Those panels look alive while being blind to
# the workload under test. hw_counters is the only exact instrument here.
#
# COUNTER SEMANTICS on this rig (NVMe-oF over irdma/RoCEv2, measured):
#   NVMe-oF READ  -> target RDMA-writes into initiator memory -> InRdmaWrites
#                    ops = ceil(bytes / 52428)  (RDMA write segment cap)
#   NVMe-oF WRITE -> target RDMA-reads from initiator memory  -> InRdmaReads
#                    4096 B per op, block-size invariant
#   Payloads <= 4 KiB ride in-capsule (OutRdmaSends only, zero RDMA r/w ops).
#
# NOTE: irdma refreshes these counters ASYNCHRONOUSLY (~1s lag). Fine for a
# 15s scrape interval, but a delta sampled immediately after a workload ends
# can read 0 -- indistinguishable from "no data on the wire".
#
# Emits atomic write via mv(1).

set -euo pipefail

OUT_DIR="/var/lib/node_exporter/textfile"
OUT_FILE="$OUT_DIR/rdma_hwcounters.prom"
TMP_FILE="$(mktemp "$OUT_DIR/rdma_hwcounters.prom.XXXXXX")"

trap 'rm -f "$TMP_FILE"' EXIT

{
    echo "# HELP rdma_hw_counter irdma per-port RDMA hardware counter"
    echo "# TYPE rdma_hw_counter counter"
    for dev_path in /sys/class/infiniband/*; do
        [ -d "$dev_path" ] || continue
        dev=$(basename "$dev_path")
        for port_path in "$dev_path"/ports/*; do
            [ -d "$port_path/hw_counters" ] || continue
            port=$(basename "$port_path")
            for c in "$port_path"/hw_counters/*; do
                [ -f "$c" ] || continue
                name=$(basename "$c")
                val=$(cat "$c" 2>/dev/null) || continue
                case $val in
                    ''|*[!0-9]*) continue ;;
                esac
                printf 'rdma_hw_counter{device="%s",port="%s",counter="%s"} %s\n' \
                    "$dev" "$port" "$name" "$val"
            done
            # Link state as a gauge alongside, so a dead link is visible.
            state=$(cat "$port_path/state" 2>/dev/null || echo "")
            case $state in
                *ACTIVE*) up=1 ;;
                *)        up=0 ;;
            esac
            printf 'rdma_port_up{device="%s",port="%s"} %s\n' "$dev" "$port" "$up"
        done
    done
} > "$TMP_FILE"

chmod 0644 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
trap - EXIT
