#!/usr/bin/env bash
# Install the ACC core-usage + Falcon tele_cli textfile collector on one host,
# plus node_exporter if it is not already there. Idempotent. An optional
# target-only gRPC shadow collector proves the faster persistent path without
# changing the dashboards.
#
# This is the recipe that was applied by hand to mmgt first and then to the two
# initiators; it exists so the third host does not get a different setup.
#
# Usage:
#   IMC_PASSWORD=<imc-root-pw> ./install_acc_stats.sh <host> [acc-targets]
#
# `acc-targets` is the ACC_TARGETS spec "netns:label:acc-fabric-ip,..." and
# defaults to the collector's own two-card mmgt default. On an initiator the
# netns field is empty, because the IMC link there is a plain host interface
# holding 100.0.0.1/24 rather than a namespaced management vport:
#
#   IMC_PASSWORD=... ./install_acc_stats.sh mmgi0 ':acc1:200.0.4.3'
#   IMC_PASSWORD=... ./install_acc_stats.sh mmgi1 ':acc1:200.0.3.3'
#   IMC_PASSWORD=... ./install_acc_stats.sh mmgt          # two-card default
#
# Target-only gRPC shadow (does not replace acc-transport):
#   IMC_PASSWORD=... ACC_GRPC_SHADOW=1 \
#     ACC_GRPC_PYTHON=/opt/acc-grpc-telemetry/venv/bin/python \
#     ACC_GRPC_PROTO_DIR=/opt/acc-grpc-telemetry/proto \
#     ./install_acc_stats.sh mmgt
#
# The password is written to /etc/default/acc-stats mode 600 and is never baked
# into the collector or the unit, so both stay byte-identical fleet-wide.
#
# node_exporter is copied from NODE_EXPORTER_BIN if given, else from whatever is
# already on the host. It is NOT downloaded: mmgi0 sits behind a pip/HTTP proxy
# quirk, and matching mmgt's exact 1.8.2 build matters more than convenience.
# Stage it first with:  scp mmgt:/usr/local/bin/node_exporter /tmp/node_exporter
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TELEMETRY_COLLECTOR="$SCRIPT_DIR/../../docs/design/v1/platform/ipu-poc/instrumentation/host/bin/acc_telemetry_textfile.py"

usage() {
    echo "Usage: IMC_PASSWORD=<pw> $0 <host> [acc-targets]" >&2
    exit 1
}

[ $# -ge 1 ] || usage
HOST="$1"
ACC_TARGETS_SPEC="${2:-}"
ACC_GRPC_SHADOW="${ACC_GRPC_SHADOW:-0}"
ACC_GRPC_PYTHON="${ACC_GRPC_PYTHON:-/opt/acc-grpc-telemetry/venv/bin/python}"
ACC_GRPC_PROTO_DIR="${ACC_GRPC_PROTO_DIR:-/opt/acc-grpc-telemetry/proto}"
ACC_GRPC_PROTO_SOURCE="${ACC_GRPC_PROTO_SOURCE:-/opt/falcon/tools/controller/python_out}"

if [ -z "${IMC_PASSWORD:-}" ]; then
    echo "IMC_PASSWORD is required (the IMC root password)." >&2
    exit 1
fi

if [ "$ACC_GRPC_SHADOW" = 1 ]; then
    if [ "$HOST" != mmgt ]; then
        echo "ACC_GRPC_SHADOW currently supports mmgt only." >&2
        exit 1
    fi
    for setting in ACC_GRPC_PYTHON ACC_GRPC_PROTO_DIR ACC_GRPC_PROTO_SOURCE; do
        value="${!setting:-}"
        case "$value" in
            "" | *[!A-Za-z0-9_./:-]*)
                echo "$setting must be a simple absolute runtime path." >&2
                exit 1
                ;;
        esac
    done
fi

for f in \
    acc_ssh_stats.py \
    acc-stats.service \
    acc-stats.timer \
    acc-transport.service \
    acc-transport.timer
do
    [ -f "$SCRIPT_DIR/$f" ] || { echo "missing $SCRIPT_DIR/$f" >&2; exit 1; }
done

if [ "$ACC_GRPC_SHADOW" = 1 ]; then
    for f in \
        acc_grpc_tunnel.py \
        acc-grpc-tunnel@.service \
        acc-grpc-telemetry@.service \
        acc-grpc-telemetry@.timer
    do
        [ -f "$SCRIPT_DIR/$f" ] || {
            echo "missing $SCRIPT_DIR/$f" >&2
            exit 1
        }
    done
    [ -f "$TELEMETRY_COLLECTOR" ] || {
        echo "missing $TELEMETRY_COLLECTOR" >&2
        exit 1
    }
