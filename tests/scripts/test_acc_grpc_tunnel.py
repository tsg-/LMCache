# SPDX-License-Identifier: Apache-2.0
"""Tests for the persistent MMG ACC gRPC tunnel command contract."""

# Standard
import importlib.util
from pathlib import Path


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts/ipu-poc/acc_grpc_tunnel.py"
TUNNEL_UNIT = ROOT / "scripts/ipu-poc/acc-grpc-tunnel@.service"
COLLECTOR_UNIT = ROOT / "scripts/ipu-poc/acc-grpc-telemetry@.service"
COLLECTOR_TIMER = ROOT / "scripts/ipu-poc/acc-grpc-telemetry@.timer"


def _load_module():
    """Load the standalone tunnel supervisor as a module."""
    spec = importlib.util.spec_from_file_location("acc_grpc_tunnel", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_master_command_stays_foreground_for_the_supervisor() -> None:
    """The supervisor, not SSH's fork mode, owns the password-bearing master."""
    tunnel = _load_module()

    command = tunnel.master_command(
        netns="IPU2",
        control_socket="/run/acc-grpc/acc1-imc.sock",
        imc_ip="100.0.0.100",
    )

    assert command[:4] == ["ip", "netns", "exec", "IPU2"]
    assert "-M" in command
    assert "-N" in command
    assert "-f" not in command
    assert command[-1] == "root@100.0.0.100"


def test_forward_command_binds_host_loopback_through_the_namespaced_proxy() -> None:
    """The collector stays on the host while the proxy enters the IPU namespace."""
    tunnel = _load_module()

    command = tunnel.forward_command(
        netns="IPU2",
        control_socket="/run/acc-grpc/acc1-imc.sock",
        imc_ip="100.0.0.100",
        acc_ip="192.168.96.2",
        fabric_ip="200.0.6.3",
        local_port=15001,
    )

    assert command[0] == "ssh"
    assert "-N" in command
    assert "-oExitOnForwardFailure=yes" in command
    assert "127.0.0.1:15001:200.0.6.3:50051" in command
    assert any(
        argument.startswith("ProxyCommand=")
        and "ip netns exec IPU2 ssh -S /run/acc-grpc/acc1-imc.sock" in argument
        and "-W %h:%p root@192.168.96.2" in argument
        for argument in command
    )


def test_proto_copy_reuses_the_persistent_imc_master() -> None:
    """Generated bindings cross the same credential-bearing hop as telemetry."""
    tunnel = _load_module()

    command = tunnel.proto_copy_command(
        netns="IPU2",
        control_socket="/run/acc-grpc/acc1-imc.sock",
        imc_ip="100.0.0.100",
        acc_ip="192.168.96.2",
        proto_source="/opt/falcon/tools/controller/python_out",
        proto_destination="/opt/acc-grpc-telemetry/proto",
        filename="telemetry_pb2.py",
    )

    assert command[0] == "scp"
    assert (
        "root@192.168.96.2:/opt/falcon/tools/controller/python_out/telemetry_pb2.py"
    ) in command
    assert "/opt/acc-grpc-telemetry/proto/telemetry_pb2.py" in command
    assert any(
        argument.startswith("ProxyCommand=")
        and "ip netns exec IPU2 ssh -S /run/acc-grpc/acc1-imc.sock" in argument
        and "-W %h:%p root@192.168.96.2" in argument
        for argument in command
    )


def test_shadow_units_keep_the_tunnel_and_collector_separate() -> None:
    """The fast gRPC path is installed as a non-disruptive shadow collector."""
    tunnel_unit = TUNNEL_UNIT.read_text(encoding="utf-8")
    collector_unit = COLLECTOR_UNIT.read_text(encoding="utf-8")
    collector_timer = COLLECTOR_TIMER.read_text(encoding="utf-8")

    assert "EnvironmentFile=/etc/default/acc-stats" in tunnel_unit
    assert "ExecStart=/usr/local/bin/acc_grpc_tunnel.py" in tunnel_unit
    assert "Requires=acc-grpc-tunnel@%i.service" in collector_unit
    assert "ExecStart=/usr/bin/env ${ACC_GRPC_PYTHON}" in collector_unit
    assert "acc-grpc-telemetry@%i.service" in collector_timer
    assert "OnUnitActiveSec=5s" in collector_timer
