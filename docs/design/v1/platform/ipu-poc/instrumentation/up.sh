#!/usr/bin/env bash
# Bring up SSH tunnels + Prom/Grafana stack for mkp1/mkp2 observability.
# Idempotent: safe to re-run.
set -euo pipefail

cd "$(dirname "$0")"

# Grafana admin password. compose declares this required with no default,
# so a pushed kit ships no known credential. Generate a local secret into
# .env on first run; .env is gitignored and compose reads it implicitly.
ensure_password() {
    if [ -n "${GRAFANA_PASSWORD:-}" ]; then
        echo "  using GRAFANA_PASSWORD from the environment"
        return
    fi
    if [ -f .env ] && grep -q '^GRAFANA_PASSWORD=' .env; then
        echo "  using GRAFANA_PASSWORD from .env"
        return
    fi
    local generated
    generated="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24)"
    umask 077
    printf 'GRAFANA_PASSWORD=%s\n' "$generated" >>.env
    echo "  generated a Grafana password into .env (mode 600)"
}

check_tunnel() {
    local port="$1"
    lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
}

ensure_tunnel() {
    local host="$1" local_port="$2" remote_port="$3"
    if check_tunnel "$local_port"; then
        echo "  tunnel $host:$remote_port -> :$local_port already up"
    else
        # ExitOnForwardFailure: without it ssh backgrounds happily even when the
        # forward could not bind, leaving Prometheus scraping a dead local port
        # and reporting it as a target that is simply down.
        # A background ControlMaster accepts additional -L listeners but can
        # leave their remote forwarding channels stale. Every managed tunnel
        # therefore owns a direct SSH connection.
        ssh -f -N -o ControlMaster=no -o ControlPath=none \
            -o ExitOnForwardFailure=yes \
            -L "$local_port:127.0.0.1:$remote_port" "$host"
        echo "  tunnel $host:$remote_port -> :$local_port opened"
    fi
}

# Override for a different pair of hosts:
#   HOSTS="newhost1:19100 newhost2:19101" ./up.sh
# Keep the local ports aligned with the targets in prometheus.yml.
HOSTS="${HOSTS:-mkp1:19100 mkp2:19101}"

# Benchmark tunnels for the lmcache_bench job. Off by default: the endpoint
# exists only while a `bench l2` process runs, so opening these when no
# benchmark is planned just adds targets that are permanently down.
#   BENCH_TUNNELS=1 ./up.sh              # mkp1 initiators 0..3 -> :19102-19105
#   BENCH_TUNNELS=1 BENCH_INITIATORS=2 ./up.sh
# The drivers assign METRICS_BASE_PORT + id on the initiator host; these map to
# the four targets configured in prometheus.yml.
BENCH_HOST="${BENCH_HOST:-mkp1}"
BENCH_INITIATORS="${BENCH_INITIATORS:-4}"
BENCH_REMOTE_BASE="${BENCH_REMOTE_BASE:-9101}"
BENCH_LOCAL_BASE="${BENCH_LOCAL_BASE:-19102}"

echo "== credentials =="
ensure_password

echo "== SSH tunnels =="
for entry in $HOSTS; do
    ensure_tunnel "${entry%%:*}" "${entry##*:}" 9100
done

if [ -n "${BENCH_TUNNELS:-}" ]; then
    for ((i = 0; i < BENCH_INITIATORS; i++)); do
        ensure_tunnel "$BENCH_HOST" \
            "$((BENCH_LOCAL_BASE + i))" "$((BENCH_REMOTE_BASE + i))"
    done
else
    echo "  bench tunnels skipped (BENCH_TUNNELS=1 to open" \
        "$BENCH_LOCAL_BASE-$((BENCH_LOCAL_BASE + BENCH_INITIATORS - 1)))"
fi

echo "== docker stack =="
if ! docker info >/dev/null 2>&1; then
    echo "  docker not running; starting Docker Desktop"
    open -a Docker
    for i in 1 2 3 4 5 6; do
        docker info >/dev/null 2>&1 && break
        sleep 10
    done
fi
docker compose up -d

host_prometheus_sha="$(shasum -a 256 prometheus.yml | cut -d' ' -f1)"
container_prometheus_sha=
for i in 1 2 3 4 5; do
    container_prometheus_sha="$(
        docker compose exec -T prometheus \
            sh -c 'sha256sum /etc/prometheus/prometheus.yml' 2>/dev/null \
            | cut -d' ' -f1 || true
    )"
    [ -n "$container_prometheus_sha" ] && break
    sleep 1
done
[ -n "$container_prometheus_sha" ] || {
    echo "ERROR: Prometheus did not start"
    exit 1
}
if [ "$host_prometheus_sha" != "$container_prometheus_sha" ]; then
    # Replacing a single-file bind mount can leave Docker Desktop serving the
    # old inode. A reload would then succeed while retaining stale targets.
    echo "  mounted Prometheus config is stale; recreating Prometheus"
    docker compose up -d --force-recreate prometheus
fi

echo "== Prometheus reload =="
reloaded=
for i in 1 2 3 4 5; do
    if curl -fsS -X POST --max-time 5 http://127.0.0.1:9090/-/reload >/dev/null; then
        reloaded=1
        break
    fi
    sleep 1
done
[ -n "$reloaded" ] || {
    echo "ERROR: Prometheus did not accept a configuration reload"
    exit 1
}

echo "== health check =="
sleep 5
# lmcache_bench targets are expected DOWN unless a bench is running right now --
# the endpoint lives only as long as one `bench l2` process. Group by job so a
# down bench target does not read as a broken node scrape.
curl -s --max-time 5 'http://127.0.0.1:9090/api/v1/targets?state=active' \
    | python3 -c "
import json, sys
d = json.load(sys.stdin)
for t in sorted(d['data']['activeTargets'], key=lambda t: t['labels'].get('job', '')):
    lb = t['labels']
    who = lb.get('host', '?')
    if lb.get('initiator') is not None:
        who += f\" initiator={lb['initiator']}\"
    note = ''
    if lb.get('job') == 'lmcache_bench' and t['health'] != 'up':
        note = '  (expected unless a bench is running)'
    print(f\"  {lb.get('job','?'):14} {who:22} health={t['health']}{note}\")
"

echo
echo "Grafana:    http://127.0.0.1:3000  (user admin; password in .env)"
echo "Prometheus: http://127.0.0.1:9090"
echo "Dashboard:  http://127.0.0.1:3000/d/ipu-poc-mkp-stub"
