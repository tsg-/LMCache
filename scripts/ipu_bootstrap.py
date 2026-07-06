#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Two-node rendezvous helper for ``VerbsRdmaTransport`` bring-up.

Implements the workflow described in
``docs/design/v1/platform/verbs-transport.md`` ("QP Bootstrap / Two-Phase
Init") for real two-node hardware testing: each side runs a process (one
``target``, one ``initiator``) that creates its QP, writes a local RDMA
endpoint file, and polls for its peer's endpoint file — both keyed by a
shared ``LMCACHE_RDMA_NONCE``. Since the two sides run on different hosts,
their endpoint files must be shuttled across the network; this script
generates the shared nonce, launches both remote commands over SSH with the
matching environment, and relays each side's endpoint file to the other via
SCP until both sides report a connected QP (or the timeout elapses).

Usage::

    python scripts/ipu_bootstrap.py \\
        --initiator-host gpu-node-1 --initiator-cmd "python serve.py" \\
        --target-host kv-node-1 --target-cmd "python serve.py"

Requires passwordless SSH (key-based auth) to both hosts.
"""

from __future__ import annotations

from dataclasses import dataclass
import argparse
import secrets
import subprocess
import sys
import threading
import time

DEFAULT_ENDPOINT_DIR = "/tmp"
DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_CONNECT_TIMEOUT_SECONDS = 30.0
READY_LOG_MARKER = "VERBS_TRANSPORT: QP connected"


@dataclass(frozen=True)
class NodeSpec:
    """SSH target and launch command for one RDMA role."""

    role: str
    host: str
    command: str


def generate_nonce() -> str:
    """Return a random session nonce shared by both rendezvous sides."""
    return secrets.token_hex(8)


def peer_role(role: str) -> str:
    """Return the role on the other side of the rendezvous."""
    return "initiator" if role == "target" else "target"


def endpoint_path(role: str, nonce: str, endpoint_dir: str) -> str:
    """Return the endpoint file path ``VerbsRdmaTransport`` writes for *role*."""
    return f"{endpoint_dir}/lmcache_rdma_{role}_{nonce}.json"


def build_remote_env(node: NodeSpec, nonce: str, endpoint_dir: str) -> dict[str, str]:
    """Return the env vars to export on *node*'s host before running its command."""
    return {
        "LMCACHE_RDMA_ROLE": node.role,
        "LMCACHE_RDMA_NONCE": nonce,
        "LMCACHE_RDMA_ENDPOINT_FILE": endpoint_path(node.role, nonce, endpoint_dir),
        "LMCACHE_RDMA_PEER_ENDPOINT_FILE": endpoint_path(
            peer_role(node.role), nonce, endpoint_dir
        ),
    }


def build_ssh_argv(node: NodeSpec, env: dict[str, str]) -> list[str]:
    """Return the ``ssh`` argv that runs *node.command* remotely with *env* set."""
    env_str = " ".join(f"{key}={value}" for key, value in env.items())
    return ["ssh", node.host, f"env {env_str} {node.command}"]


def launch_remote(node: NodeSpec, env: dict[str, str]) -> subprocess.Popen:
    """Start *node.command* on *node.host* over SSH, merging stderr into stdout."""
    argv = build_ssh_argv(node, env)
    return subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )


def relay_endpoint_file(
    src_host: str, src_path: str, dst_host: str, dst_path: str
) -> bool:
    """Copy *src_host*:*src_path* to *dst_host*:*dst_path* via a local SCP hop.

    Returns:
        True if both the download and upload legs of the relay succeeded.
    """
    local_tmp = f"/tmp/.ipu_bootstrap_relay_{secrets.token_hex(4)}.json"
    download = subprocess.run(
        ["scp", "-q", f"{src_host}:{src_path}", local_tmp],
        capture_output=True,
    )
    if download.returncode != 0:
        return False
    upload = subprocess.run(
        ["scp", "-q", local_tmp, f"{dst_host}:{dst_path}"],
        capture_output=True,
    )
    subprocess.run(["rm", "-f", local_tmp], capture_output=True)
    return upload.returncode == 0


