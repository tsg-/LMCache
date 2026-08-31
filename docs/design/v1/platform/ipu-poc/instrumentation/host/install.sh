#!/usr/bin/env bash
# install.sh -- stand up node_exporter + textfile collectors on one test host.
#
# Run AS ROOT ON THE TARGET HOST, from an unpacked copy of the host/ directory:
#     scp -r host newhost:/tmp/obs && ssh newhost 'RDMA_FABRIC_IFACE=ens2f0 \
#       ACC_TELEMETRY_ENDPOINT=<acc-ip>:50051 \
#       ACC_TELEMETRY_PROTO_DIR=<dir-with-telemetry_pb2.py> bash /tmp/obs/install.sh'
#
# On a host with no accelerator, SKIP_ACC_TELEMETRY=1 replaces the two
# ACC_TELEMETRY_ variables and installs the other five collectors.
#
# Idempotent: safe to re-run. Overwrites scripts and units, restarts services.
#
# WHY TIMERS AND NOT CRON: the original deployment drove nvme_stats and rdma_nic
# from crontab. A later `sed -i "/rdma_hwcounters_textfile/d"` cleanup took the
# whole crontab with it, leaving both scripts on disk and executable with nothing
# invoking them. node_exporter kept serving the stale .prom files as if current,
# which renders in Grafana as a flat line -- indistinguishable from a quiet
# fabric. Measured drift before repair: mkp1 ~3h, mkp2 ~12h, 254.3 GiB of
# unaccounted port_rx_bytes. Every collector here gets its own systemd timer so
# `systemctl list-timers` is the complete inventory.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
IFACE="${RDMA_FABRIC_IFACE:-}"
LISTEN="${NODE_EXPORTER_LISTEN:-127.0.0.1:9100}"
NE_VERSION="${NODE_EXPORTER_VERSION:-1.8.2}"
TEXTFILE_DIR=/var/lib/node_exporter/textfile
ACC_TELEMETRY_ENDPOINT="${ACC_TELEMETRY_ENDPOINT:-}"
ACC_TELEMETRY_PROTO_DIR="${ACC_TELEMETRY_PROTO_DIR:-}"
# SKIP_ACC_TELEMETRY=1 drops the ACC collector and installs the other five. For a
# host with no accelerator to point the endpoint at; the alternative was telling
# people to delete unit names out of this script by hand.
SKIP_ACC_TELEMETRY="${SKIP_ACC_TELEMETRY:-}"

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

if [ -z "$IFACE" ]; then
    echo "ERROR: set RDMA_FABRIC_IFACE to the RDMA *fabric* interface." >&2
    echo "       NEVER point it at the SSH management interface -- the" >&2
    echo "       collector only reads counters, but a wrong name silently" >&2
    echo "       produces mgmt-plane numbers labelled as fabric." >&2
    echo "  candidates:" >&2
    ip -br addr show | sed 's/^/    /' >&2
    exit 1
fi
ip link show "$IFACE" >/dev/null 2>&1 || { echo "no such interface: $IFACE" >&2; exit 1; }

if [ -n "$SKIP_ACC_TELEMETRY" ]; then
    echo "SKIP_ACC_TELEMETRY set: installing five collectors, no ACC telemetry."
    echo "  the authoritative payload-byte source is the ACC; without it the"
    echo "  NIC counters are a traffic-presence diagnostic only."
elif [ -z "$ACC_TELEMETRY_ENDPOINT" ] || [ -z "$ACC_TELEMETRY_PROTO_DIR" ]; then
    echo "ERROR: set ACC_TELEMETRY_ENDPOINT and ACC_TELEMETRY_PROTO_DIR." >&2
    echo "       Example: ACC_TELEMETRY_ENDPOINT=10.0.0.35:50051" >&2
    echo "       The protobuf directory must contain telemetry_pb2.py and" >&2
    echo "       telemetry_pb2_grpc.py from the installed feature pack." >&2
    echo "       On a host with no accelerator, SKIP_ACC_TELEMETRY=1 installs" >&2
    echo "       the other five collectors instead." >&2
    exit 1
else
    [ -f "$ACC_TELEMETRY_PROTO_DIR/telemetry_pb2.py" ] &&
        [ -f "$ACC_TELEMETRY_PROTO_DIR/telemetry_pb2_grpc.py" ] || {
        echo "ERROR: invalid ACC_TELEMETRY_PROTO_DIR: $ACC_TELEMETRY_PROTO_DIR" >&2
        exit 1
    }
fi

echo "== dependencies =="
missing=()
for c in nvme ethtool jq pcm-memory python3; do
    command -v "$c" >/dev/null || missing+=("$c")
