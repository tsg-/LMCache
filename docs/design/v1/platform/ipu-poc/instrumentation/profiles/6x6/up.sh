#!/bin/bash
# Idempotently bring up the 6x6 telemetry additions: mmgt2 SSH tunnels and the
# lmcache-prom-6x6 container. Never touches the 6x2 or 4x4 stacks.
set -u
tun() {  # <local-port> <remote-port>
    ss -ltn | grep -q "172.17.0.1:$1 " && { echo "tunnel :$1 up"; return; }
    ssh -F /root/.ssh/lmcache-6x6-tunnels.conf lmcache6x6-mmgt2 -f -N \
        -o ControlMaster=no -o ControlPath=none -o ExitOnForwardFailure=yes \
        -o ServerAliveInterval=15 -o ServerAliveCountMax=4 \
        -L 172.17.0.1:$1:127.0.0.1:$2 && echo "tunnel :$1 started"
}
tun 19212 9100   # mmgt2 node_exporter
tun 19213 9188   # mmgt2 acc_grpc_exporter (acc5/acc6)
if ! docker ps -a --format '{{.Names}}' | grep -qx lmcache-prom-6x6; then
    docker run -d --name lmcache-prom-6x6 --restart unless-stopped \
        --network lmcache-telemetry_default --add-host host.docker.internal:host-gateway \
        -p 10.166.96.242:9092:9090 \
        -v /root/lmcache-telemetry-6x6/prometheus.yml:/etc/prometheus/prometheus.yml:ro \
        -v lmcache-telemetry-6x6_prom-data:/prometheus \
        prom/prometheus:v2.55.1 \
        --config.file=/etc/prometheus/prometheus.yml --storage.tsdb.path=/prometheus \
        --storage.tsdb.retention.time=90d --web.enable-lifecycle
else
    docker start lmcache-prom-6x6 >/dev/null; echo "lmcache-prom-6x6 running"
fi