def watch_for_marker(
    proc: subprocess.Popen, marker: str, ready: threading.Event
) -> None:
    """Stream *proc*'s stdout to this process's stdout; set *ready* on *marker*."""
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        if marker in line:
            ready.set()


def run_rendezvous(
    initiator: NodeSpec,
    target: NodeSpec,
    nonce: str,
    endpoint_dir: str,
    timeout_seconds: float,
    dry_run: bool,
) -> int:
    """Launch both nodes, relay endpoint files, and wait for QP connect.

    Returns:
        Process exit code: 0 on success, 1 on rendezvous timeout.
    """
    initiator_env = build_remote_env(initiator, nonce, endpoint_dir)
    target_env = build_remote_env(target, nonce, endpoint_dir)

    if dry_run:
        print("nonce:", nonce)
        print("initiator:", " ".join(build_ssh_argv(initiator, initiator_env)))
        print("target:   ", " ".join(build_ssh_argv(target, target_env)))
        return 0

    initiator_proc = launch_remote(initiator, initiator_env)
    target_proc = launch_remote(target, target_env)

    initiator_ready = threading.Event()
    target_ready = threading.Event()
    watchers = [
        threading.Thread(
            target=watch_for_marker,
            args=(initiator_proc, READY_LOG_MARKER, initiator_ready),
            daemon=True,
        ),
        threading.Thread(
            target=watch_for_marker,
            args=(target_proc, READY_LOG_MARKER, target_ready),
            daemon=True,
        ),
    ]
    for watcher in watchers:
        watcher.start()

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline and not (
        initiator_ready.is_set() and target_ready.is_set()
    ):
        relay_endpoint_file(
            target.host,
            endpoint_path(target.role, nonce, endpoint_dir),
            initiator.host,
            endpoint_path(target.role, nonce, endpoint_dir),
        )
        relay_endpoint_file(
            initiator.host,
            endpoint_path(initiator.role, nonce, endpoint_dir),
            target.host,
            endpoint_path(initiator.role, nonce, endpoint_dir),
        )
        time.sleep(DEFAULT_POLL_INTERVAL_SECONDS)

    if initiator_ready.is_set() and target_ready.is_set():
        print("RDMA QP connected on both sides.", file=sys.stderr)
        return 0

    print("Rendezvous timed out waiting for QP connect.", file=sys.stderr)
    initiator_proc.terminate()
    target_proc.terminate()
    return 1


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse command-line arguments for the bootstrap helper."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--initiator-host", required=True, help="SSH host for the initiator role."
    )
    parser.add_argument(
        "--initiator-cmd", required=True, help="Command to run on the initiator host."
    )
    parser.add_argument(
        "--target-host", required=True, help="SSH host for the target role."
    )
    parser.add_argument(
        "--target-cmd", required=True, help="Command to run on the target host."
    )
    parser.add_argument(
        "--nonce",
        default=None,
        help="Shared rendezvous nonce (default: generated randomly).",
    )
    parser.add_argument(
        "--endpoint-dir",
        default=DEFAULT_ENDPOINT_DIR,
        help=f"Directory for endpoint files on both hosts (default: "
        f"{DEFAULT_ENDPOINT_DIR}).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_CONNECT_TIMEOUT_SECONDS,
        help=f"Seconds to wait for QP connect (default: "
        f"{DEFAULT_CONNECT_TIMEOUT_SECONDS}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the SSH commands that would be run without executing them.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Entry point: parse args, build node specs, run the rendezvous."""
    args = parse_args(argv)
    nonce = args.nonce or generate_nonce()
    initiator = NodeSpec(
        role="initiator", host=args.initiator_host, command=args.initiator_cmd
    )
    target = NodeSpec(role="target", host=args.target_host, command=args.target_cmd)
    return run_rendezvous(
        initiator, target, nonce, args.endpoint_dir, args.timeout, args.dry_run
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
