#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Maintain one MMG namespace-local tunnel to an ACC telemetry gRPC endpoint."""

from __future__ import annotations

# Standard
import argparse
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path


IMC_IP = "100.0.0.100"
ACC_IP = "192.168.96.2"
PROTO_FILENAMES = ("telemetry_pb2.py", "telemetry_pb2_grpc.py")


def master_command(netns: str, control_socket: str, imc_ip: str) -> list[str]:
    """Build the password-bearing IMC SSH control-master command."""
    return [
        "ip",
        "netns",
        "exec",
        netns,
        "ssh",
        "-oStrictHostKeyChecking=no",
        "-oUserKnownHostsFile=/dev/null",
        "-M",
        "-S",
        control_socket,
        "-N",
        f"root@{imc_ip}",
    ]


def _proxy_command(
    netns: str,
    control_socket: str,
    imc_ip: str,
    acc_ip: str,
) -> str:
    """Build the two-hop SSH proxy used after the IMC master is ready."""
    return (
        f"ip netns exec {netns} ssh -S {control_socket} "
        f"-oStrictHostKeyChecking=no "
        f"-oUserKnownHostsFile=/dev/null root@{imc_ip} "
        f"ssh -oStrictHostKeyChecking=no -oUserKnownHostsFile=/dev/null "
        f"-W %h:%p root@{acc_ip}"
    )


def forward_command(
    netns: str,
    control_socket: str,
    imc_ip: str,
    acc_ip: str,
    fabric_ip: str,
    local_port: int,
) -> list[str]:
    """Build the namespace-local ACC telemetry port-forward command."""
    return [
        "ssh",
        "-N",
        "-oExitOnForwardFailure=yes",
        "-oConnectTimeout=5",
        "-oStrictHostKeyChecking=no",
        "-oUserKnownHostsFile=/dev/null",
        "-o",
        f"ProxyCommand={_proxy_command(netns, control_socket, imc_ip, acc_ip)}",
        "-L",
        f"127.0.0.1:{local_port}:{fabric_ip}:50051",
        f"root@{acc_ip}",
    ]


def proto_copy_command(
    netns: str,
    control_socket: str,
    imc_ip: str,
    acc_ip: str,
    proto_source: str,
    proto_destination: str,
    filename: str,
) -> list[str]:
    """Build an SCP command that copies one generated binding from the ACC."""
    return [
        "scp",
        "-q",
        "-oStrictHostKeyChecking=no",
        "-oUserKnownHostsFile=/dev/null",
        "-o",
        f"ProxyCommand={_proxy_command(netns, control_socket, imc_ip, acc_ip)}",
        f"root@{acc_ip}:{proto_source}/{filename}",
        f"{proto_destination}/{filename}",
    ]


def _stage_proto(
    netns: str,
    control_socket: str,
    proto_dir: Path,
    proto_source: str,
) -> None:
    """Copy missing generated telemetry bindings through the live IMC master."""
    proto_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
    for filename in PROTO_FILENAMES:
        destination = proto_dir / filename
        if destination.is_file():
            continue
        result = subprocess.run(
            proto_copy_command(
                netns,
                control_socket,
                IMC_IP,
                ACC_IP,
                proto_source,
                str(proto_dir),
                filename,
            ),
            capture_output=True,
            check=False,
            text=True,
        )
        if result.returncode != 0:
            detail = result.stderr.strip()
            raise RuntimeError(f"could not stage {filename}: {detail}")
        destination.chmod(0o644)


def _read_master_until_ready(
    pid: int,
    master_fd: int,
    control_socket: str,
    password: str,
    netns: str,
    imc_ip: str,
) -> None:
    """Answer the IMC password prompt and wait for the SSH master socket."""
    deadline = time.monotonic() + 15
    output = bytearray()
    while time.monotonic() < deadline:
        if os.path.exists(control_socket):
            check = subprocess.run(
                [
                    "ip",
                    "netns",
                    "exec",
                    netns,
                    "ssh",
                    "-S",
                    control_socket,
                    "-O",
                    "check",
                    f"root@{imc_ip}",
                ],
                capture_output=True,
                check=False,
            )
            if check.returncode == 0:
                return

        ready, _, _ = select.select([master_fd], [], [], 0.2)
        if master_fd in ready:
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                chunk = b""
            if chunk:
                output.extend(chunk)
                if b"password" in chunk.lower():
                    os.write(master_fd, (password + "\n").encode())

        exited_pid, status = os.waitpid(pid, os.WNOHANG)
        if exited_pid:
            detail = output.decode(errors="replace").strip()
            raise RuntimeError(f"IMC SSH master exited with status {status}: {detail}")
    raise TimeoutError("timed out waiting for the IMC SSH master")


def _stop_process(process: subprocess.Popen[str] | None) -> None:
    """Terminate a subprocess without leaving a forwarding child behind."""
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def supervise(
    netns: str,
    fabric_ip: str,
    local_port: int,
    control_dir: Path,
    proto_dir: Path,
    proto_source: str,
    password: str,
) -> None:
    """Keep a namespace-local tunnel to one ACC telemetry endpoint alive."""
    control_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    control_socket = str(control_dir / "imc.sock")
    Path(control_socket).unlink(missing_ok=True)
    subprocess.run(
        ["ip", "netns", "exec", netns, "ip", "link", "set", "lo", "up"],
        check=True,
    )

    pid, master_fd = os.forkpty()
    if pid == 0:
        os.execvp(
            "ip",
            master_command(netns, control_socket, IMC_IP),
        )

    forward: subprocess.Popen[str] | None = None
    try:
        _read_master_until_ready(
            pid,
            master_fd,
            control_socket,
            password,
            netns,
            IMC_IP,
        )
        _stage_proto(netns, control_socket, proto_dir, proto_source)
        forward = subprocess.Popen(
            forward_command(
                netns,
                control_socket,
                IMC_IP,
                ACC_IP,
                fabric_ip,
                local_port,
            ),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        while True:
            if forward.poll() is not None:
                error = forward.stderr.read().strip() if forward.stderr else ""
                raise RuntimeError(f"ACC forward exited: {error}")
            exited_pid, status = os.waitpid(pid, os.WNOHANG)
            if exited_pid:
                raise RuntimeError(f"IMC SSH master exited with status {status}")
            time.sleep(0.2)
    finally:
        _stop_process(forward)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        Path(control_socket).unlink(missing_ok=True)


def _parse_args() -> argparse.Namespace:
    """Parse one ACC namespace tunnel configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--netns", required=True)
    parser.add_argument("--fabric-ip", required=True)
    parser.add_argument("--local-port", type=int, required=True)
    parser.add_argument("--control-dir", type=Path, required=True)
    parser.add_argument("--proto-dir", type=Path, required=True)
    parser.add_argument(
        "--proto-source",
        default="/opt/falcon/tools/controller/python_out",
    )
    return parser.parse_args()


def main() -> int:
    """Run the tunnel supervisor until systemd stops it."""
    args = _parse_args()
    password = os.environ.get("IMC_PASSWORD", "")
    if not password:
        print("IMC_PASSWORD is unset", file=sys.stderr)
        return 2
    if not 1 <= args.local_port <= 65535:
        print("--local-port must be in 1..65535", file=sys.stderr)
        return 2
    try:
        supervise(
            netns=args.netns,
            fabric_ip=args.fabric_ip,
            local_port=args.local_port,
            control_dir=args.control_dir,
            proto_dir=args.proto_dir,
            proto_source=args.proto_source,
            password=password,
        )
    except Exception as error:
        print(f"ACC gRPC tunnel failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
