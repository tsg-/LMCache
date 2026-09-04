# SPDX-License-Identifier: Apache-2.0
"""Regression checks for the instrumentation stack launcher."""

# Standard
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
INSTRUMENTATION = REPO_ROOT / "docs/design/v1/platform/ipu-poc/instrumentation"
UP = REPO_ROOT / "docs/design/v1/platform/ipu-poc/instrumentation/up.sh"


def test_password_generation_is_safe_with_pipefail() -> None:
    """The first-run password path must not trip an expected SIGPIPE."""
    source = UP.read_text(encoding="utf-8")

    assert 'openssl rand -hex 12' in source
    assert "tr -dc 'A-Za-z0-9' </dev/urandom | head -c 24" not in source


def test_mmg_monitoring_stack_uses_host_gateway_and_mmg_targets() -> None:
    """The checked-in stack must work from a Linux monitoring host."""
    compose = (INSTRUMENTATION / "docker-compose.yml").read_text(encoding="utf-8")
    prometheus = (INSTRUMENTATION / "prometheus.yml").read_text(encoding="utf-8")
    launcher = UP.read_text(encoding="utf-8")

    assert "host.docker.internal:host-gateway" in compose
    assert "host.docker.internal:19106" in prometheus
    assert "host.docker.internal:19107" in prometheus
    assert "host.docker.internal:19108" in prometheus
    assert "mkp1:19100" not in launcher
    assert "mkp2:19101" not in launcher
    assert "COLLECTOR_CHECKS=''" in launcher
    assert launcher.count("curl --noproxy '*'") == 2
    assert "docker network inspect bridge" in launcher
    assert 'TUNNEL_BIND_ADDR:$local_port' in launcher
    assert "MMG_BENCH_TUNNELS_ENABLED" in launcher


def test_launcher_discovers_the_published_prometheus_endpoint() -> None:
    """The launcher must not assume Prometheus binds to host loopback."""
    launcher = UP.read_text(encoding="utf-8")

    assert "docker compose port prometheus 9090" in launcher
    assert 'PROMETHEUS_URL="http://$PROMETHEUS_ENDPOINT"' in launcher
    assert '"$PROMETHEUS_URL/-/reload"' in launcher
    assert '"$PROMETHEUS_URL/api/v1/targets?state=active"' in launcher
    assert "Prometheus: $PROMETHEUS_URL" in launcher
