#!/usr/bin/env bash
# Current six-initiator monitoring. --check performs reads only.
# Reuse the existing 6x2 container and volume; retain the historical stacks.
set -euo pipefail

STACK_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$STACK_ROOT"
CHECK_ONLY=0
if [ "${1:-}" = "--check" ]; then CHECK_ONLY=1; shift; fi
[ "$#" -eq 0 ] || { echo "usage: ./up.sh [--check]" >&2; exit 2; }
docker info >/dev/null

TUNNEL_BIND_ADDR="${TUNNEL_BIND_ADDR:-$(
    docker network inspect bridge --format '{{(index .IPAM.Config 0).Gateway}}'
)}"
MMG_BENCH_TUNNELS="${MMG_BENCH_TUNNELS:-1}"
PROM_CONFIG="$STACK_ROOT/profiles/6x2/prometheus.yml"
failures=0

ssh_host() {
    SSH_OPTIONS=()
    SSH_TARGET="$1"
    case "$1" in
        mmgi4|mmgi5)
            SSH_OPTIONS=(-F "${MMG_EXTRA_INITIATOR_SSH_CONFIG:-/root/.ssh/lmcache-6x2-tunnels.conf}")
            SSH_TARGET="lmcache6x2-$1"
            ;;
        mmgt2)
            SSH_OPTIONS=(-F "${MMG_SECOND_TARGET_SSH_CONFIG:-/root/.ssh/lmcache-6x6-tunnels.conf}")
            SSH_TARGET=lmcache6x6-mmgt2
            ;;
    esac
}

tunnel() {
    local host="$1" port="$2" remote_port="$3" required="$4"
    if ss -H -ltn | awk -v endpoint="$TUNNEL_BIND_ADDR:$port" \
        '$4 == endpoint {found=1} END {exit !found}'; then
        return
    fi
    if [ "$CHECK_ONLY" -eq 1 ]; then
        echo "  missing tunnel $host:$remote_port -> $TUNNEL_BIND_ADDR:$port"
        if [ "$required" -eq 1 ]; then failures=$((failures + 1)); fi
        return
    fi
    ssh_host "$host"
    ssh "${SSH_OPTIONS[@]}" -f -N -o BatchMode=yes -o ConnectTimeout=6 \
        -o ControlMaster=no -o ControlPath=none -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
        -L "$TUNNEL_BIND_ADDR:$port:127.0.0.1:$remote_port" "$SSH_TARGET"
    echo "  opened $host:$remote_port -> $TUNNEL_BIND_ADDR:$port"
}

