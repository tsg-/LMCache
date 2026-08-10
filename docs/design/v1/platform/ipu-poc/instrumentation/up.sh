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
    local host="$1" local_port="$2"
    if check_tunnel "$local_port"; then
        echo "  tunnel $host -> :$local_port already up"
    else
        ssh -f -N -L "$local_port:127.0.0.1:9100" "$host"
        echo "  tunnel $host -> :$local_port opened"
    fi
}

# Override for a different pair of hosts:
#   HOSTS="newhost1:19100 newhost2:19101" ./up.sh
# Keep the local ports aligned with the targets in prometheus.yml.
HOSTS="${HOSTS:-mkp1:19100 mkp2:19101}"

echo "== credentials =="
ensure_password

echo "== SSH tunnels =="
for entry in $HOSTS; do
    ensure_tunnel "${entry%%:*}" "${entry##*:}"
done

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

echo "== health check =="
sleep 5
curl -s --max-time 5 'http://127.0.0.1:9090/api/v1/targets?state=active' \
    | python3 -c "
import json, sys
d = json.load(sys.stdin)
for t in d['data']['activeTargets']:
    print(f\"  {t['labels'].get('host','?')} health={t['health']}\")
"

echo
echo "Grafana:    http://127.0.0.1:3000  (user admin; password in .env)"
echo "Prometheus: http://127.0.0.1:9090"
echo "Dashboard:  http://127.0.0.1:3000/d/ipu-poc-mkp-stub"