fi

echo "== $HOST: staging collector =="
scp -q "$SCRIPT_DIR/acc_ssh_stats.py" "$HOST:/tmp/acc_ssh_stats.py"
scp -q "$SCRIPT_DIR/acc-stats.service" "$HOST:/tmp/acc-stats.service"
scp -q "$SCRIPT_DIR/acc-stats.timer" "$HOST:/tmp/acc-stats.timer"
scp -q "$SCRIPT_DIR/acc-transport.service" "$HOST:/tmp/acc-transport.service"
scp -q "$SCRIPT_DIR/acc-transport.timer" "$HOST:/tmp/acc-transport.timer"

if [ "$ACC_GRPC_SHADOW" = 1 ]; then
    scp -q "$SCRIPT_DIR/acc_grpc_tunnel.py" "$HOST:/tmp/acc_grpc_tunnel.py"
    scp -q "$SCRIPT_DIR/acc-grpc-tunnel@.service" "$HOST:/tmp/"
    scp -q "$SCRIPT_DIR/acc-grpc-telemetry@.service" "$HOST:/tmp/"
    scp -q "$SCRIPT_DIR/acc-grpc-telemetry@.timer" "$HOST:/tmp/"
    scp -q "$TELEMETRY_COLLECTOR" "$HOST:/tmp/acc_telemetry_textfile.py"
fi

if [ -n "${NODE_EXPORTER_BIN:-}" ]; then
    echo "== $HOST: staging node_exporter from $NODE_EXPORTER_BIN =="
    scp -q "$NODE_EXPORTER_BIN" "$HOST:/tmp/node_exporter"
fi

# The credential travels as a mode-600 file rather than on the command line, so
# it never appears in the remote process list. stdin is not available for it --
# the remote script itself is fed over stdin below.
ENV_TMP="$(mktemp)"
trap 'rm -f "$ENV_TMP"' EXIT
chmod 600 "$ENV_TMP"
{
    echo "# Host-specific config for acc-stats.service. Mode 600 -- holds a credential."
    echo "IMC_PASSWORD=${IMC_PASSWORD}"
    [ -n "$ACC_TARGETS_SPEC" ] && echo "ACC_TARGETS=${ACC_TARGETS_SPEC}"
} > "$ENV_TMP"
scp -q -p "$ENV_TMP" "$HOST:/tmp/acc-stats.env"

if [ "$ACC_GRPC_SHADOW" = 1 ]; then
    for target in \
        "acc1:IPU2:200.0.6.3:15001" \
        "acc2:IPU1:200.0.5.3:15002"
    do
        IFS=: read -r label netns fabric_ip local_port <<<"$target"
        config_tmp="$(mktemp)"
        trap 'rm -f "$ENV_TMP" "$config_tmp"' EXIT
        chmod 600 "$config_tmp"
        {
            echo "# Host-specific config for the ${label} gRPC shadow collector."
            echo "ACC_GRPC_NETNS=$netns"
            echo "ACC_GRPC_FABRIC_IP=$fabric_ip"
            echo "ACC_GRPC_LOCAL_PORT=$local_port"
            echo "ACC_GRPC_PYTHON=$ACC_GRPC_PYTHON"
            echo "ACC_TELEMETRY_ENDPOINT=127.0.0.1:$local_port"
            echo "ACC_TELEMETRY_PROTO_DIR=$ACC_GRPC_PROTO_DIR/$label"
            echo "ACC_GRPC_PROTO_SOURCE=$ACC_GRPC_PROTO_SOURCE"
            echo "ACC_TELEMETRY_ACC=$label"
            echo "ACC_TELEMETRY_OUTPUT_FILENAME=acc_grpc_${label}.prom"
        } >"$config_tmp"
        scp -q -p "$config_tmp" "$HOST:/tmp/acc-grpc-${label}.env"
        rm -f "$config_tmp"
    done
fi

ssh "$HOST" 'bash -s' <<'REMOTE'
set -euo pipefail

TEXTFILE_DIR=/var/lib/node_exporter/textfile

echo "== $(hostname): node_exporter =="
if [ ! -x /usr/local/bin/node_exporter ]; then
    if [ -f /tmp/node_exporter ]; then
        install -m 0755 /tmp/node_exporter /usr/local/bin/node_exporter
        echo "  installed from staged binary"
    else
        echo "  ERROR: node_exporter absent and nothing staged at /tmp/node_exporter" >&2
        echo "  re-run with NODE_EXPORTER_BIN=<local path>" >&2
        exit 1
    fi