sha256_file() {
    if command -v sha256sum >/dev/null; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

echo "== current SSH tunnels =="
for entry in mmgt:19206 mmgt2:19212 mmgi0:19207 mmgi1:19208 \
    mmgi2:19209 mmgi3:19200 mmgi4:19201 mmgi5:19202; do
    tunnel "${entry%%:*}" "${entry##*:}" 9100 1
done
tunnel mmgt 19211 9188 1
tunnel mmgt2 19213 9188 1
if [ "$MMG_BENCH_TUNNELS" = "1" ]; then
    for entry in mmgi0:19210:9101 mmgi1:19220:9102 mmgi2:19230:9103 \
        mmgi3:19240:9104 mmgi4:19250:9105 mmgi5:19260:9106; do
        IFS=: read -r host port remote_port <<<"$entry"
        tunnel "$host" "$port" "$remote_port" 0
    done
fi

if [ "$CHECK_ONLY" -eq 0 ]; then
    # Existing management bindings take precedence over the portable default.
    bind_addr="$(docker inspect -f \
        '{{with index .HostConfig.PortBindings "3000/tcp"}}{{(index . 0).HostIp}}{{end}}' \
        lmcache-grafana 2>/dev/null || true)"
    export MONITOR_BIND_ADDR="${MONITOR_BIND_ADDR:-${bind_addr:-127.0.0.1}}"
    if [ "$(docker inspect -f '{{.State.Running}}' lmcache-grafana 2>/dev/null || true)" != true ]; then
        if [ -z "${GRAFANA_PASSWORD:-}" ] &&
            ! { [ -f .env ] && grep -q '^GRAFANA_PASSWORD=' .env; }; then
            (umask 077; printf 'GRAFANA_PASSWORD=%s\n' "$(openssl rand -hex 12)" >>.env)
        fi
        docker compose -p lmcache-telemetry up -d
    fi
    if docker inspect lmcache-prom-6x2 >/dev/null 2>&1; then
        docker start lmcache-prom-6x2 >/dev/null
    else
        docker run -d --name lmcache-prom-6x2 --restart unless-stopped \
            --network lmcache-telemetry_default \
            --add-host host.docker.internal:host-gateway \
            -p "$MONITOR_BIND_ADDR:9091:9090" \
            -v "$PROM_CONFIG:/etc/prometheus/prometheus.yml:ro" \
            -v lmcache-telemetry-6x2_prom-data:/prometheus prom/prometheus:v2.55.1 \
            --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/prometheus
    fi
fi

echo "== running configuration =="
actual_sha="$(docker exec lmcache-prom-6x2 sha256sum /etc/prometheus/prometheus.yml | cut -d' ' -f1)"
[ "$(sha256_file "$PROM_CONFIG")" = "$actual_sha" ] || {
    echo "ERROR: running 6x2 Prometheus config differs from $PROM_CONFIG" >&2
    echo "  reconcile its bind-mounted source before reloading; no container was recreated" >&2
    exit 1
}
docker exec lmcache-prom-6x2 promtool check config /etc/prometheus/prometheus.yml
if [ "$CHECK_ONLY" -eq 0 ]; then
    # This existing container has no HTTP lifecycle endpoint.
    docker kill --signal=HUP lmcache-prom-6x2 >/dev/null
fi

COLLECTOR_CHECKS="${COLLECTOR_CHECKS-\
mmgt:acc_stats.prom:90 mmgt:mmgt_nic.prom:15 mmgt:numa_stats.prom:15 \
mmgt:pcm_memory.prom:15 mmgt:pcm_pcie.prom:15 mmgt:nvme_stats.prom:45 \
mmgt2:acc_stats.prom:90 mmgt2:mmgt_nic.prom:15 mmgt2:numa_stats.prom:15 \
mmgt2:pcm_memory.prom:15 mmgt2:pcm_pcie.prom:15 mmgt2:nvme_stats.prom:45 \
mmgi0:acc_stats.prom:90 mmgi0:nvmeof_qp.prom:90 \
mmgi1:acc_stats.prom:90 mmgi1:nvmeof_qp.prom:90 \
mmgi2:acc_stats.prom:90 mmgi2:nvmeof_qp.prom:90 \
mmgi3:acc_stats.prom:90 mmgi3:nvmeof_qp.prom:90 \
mmgi4:acc_stats.prom:90 mmgi4:nvmeof_qps.prom:30 \
mmgi5:acc_stats.prom:90 mmgi5:nvmeof_qps.prom:30}"
echo "== collector freshness =="
now="$(date +%s)"
for host in mmgt mmgt2 mmgi0 mmgi1 mmgi2 mmgi3 mmgi4 mmgi5; do
    textfile_dir=/var/lib/node_exporter/textfile
    case "$host" in mmgi4|mmgi5) textfile_dir=/var/lib/lmcache-6x2/textfile ;; esac
    specs=
    paths=
    for entry in $COLLECTOR_CHECKS; do
        [ "${entry%%:*}" = "$host" ] || continue
        spec="${entry#*:}"
        specs="$specs $spec"
        paths="$paths $(printf '%q' "$textfile_dir/${spec%%:*}")"
    done
    [ -n "$specs" ] || continue
    ssh_host "$host"
    mtimes="$(ssh "${SSH_OPTIONS[@]}" -o BatchMode=yes -o ConnectTimeout=6 \
        "$SSH_TARGET" "stat -c '%n %Y' $paths" 2>/dev/null || true)"
    for spec in $specs; do
        path="$textfile_dir/${spec%%:*}"
        mtime="$(awk -v path="$path" '$1 == path {print $2}' <<<"$mtimes")"
        if [ -z "$mtime" ] || [ "$((now - mtime))" -gt "${spec##*:}" ]; then
            echo "  $host: stale or missing ${spec%%:*}"
            failures=$((failures + 1))
        fi
    done
done
for port in 19211 19213; do
    if ! curl --noproxy '*' -fsS --max-time 5 \
        "http://$TUNNEL_BIND_ADDR:$port/metrics" | grep -x 'acc_telemetry_exporter_complete 1' >/dev/null; then
        echo "  target ACC exporter on :$port is missing or incomplete"
        failures=$((failures + 1))
    fi
done

echo "== current scrape health =="
published="$(docker port lmcache-prom-6x2 9090 | head -1)"
curl --noproxy '*' -fsS --max-time 10 "http://$published/api/v1/targets?state=active" |
    python3 -c '
import json, sys
targets = json.load(sys.stdin)["data"]["activeTargets"]
expected = {("node_6x2", host) for host in ["mmgt", "mmgt2"] + [f"mmgi{i}" for i in range(6)]}
expected.update({("acc_grpc_6x2", "mmgt"), ("acc_grpc_6x2", "mmgt2")})
healthy = set()
for target in targets:
    labels = target["labels"]
    key = (labels.get("job"), labels.get("host"))
    if key in expected and target["health"] == "up":
        healthy.add(key)
    if key in expected or target["health"] == "up":
        print("  {} {}: {}".format(*key, target["health"]))
missing = expected - healthy
if missing:
    print("ERROR: missing or unhealthy infrastructure:", sorted(missing))
    sys.exit(1)
print("  10/10 infrastructure scrapes healthy; benchmark endpoints may be down between runs")
'
grafana_endpoint="$(docker port lmcache-grafana 3000 | head -1)"
curl --noproxy '*' -fsS --max-time 10 "http://$grafana_endpoint/api/health" |
    python3 -c '
import json, sys
if json.load(sys.stdin).get("database") != "ok":
    sys.exit("ERROR: Grafana database is unhealthy")
print("  Grafana healthy")
'
echo "Dashboard: http://$grafana_endpoint/d/lmcache-6x2"
echo "Prometheus: http://$published"
[ "$failures" -eq 0 ]