done
if [ ${#missing[@]} -gt 0 ]; then
    echo "  installing: ${missing[*]}"
    if command -v dnf >/dev/null; then
        dnf install -y nvme-cli ethtool jq pcm python3
    elif command -v apt-get >/dev/null; then
        apt-get update && apt-get install -y nvme-cli ethtool jq pcm python3
    else
        echo "  no dnf/apt; install manually: ${missing[*]}" >&2; exit 1
    fi
else
    echo "  nvme, ethtool, jq, pcm-memory, python3 present"
fi
if [ -z "$SKIP_ACC_TELEMETRY" ]; then
    python3 -c 'import grpc' >/dev/null 2>&1 || {
        echo "ERROR: Python grpc module is required for ACC telemetry." >&2
        exit 1
    }
fi

echo "== node_exporter binary =="
if [ -x /usr/local/bin/node_exporter ]; then
    echo "  present: $(/usr/local/bin/node_exporter --version 2>&1 | head -1)"
else
    tarball="node_exporter-${NE_VERSION}.linux-amd64"
    echo "  fetching ${tarball}"
    curl -fsSL "https://github.com/prometheus/node_exporter/releases/download/v${NE_VERSION}/${tarball}.tar.gz" \
        | tar xz -C /tmp
    install -m 0755 "/tmp/${tarball}/node_exporter" /usr/local/bin/node_exporter
    rm -rf "/tmp/${tarball}"
fi

echo "== user + textfile dir =="
id node_exporter >/dev/null 2>&1 || useradd --system --no-create-home --shell /sbin/nologin node_exporter
install -d -o node_exporter -g node_exporter -m 0755 "$TEXTFILE_DIR"

collectors=(nvme_stats_textfile.sh rdma_hwcounters_textfile.sh rdma_nic_textfile.sh
            pcm_memory_textfile.sh numa_stats_textfile.sh)
timers=(rdma-hwcounters nvme-stats rdma-nic pcm-memory numa-stats)
if [ -z "$SKIP_ACC_TELEMETRY" ]; then
    collectors+=(acc_telemetry_textfile.py)
    timers+=(acc-telemetry)
fi

echo "== collector scripts =="
for f in "${collectors[@]}"; do
    install -m 0755 "$SRC/bin/$f" "/usr/local/bin/$f"
    echo "  /usr/local/bin/$f"
done

echo "== systemd units =="
for u in node_exporter.service "${timers[@]/%/.service}" "${timers[@]/%/.timer}"; do
    install -m 0644 "$SRC/systemd/$u" "/etc/systemd/system/$u"
    echo "  /etc/systemd/system/$u"
done

# The collector script's built-in default iface is right for mkp1/mkp2 only.
# Pin it explicitly per host via the unit rather than editing the script, so the
# script stays byte-identical across the fleet (md5 is a useful drift check).
mkdir -p /etc/systemd/system/rdma-nic.service.d
cat > /etc/systemd/system/rdma-nic.service.d/iface.conf <<EOF
[Service]
Environment=RDMA_FABRIC_IFACE=${IFACE}
EOF
echo "  rdma-nic.service.d/iface.conf -> RDMA_FABRIC_IFACE=${IFACE}"

if [ -z "$SKIP_ACC_TELEMETRY" ]; then
    mkdir -p /etc/systemd/system/acc-telemetry.service.d
    cat > /etc/systemd/system/acc-telemetry.service.d/endpoint.conf <<EOF
[Service]
Environment=ACC_TELEMETRY_ENDPOINT=${ACC_TELEMETRY_ENDPOINT}
Environment=ACC_TELEMETRY_PROTO_DIR=${ACC_TELEMETRY_PROTO_DIR}
EOF
    echo "  acc-telemetry.service.d/endpoint.conf -> ${ACC_TELEMETRY_ENDPOINT}"
fi

if [ "$LISTEN" != "127.0.0.1:9100" ]; then
    mkdir -p /etc/systemd/system/node_exporter.service.d
    cat > /etc/systemd/system/node_exporter.service.d/listen.conf <<EOF
[Service]
Environment=NE_LISTEN=${LISTEN}
EOF
    echo "  node_exporter.service.d/listen.conf -> ${LISTEN}"
fi

echo "== enable =="
# Guard against the failure mode this script exists to prevent: a leftover cron
# entry racing the timers, producing two writers for the same .prom file.
if crontab -l 2>/dev/null | grep -qE 'nvme_stats_textfile|rdma_nic_textfile|rdma_hwcounters_textfile|pcm_memory_textfile|numa_stats_textfile|acc_telemetry_textfile'; then
    echo "  WARNING: root crontab still drives a textfile collector." >&2
    echo "           Remove those lines -- timers now own the schedule." >&2
    crontab -l | grep -nE 'nvme_stats_textfile|rdma_nic_textfile|rdma_hwcounters_textfile|pcm_memory_textfile|numa_stats_textfile|acc_telemetry_textfile' >&2
fi

systemctl daemon-reload
systemctl enable --now node_exporter.service
systemctl enable --now "${timers[@]/%/.timer}"
# Prime the .prom files so the first scrape is not empty.
systemctl start "${timers[@]/%/.service}"

echo
echo "== verify: $(hostname) =="
systemctl list-timers --all --no-pager \
    | grep -E 'rdma-hwcounters|nvme-stats|rdma-nic|pcm-memory|numa-stats|acc-telemetry' || true
echo
echo "  textfile freshness (now $(date '+%H:%M:%S')):"
ls -l --time-style=+%H:%M:%S "$TEXTFILE_DIR"/*.prom | awk '{printf "    %s  %s\n", $6, $7}'
echo
echo "  collector health:"
curl -s "http://${LISTEN}/metrics" \
    | grep -E 'node_textfile_scrape_error|node_scrape_collector_success\{collector="(textfile|diskstats)"\}' \
    | sed 's/^/    /'
echo
echo "  sample series present:"
for m in rdma_hw_counter rdma_nic_stat nvme_smart_field rdma_port_up \
         pcm_memory_bandwidth_megabytes_per_second numa_node_memory_bytes \
         acc_telemetry_bytes_total; do
    n=$(curl -s "http://${LISTEN}/metrics" | grep -c "^${m}{") || n=0
    printf '    %-18s %s series\n' "$m" "$n"
done
