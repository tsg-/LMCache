#!/usr/bin/env bash
# Bring up SSH tunnels + Prom/Grafana stack for the MMG-400 observability rig.
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
    generated="$(openssl rand -hex 12)"
    umask 077
    printf 'GRAFANA_PASSWORD=%s\n' "$generated" >>.env
    echo "  generated a Grafana password into .env (mode 600)"
}

check_tunnel() {
    local port="$1"
    lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
}

# On Linux, Prometheus runs in Docker and reaches the host through the bridge
# gateway rather than the host loopback device. Binding each SSH forward to
# that gateway exposes it only to local Docker networks, not the management
# network. Docker Desktop continues to use the normal loopback binding.
if [ "$(uname -s)" = "Linux" ] && docker info >/dev/null 2>&1; then
    TUNNEL_BIND_ADDR="$(
        docker network inspect bridge \
            --format '{{(index .IPAM.Config 0).Gateway}}' 2>/dev/null || true
    )"
fi
TUNNEL_BIND_ADDR="${TUNNEL_BIND_ADDR:-127.0.0.1}"

# sha256sum (coreutils) is the safer default on Linux, where Perl's
# Digest::SHA -- what `shasum` needs -- is not guaranteed on a minimal
# image. macOS has no sha256sum by default, so fall back to shasum there.
sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | cut -d' ' -f1
    else
        shasum -a 256 "$1" | cut -d' ' -f1
    fi
}

# Prometheus reporting a scrape target `up` only proves node_exporter
# answered -- it says nothing about whether a specific textfile collector
# is still running. acc-transport.timer being silently disabled on mmgt for
# 22+ hours (2026-09-01) is exactly the failure this catches: node/mmgt
# stayed `up` the whole time on the strength of the other seven collectors.
# max_age is roughly 3x the collector's timer interval -- see
# host/systemd/*.timer and scripts/ipu-poc/acc-*.timer for the source
# values. Override with COLLECTOR_CHECKS="host:file:max_age_s ...".
# Set COLLECTOR_CHECKS='' for a forwarding-only monitoring key. Such a key
# cannot execute the remote stat commands used by this optional probe.
COLLECTOR_CHECKS="${COLLECTOR_CHECKS-\
mmgt:acc_transport.prom:30 mmgt:acc_stats.prom:90 mmgt:acc_grpc_acc1.prom:6 \
mmgt:acc_grpc_acc2.prom:6 mmgt:pcm_memory.prom:15 mmgt:pcm_pcie.prom:15 \
mmgt:numa_stats.prom:15 mmgt:nvme_stats.prom:45 mmgt:mmgt_nic.prom:15 \
mmgi0:acc_transport.prom:30 mmgi0:acc_stats.prom:90 \
mmgi1:acc_transport.prom:30 mmgi1:acc_stats.prom:90}"

check_collectors() {
    local textfile_dir=/var/lib/node_exporter/textfile
    local now host entry names spec name max_age path mtimes mtime age
    local stale_found=
    now="$(date +%s)"
    for host in mmgt mmgi0 mmgi1; do
        names=""
        for entry in $COLLECTOR_CHECKS; do
            [ "${entry%%:*}" = "$host" ] && names="$names ${entry#*:}"
        done
        [ -n "$names" ] || continue

        # One ssh call per host: stat every file in a single remote command
        # rather than one connection per file.
        mtimes="$(ssh -o BatchMode=yes -o ConnectTimeout=5 "$host" \
            "stat -c '%n %Y' $(for spec in $names; do
                printf '%s/%s ' "$textfile_dir" "${spec%%:*}"
            done) 2>/dev/null" 2>/dev/null || true)"

        for spec in $names; do
            name="${spec%%:*}"
            max_age="${spec##*:}"
            path="$textfile_dir/$name"
            mtime="$(echo "$mtimes" | awk -v p="$path" '$1==p {print $2}')"
            if [ -z "$mtime" ]; then
                echo "  $host: MISSING $name"
                stale_found=1
                continue
            fi
            age=$((now - mtime))
            if [ "$age" -gt "$max_age" ]; then
                echo "  $host: STALE $name (${age}s old, expected <${max_age}s)"
                stale_found=1
            fi
        done
    done
    if [ -z "$stale_found" ]; then
        echo "  all collectors fresh"
    fi
    # A stale or missing collector is informational, not fatal: it must
    # never trip `set -e` and abort tunnels/docker/Prometheus setup that
    # would otherwise be fine.
    return 0
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
        -L "$TUNNEL_BIND_ADDR:$local_port:127.0.0.1:$remote_port" "$host"
        echo "  tunnel $host:$remote_port -> :$local_port opened"
    fi
}

# Override for a different host set:
#   HOSTS="newhost1:19106 newhost2:19107" ./up.sh
# Keep the local ports aligned with the targets in prometheus.yml.
HOSTS="${HOSTS:-mmgt:19106 mmgi0:19107 mmgi1:19108}"

