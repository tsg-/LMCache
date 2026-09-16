#!/usr/bin/env bash
# Export configured NVMe-oF I/O queue pairs as node_exporter textfile metrics.

set -euo pipefail

OUT_DIR="${OUT_DIR:-/var/lib/node_exporter/textfile}"
SYS_CLASS_NVME="${SYS_CLASS_NVME:-/sys/class/nvme}"
OUT_FILE="$OUT_DIR/nvmeof_qp.prom"
TMP_FILE="$(mktemp "$OUT_DIR/nvmeof_qp.prom.XXXXXX")"

trap 'rm -f "$TMP_FILE"' EXIT

controller_count=0
io_qps_total=0

{
    echo "# HELP nvmeof_configured_io_qps Configured I/O queue pairs per NVMe-oF controller"
    echo "# TYPE nvmeof_configured_io_qps gauge"
    for controller in "$SYS_CLASS_NVME"/nvme*; do
        [ -d "$controller" ] || continue
        transport="$(cat "$controller/transport" 2>/dev/null || true)"
        [ "$transport" = "rdma" ] || continue

        queue_count="$(cat "$controller/queue_count" 2>/dev/null || true)"
        [[ "$queue_count" =~ ^[0-9]+$ ]] || continue
        [ "$queue_count" -ge 1 ] || continue

        state="$(cat "$controller/state" 2>/dev/null || echo unknown)"
        io_qps=$((queue_count - 1))
        name="${controller##*/}"
        printf 'nvmeof_configured_io_qps{controller="%s",state="%s"} %s\n' \
            "$name" "$state" "$io_qps"
        controller_count=$((controller_count + 1))
        io_qps_total=$((io_qps_total + io_qps))
    done

    echo "# HELP nvmeof_configured_controller_count RDMA NVMe-oF controllers"
    echo "# TYPE nvmeof_configured_controller_count gauge"
    printf 'nvmeof_configured_controller_count %s\n' "$controller_count"
    echo "# HELP nvmeof_configured_io_qps_total Configured I/O queue pairs across RDMA NVMe-oF controllers"
    echo "# TYPE nvmeof_configured_io_qps_total gauge"
    printf 'nvmeof_configured_io_qps_total %s\n' "$io_qps_total"
} > "$TMP_FILE"

chmod 0644 "$TMP_FILE"
mv "$TMP_FILE" "$OUT_FILE"
trap - EXIT
