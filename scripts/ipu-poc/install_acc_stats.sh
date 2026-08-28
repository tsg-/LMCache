#!/usr/bin/env bash
# Install the ACC core-usage + Falcon tele_cli textfile collector on one host,
# plus node_exporter if it is not already there. Idempotent.
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
# The password is written to /etc/default/acc-stats mode 600 and is never baked
# into the collector or the unit, so both stay byte-identical fleet-wide.
#
# node_exporter is copied from NODE_EXPORTER_BIN if given, else from whatever is
# already on the host. It is NOT downloaded: mmgi0 sits behind a pip/HTTP proxy
# quirk, and matching mmgt's exact 1.8.2 build matters more than convenience.
# Stage it first with:  scp mmgt:/usr/local/bin/node_exporter /tmp/node_exporter
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    echo "Usage: IMC_PASSWORD=<pw> $0 <host> [acc-targets]" >&2
    exit 1
}

[ $# -ge 1 ] || usage
HOST="$1"
ACC_TARGETS_SPEC="${2:-}"

if [ -z "${IMC_PASSWORD:-}" ]; then
    echo "IMC_PASSWORD is required (the IMC root password)." >&2
    exit 1
fi

for f in acc_ssh_stats.py acc-stats.service acc-stats.timer; do
    [ -f "$SCRIPT_DIR/$f" ] || { echo "missing $SCRIPT_DIR/$f" >&2; exit 1; }
done

echo "== $HOST: staging collector =="
scp -q "$SCRIPT_DIR/acc_ssh_stats.py" "$HOST:/tmp/acc_ssh_stats.py"
scp -q "$SCRIPT_DIR/acc-stats.service" "$HOST:/tmp/acc-stats.service"
scp -q "$SCRIPT_DIR/acc-stats.timer" "$HOST:/tmp/acc-stats.timer"

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

[ -f /tmp/acc-stats.env ] || { echo "  ERROR: /tmp/acc-stats.env not staged" >&2; exit 1; }
install -m 0600 /tmp/acc-stats.env /etc/default/acc-stats
shred -u /tmp/acc-stats.env 2>/dev/null || rm -f /tmp/acc-stats.env
echo "  wrote /etc/default/acc-stats (mode 600)"

systemctl daemon-reload
systemctl enable --now node_exporter.service >/dev/null 2>&1 || systemctl restart node_exporter.service
systemctl enable --now acc-stats.timer >/dev/null

echo "== $(hostname): first sample =="
systemctl start acc-stats.service
for _ in $(seq 1 30); do
    [ -s "$TEXTFILE_DIR/acc_stats.prom" ] && break
    sleep 1
done

if [ ! -s "$TEXTFILE_DIR/acc_stats.prom" ]; then
    echo "  ERROR: no acc_stats.prom written" >&2
    systemctl status acc-stats.service --no-pager -l | tail -20 >&2
    exit 1
fi
chown node_exporter:node_exporter "$TEXTFILE_DIR/acc_stats.prom" 2>/dev/null || true

cores=$(grep -c '^acc_cpu_busy_percent' "$TEXTFILE_DIR/acc_stats.prom" || true)
tele=$(grep -c '^acc_tele_field' "$TEXTFILE_DIR/acc_stats.prom" || true)
echo "  acc_cpu_busy_percent series: $cores"
echo "  acc_tele_field series:       $tele"
[ "$cores" -gt 0 ] || { echo "  ERROR: no ACC core series" >&2; exit 1; }

echo "== $(hostname): node_exporter is serving them =="
# --noproxy is required, not cosmetic: mmgi0 carries a curl proxy config that
# intercepts even localhost and answers /metrics with a 403, which looks exactly
# like a broken exporter.
served=$(curl -s --noproxy '*' localhost:9100/metrics | grep -c '^acc_cpu_busy_percent' || true)
echo "  served via :9100: $served"
[ "$served" -gt 0 ] || { echo "  ERROR: node_exporter is not exporting the textfile" >&2; exit 1; }
rm -f /tmp/acc_ssh_stats.py /tmp/acc-stats.service /tmp/acc-stats.timer /tmp/node_exporter
REMOTE

echo "== $HOST: done =="