# Same for the lmcache_bench_mmg job, where the load runs on two initiator
# hosts. Local base per host is 19110 / 19120, so the last digit of the local
# port is the initiator id -- prometheus.yml relabels on exactly that.
#   MMG_BENCH_TUNNELS=1 ./up.sh                       # mmgi0+mmgi1 i0..3
#   MMG_BENCH_TUNNELS=1 MMG_BENCH_INITIATORS=2 ./up.sh
MMG_BENCH_HOSTS="${MMG_BENCH_HOSTS:-mmgi0:19110 mmgi1:19120}"
MMG_BENCH_INITIATORS="${MMG_BENCH_INITIATORS:-4}"
MMG_BENCH_REMOTE_BASE="${MMG_BENCH_REMOTE_BASE:-9101}"

echo "== credentials =="
ensure_password

echo "== SSH tunnels =="
for entry in $HOSTS; do
    ensure_tunnel "${entry%%:*}" "${entry##*:}" 9100
done

if [ -n "$COLLECTOR_CHECKS" ]; then
    echo "== collector freshness (mmgt/mmgi0/mmgi1) =="
    check_collectors
else
    echo "== collector freshness =="
    echo "  skipped (COLLECTOR_CHECKS='')"
fi

if [ -n "${MMG_BENCH_TUNNELS:-}" ]; then
    for entry in $MMG_BENCH_HOSTS; do
        for ((i = 0; i < MMG_BENCH_INITIATORS; i++)); do
            ensure_tunnel "${entry%%:*}" \
                "$((${entry##*:} + i))" "$((MMG_BENCH_REMOTE_BASE + i))"
        done
    done
else
    echo "  mmg bench tunnels skipped (MMG_BENCH_TUNNELS=1 to open" \
        "19110-$((19110 + MMG_BENCH_INITIATORS - 1)) and" \
        "19120-$((19120 + MMG_BENCH_INITIATORS - 1)))"
fi

echo "== docker stack =="
if ! docker info >/dev/null 2>&1; then
    if [ "$(uname -s)" = "Darwin" ]; then
        echo "  docker not running; starting Docker Desktop"
        open -a Docker
        for i in 1 2 3 4 5 6; do
            docker info >/dev/null 2>&1 && break
            sleep 10
        done
    else
        # No GUI app to launch on Linux; the daemon is a systemd service.
        echo "ERROR: Docker is not running. Start it (e.g. 'sudo systemctl" \
            "start docker') and re-run." >&2
        exit 1
    fi
fi
docker compose up -d

PROMETHEUS_ENDPOINT="$(docker compose port prometheus 9090 2>/dev/null || true)"
PROMETHEUS_ENDPOINT="${PROMETHEUS_ENDPOINT%%$'\n'*}"
[ -n "$PROMETHEUS_ENDPOINT" ] || {
    echo "ERROR: Prometheus has no published port" >&2
    exit 1
}
PROMETHEUS_URL="http://$PROMETHEUS_ENDPOINT"

host_prometheus_sha="$(sha256_file prometheus.yml)"
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
    if curl --noproxy '*' -fsS -X POST --max-time 5 \
        "$PROMETHEUS_URL/-/reload" >/dev/null; then
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
#
# "Down" has two causes that look identical unless the error text is read:
# the SSH tunnel itself is gone ("connect: connection refused" at the Docker
# gateway, since nothing is even listening on the local port), or the tunnel
# is fine and the remote metrics server just is not running right now between
# sweep cells (the tunnel accepts the TCP connection, then the far end resets
# it -- "EOF"). Only the first is actually broken. Conflating them is exactly
# how a dead tunnel went unnoticed for a full session.
export MMG_BENCH_TUNNELS_ENABLED="${MMG_BENCH_TUNNELS:+1}"
curl --noproxy '*' -s --max-time 5 \
    "$PROMETHEUS_URL/api/v1/targets?state=active" \
    | python3 -c "
import json, os, sys
d = json.load(sys.stdin)
tunnel_down = []
for t in sorted(d['data']['activeTargets'], key=lambda t: t['labels'].get('job', '')):
    lb = t['labels']
    job = lb.get('job', '?')
    who = lb.get('host', '?')
    if lb.get('initiator') is not None:
        who += f\" initiator={lb['initiator']}\"
    note = ''
    if job.startswith('lmcache_bench') and t['health'] != 'up':
        if not os.environ.get('MMG_BENCH_TUNNELS_ENABLED'):
            note = '  (bench tunnels not enabled)'
        elif 'connection refused' in t.get('lastError', ''):
            note = '  TUNNEL DOWN'
            tunnel_down.append(job)
        else:
            note = '  (expected unless a bench is running)'
    print(f\"  {job:14} {who:22} health={t['health']}{note}\")
if 'lmcache_bench_mmg' in tunnel_down:
    print('  -> run: MMG_BENCH_TUNNELS=1 ./up.sh')
"

echo
echo "Grafana:    http://127.0.0.1:3000  (user admin; password in .env)"
echo "Prometheus: $PROMETHEUS_URL"
echo "Dashboard:  http://127.0.0.1:3000/d/ipu-poc-mkp-stub"
