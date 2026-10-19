#!/usr/bin/env bash
# rdma_hwcounters.sh — export irdma RDMA hw_counters as node_exporter textfile.
#
# WHY THIS EXISTS: node_exporter's built-in `infiniband` collector reads
# /sys/class/infiniband/<dev>/ports/<p>/counters/, which irdma DOES NOT
# EXPOSE (only hw_counters/). The collector therefore hard-fails with
# node_scrape_collector_success{collector="infiniband"} 0 and emits nothing.
#
# RELATIONSHIP TO ethtool (rdma_nic_textfile.sh): that script's port-rx_bytes /
# port-tx-bytes ARE the byte-accurate throughput instrument on Falcon -- measured
# 2026-08-12 at ratio 1.0347 against a known 96.05 Gb/s sustained load, i.e.
# payload plus wire framing. An earlier version of this comment claimed they were
# not a throughput instrument because short-window rates exceeded 100 GbE; that
# was textfile staleness aliasing the rate, not a counter defect, and it is fixed
# by using a >=60s window. hw_counters are kept for DIRECTION and TRANSACTION
# SHAPE, which ethtool cannot give.
#
# COUNTER SEMANTICS (NVMe-oF over irdma on Falcon/MEV, measured):
#   NVMe-oF READ  -> target RDMA-writes into initiator memory -> InRdmaWrites
#   NVMe-oF WRITE -> target RDMA-reads from initiator memory  -> InRdmaReads
#   Payloads <= 4 KiB ride in-capsule (OutRdmaSends only, zero RDMA r/w ops).
#
# THESE ARE TRANSACTION COUNTS, NOT BYTES, AND FALCON HAS NO FIXED CONVERSION.
# Measured across all 20 cells of the 2026-08-12 remote_xfs read sweep, bytes per
# InRdmaWrite is stable within a block size (+/-0.3% across reps) but varies 13x
# across block sizes: 3,523 B at 4k, 14,004 at 16k, 38,991 at 144k, 45,073 at
# 256k, 45,084 at 512k -- saturating near 45 KB. The 52,428 B segment cap that
# holds on the RoCE path does NOT hold here, so ops*const is wrong at every block
# size. Never compare this rate across block sizes.
#
# Note also that neither `rdma stat show link` nor netdev
# /sys/class/net/<iface>/statistics/* track Falcon traffic at all: mkp1 sent
# 1.25 GiB of RDMA WRITE and netdev tx_bytes moved 140 bytes.
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