else
    echo "  already present: $(/usr/local/bin/node_exporter --version 2>&1 | head -1)"
fi

id -u node_exporter >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin node_exporter
mkdir -p "$TEXTFILE_DIR"
chown -R node_exporter:node_exporter /var/lib/node_exporter

if [ ! -f /etc/systemd/system/node_exporter.service ]; then
    cat > /etc/systemd/system/node_exporter.service <<'UNIT'
[Unit]
Description=Prometheus Node Exporter
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=node_exporter
Group=node_exporter
ExecStart=/usr/local/bin/node_exporter \
    --web.listen-address=127.0.0.1:9100 \
    --collector.textfile.directory=/var/lib/node_exporter/textfile \
    --collector.processes \
    --collector.systemd \
    --no-collector.wifi \
    --no-collector.bonding \
    --no-collector.zfs
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadOnlyPaths=/
ReadWritePaths=/var/lib/node_exporter/textfile

[Install]
WantedBy=multi-user.target
UNIT
    echo "  wrote node_exporter.service"
fi

echo "== $(hostname): acc-stats =="
install -m 0755 /tmp/acc_ssh_stats.py /usr/local/bin/acc_ssh_stats.py
install -m 0644 /tmp/acc-stats.service /etc/systemd/system/acc-stats.service
install -m 0644 /tmp/acc-stats.timer /etc/systemd/system/acc-stats.timer
install -m 0644 /tmp/acc-transport.service /etc/systemd/system/acc-transport.service
install -m 0644 /tmp/acc-transport.timer /etc/systemd/system/acc-transport.timer

[ -f /tmp/acc-stats.env ] || { echo "  ERROR: /tmp/acc-stats.env not staged" >&2; exit 1; }
install -m 0600 /tmp/acc-stats.env /etc/default/acc-stats
shred -u /tmp/acc-stats.env 2>/dev/null || rm -f /tmp/acc-stats.env
echo "  wrote /etc/default/acc-stats (mode 600)"

if [ -f /tmp/acc-grpc-acc1.env ]; then
    echo "== $(hostname): ACC gRPC shadow =="
    install -m 0755 /tmp/acc_grpc_tunnel.py /usr/local/bin/acc_grpc_tunnel.py
    install -m 0755 /tmp/acc_telemetry_textfile.py /usr/local/bin/acc_telemetry_textfile.py
    install -m 0644 /tmp/acc-grpc-tunnel@.service /etc/systemd/system/
    install -m 0644 /tmp/acc-grpc-telemetry@.service /etc/systemd/system/
    install -m 0644 /tmp/acc-grpc-telemetry@.timer /etc/systemd/system/
    for label in acc1 acc2; do
        install -m 0600 "/tmp/acc-grpc-${label}.env" "/etc/default/acc-grpc-${label}"
        set -a
        # shellcheck disable=SC1090
        . "/etc/default/acc-grpc-${label}"
        set +a
        if [ ! -x "$ACC_GRPC_PYTHON" ]; then
            venv_dir="$(dirname "$(dirname "$ACC_GRPC_PYTHON")")"
            python3 -m venv "$venv_dir"
        fi
        if ! "$ACC_GRPC_PYTHON" -c 'import grpc, google.protobuf'; then
            "$ACC_GRPC_PYTHON" -m pip install --disable-pip-version-check \
                grpcio protobuf
        fi
    done
fi

systemctl daemon-reload
systemctl enable --now node_exporter.service >/dev/null 2>&1 || systemctl restart node_exporter.service
systemctl enable --now acc-stats.timer >/dev/null
systemctl enable --now acc-transport.timer >/dev/null
if [ -f /etc/default/acc-grpc-acc1 ]; then
    for label in acc1 acc2; do
        systemctl enable --now "acc-grpc-tunnel@${label}.service" >/dev/null
        systemctl enable --now "acc-grpc-telemetry@${label}.timer" >/dev/null
    done
fi

echo "== $(hostname): first sample =="
systemctl start acc-stats.service
systemctl start acc-transport.service
if [ -f /etc/default/acc-grpc-acc1 ]; then
    systemctl start acc-grpc-telemetry@acc1.service
    systemctl start acc-grpc-telemetry@acc2.service
fi
for _ in $(seq 1 30); do
    [ -s "$TEXTFILE_DIR/acc_stats.prom" ] &&
        [ -s "$TEXTFILE_DIR/acc_transport.prom" ] &&
        { [ ! -f /etc/default/acc-grpc-acc1 ] ||
            { [ -s "$TEXTFILE_DIR/acc_grpc_acc1.prom" ] &&
                [ -s "$TEXTFILE_DIR/acc_grpc_acc2.prom" ]; }; } && break
    sleep 1
done

if [ ! -s "$TEXTFILE_DIR/acc_stats.prom" ] ||
    [ ! -s "$TEXTFILE_DIR/acc_transport.prom" ]; then
    echo "  ERROR: ACC textfiles were not written" >&2
    systemctl status acc-stats.service --no-pager -l | tail -20 >&2
    systemctl status acc-transport.service --no-pager -l | tail -20 >&2
    exit 1
fi
if [ -f /etc/default/acc-grpc-acc1 ] &&
    { [ ! -s "$TEXTFILE_DIR/acc_grpc_acc1.prom" ] ||
        [ ! -s "$TEXTFILE_DIR/acc_grpc_acc2.prom" ]; }; then
    echo "  ERROR: ACC gRPC shadow textfiles were not written" >&2
    systemctl status acc-grpc-tunnel@acc1.service --no-pager -l | tail -20 >&2
    systemctl status acc-grpc-tunnel@acc2.service --no-pager -l | tail -20 >&2
    exit 1
fi
chown node_exporter:node_exporter "$TEXTFILE_DIR/acc_stats.prom" 2>/dev/null || true
chown node_exporter:node_exporter "$TEXTFILE_DIR/acc_transport.prom" 2>/dev/null || true
chown node_exporter:node_exporter "$TEXTFILE_DIR"/acc_grpc_*.prom 2>/dev/null || true

cores=$(grep -c '^acc_cpu_busy_percent' "$TEXTFILE_DIR/acc_stats.prom" || true)
tele=$(grep -c '^acc_tele_field' "$TEXTFILE_DIR/acc_transport.prom" || true)
echo "  acc_cpu_busy_percent series: $cores"
echo "  acc_tele_field series:       $tele"
[ -f /etc/default/acc-grpc-acc1 ] && \
    echo "  acc_telemetry_bytes_total:  $(grep -ch '^acc_telemetry_bytes_total' "$TEXTFILE_DIR"/acc_grpc_*.prom || true)"
[ "$cores" -gt 0 ] || { echo "  ERROR: no ACC core series" >&2; exit 1; }

echo "== $(hostname): node_exporter is serving them =="
# --noproxy is required, not cosmetic: mmgi0 carries a curl proxy config that
# intercepts even localhost and answers /metrics with a 403, which looks exactly
# like a broken exporter.
served=$(curl -s --noproxy '*' localhost:9100/metrics | grep -c '^acc_cpu_busy_percent' || true)
served_tele=$(curl -s --noproxy '*' localhost:9100/metrics | grep -c '^acc_tele_field' || true)
echo "  served core series via :9100:      $served"
echo "  served transport series via :9100: $served_tele"
[ "$served" -gt 0 ] || { echo "  ERROR: node_exporter is not exporting core telemetry" >&2; exit 1; }
[ "$served_tele" -gt 0 ] || { echo "  ERROR: node_exporter is not exporting transport telemetry" >&2; exit 1; }
if [ -f /etc/default/acc-grpc-acc1 ]; then
    served_grpc=$(curl -s --noproxy '*' localhost:9100/metrics | grep -c '^acc_telemetry_bytes_total' || true)
    echo "  served gRPC shadow series via :9100: $served_grpc"
    [ "$served_grpc" -gt 0 ] || {
        echo "  ERROR: node_exporter is not exporting gRPC shadow telemetry" >&2
        exit 1
    }
fi
rm -f \
    /tmp/acc_ssh_stats.py \
    /tmp/acc-stats.service \
    /tmp/acc-stats.timer \
    /tmp/acc-transport.service \
    /tmp/acc-transport.timer \
    /tmp/acc_grpc_tunnel.py \
    /tmp/acc_telemetry_textfile.py \
    /tmp/acc-grpc-tunnel@.service \
    /tmp/acc-grpc-telemetry@.service \
    /tmp/acc-grpc-telemetry@.timer \
    /tmp/acc-grpc-acc1.env \
    /tmp/acc-grpc-acc2.env \
    /tmp/node_exporter
REMOTE

echo "== $HOST: done =="
