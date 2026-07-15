#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Emit a fail-closed hardware-provenance manifest for a benchmark host."""

from __future__ import annotations

import argparse
import ctypes
import importlib.metadata
import json
import mmap
import os
import platform
import re
import resource
import socket
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict


MANIFEST_VERSION = 1
_NUMA_NODE_RE = re.compile(r"\bN(?P<node>\d+)=(?P<pages>\d+)")
_IBV_FIELD_RE = re.compile(r"^\s*(?P<key>[^:]+):\s*(?P<value>.+?)\s*$")


class Allocation(TypedDict):
    region: str
    size_bytes: int
    resident_nodes: list[int]
    pages_off_node: int
    gate_failed: bool


def parse_alloc_region(value: str) -> tuple[str, int]:
    """Parse a ``name:size`` allocation specification.

    Args:
        value: Region name followed by a positive byte count.

    Returns:
        The region name and byte count.

    Raises:
        argparse.ArgumentTypeError: If the value is malformed.
    """
    name, separator, size_text = value.partition(":")
    if not separator or not name:
        raise argparse.ArgumentTypeError("allocation regions must use name:size")
    try:
        size = int(size_text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("allocation size must be an integer") from exc
    if size <= 0:
        raise argparse.ArgumentTypeError("allocation size must be positive")
    return name, size


def parse_numa_maps(contents: str) -> dict[int, int]:
    """Return total resident pages by NUMA node from ``/proc/*/numa_maps``."""
    pages: dict[int, int] = {}
    for match in _NUMA_NODE_RE.finditer(contents):
        node = int(match.group("node"))
        pages[node] = pages.get(node, 0) + int(match.group("pages"))
    return pages


def parse_numa_maps_for_address(contents: str, address: int) -> dict[int, int]:
    """Return NUMA pages for the VMA beginning at ``address``."""
    address_text = f"{address:x}"
    for line in contents.splitlines():
        fields = line.split()
        if fields and fields[0] == address_text:
            return parse_numa_maps(line)
    return {}


def parse_ibv_devinfo(contents: str) -> dict[str, str]:
    """Parse the ``key: value`` fields emitted by ``ibv_devinfo -v``."""
    fields: dict[str, str] = {}
    for line in contents.splitlines():
        match = _IBV_FIELD_RE.match(line)
        if match:
            fields[match.group("key").strip()] = match.group("value").strip()
    return fields


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _command(args: list[str]) -> str:
    result = subprocess.run(args, capture_output=True, check=False, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _git_value(args: list[str]) -> str:
    return _command(["git", *args])


def _node_cpus(node: int) -> set[int]:
    value = _read_text(Path(f"/sys/devices/system/node/node{node}/cpulist"))
    cpus: set[int] = set()
    for item in value.split(","):
        start_text, separator, end_text = item.partition("-")
        start = int(start_text)
        end = int(end_text) if separator else start
        cpus.update(range(start, end + 1))
    return cpus


def _cpu_affinity_node(expected_node: int) -> tuple[list[int], int | None]:
    affinity = sorted(os.sched_getaffinity(0))
    expected_cpus = _node_cpus(expected_node)
    if set(affinity).issubset(expected_cpus):
        return affinity, expected_node
    return affinity, None


def _nic_cpu_socket(node: int) -> int:
    cpu = min(_node_cpus(node))
    return int(
        _read_text(
            Path(f"/sys/devices/system/cpu/cpu{cpu}/topology/physical_package_id")
        )
    )


def _probe_allocation(name: str, size: int, expected_node: int) -> Allocation:
    """Fault a private anonymous VMA and attribute only its NUMA pages."""
    page_size = os.sysconf("SC_PAGE_SIZE")
    with mmap.mmap(-1, size, flags=mmap.MAP_PRIVATE | mmap.MAP_ANONYMOUS) as probe:
        address = ctypes.addressof(ctypes.c_char.from_buffer(probe))
        for offset in range(0, size, page_size):
            probe[offset] = 1
        resident = parse_numa_maps_for_address(
            _read_text(Path("/proc/self/numa_maps")), address
        )
    off_node = sum(pages for node, pages in resident.items() if node != expected_node)
    return {
        "region": name,
        "size_bytes": size,
        "resident_nodes": sorted(resident),
        "pages_off_node": off_node,
        "gate_failed": off_node > 0,
    }


def _env_values() -> dict[str, str]:
    prefixes = ("LMCACHE_", "NIXL_", "UCX_")
    return {key: value for key, value in os.environ.items() if key.startswith(prefixes)}


def _cli_args(args: argparse.Namespace) -> dict[str, object]:
    values: dict[str, object] = {}
    for name, value in vars(args).items():
        if isinstance(value, Path):
            values[name] = str(value)
        elif name == "alloc_region":
            values[name] = [
                {"name": region_name, "size_bytes": region_size}
                for region_name, region_size in value
            ]
        else:
            values[name] = value
    return values


def _hugepages_1g_free() -> int:
    contents = _read_text(Path("/proc/meminfo"))
    match = re.search(r"^HugePages_Free:\s+(?P<count>\d+)$", contents, re.MULTILINE)
    if not match:
        return 0
    hugepage_size = re.search(
        r"^Hugepagesize:\s+(?P<size>\d+) kB$", contents, re.MULTILINE
    )
    if not hugepage_size or int(hugepage_size.group("size")) != 1024 * 1024:
        return 0
    return int(match.group("count"))


def _optional_command(args: list[str]) -> str:
    try:
        return _command(args)
    except RuntimeError:
        return "unknown"


def _mlx5_core_version() -> str:
    version_path = Path("/sys/module/mlx5_core/version")
    if version_path.exists():
        return _read_text(version_path) or "unknown"
    return _optional_command(["modinfo", "-F", "version", "mlx5_core"]) or "unknown"


def collect_manifest(args: argparse.Namespace) -> dict[str, object]:
    """Collect a manifest and calculate all section gates.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Schema-v1 manifest object. Collector failures are represented as gate
        failures so callers can persist the diagnostic JSON before exiting.
    """
    failures: list[str] = []
    allocations = [
        _probe_allocation(name, size, args.numa_node)
        for name, size in args.alloc_region
    ]
    affinity, affinity_node = _cpu_affinity_node(args.numa_node)
    numa_failed = affinity_node != args.numa_node or any(
        allocation["gate_failed"] for allocation in allocations
    )
    if affinity_node != args.numa_node:
        failures.append("CPU affinity is not contained by the expected NUMA node")
    for allocation in allocations:
        if allocation["gate_failed"]:
            failures.append(
                f"{allocation['region']} has pages outside expected NUMA node"
            )

    device_path = Path(f"/sys/class/infiniband/{args.nic}/device")
    nic_node = int(_read_text(device_path / "numa_node"))
    nic_pci_addr = (device_path / "uevent").resolve().parent.name
    pcie_failed = nic_node != args.numa_node
    if pcie_failed:
        failures.append(
            f"NIC {args.nic} is on NUMA node {nic_node}, expected {args.numa_node}"
        )

    ibv = parse_ibv_devinfo(_command(["ibv_devinfo", "-d", args.nic, "-v"]))
    state = ibv.get("state", "")
    link_layer = ibv.get("link_layer", "")
    mtu = int(ibv.get("active_mtu", "0").split()[0])
    gid_path = Path(f"/sys/class/infiniband/{args.nic}/ports/1/gids/{args.gid_index}")
    gid_value = _read_text(gid_path)
    rdma_failed = not state.startswith("PORT_ACTIVE") or mtu < 4096 or not gid_value
    if rdma_failed:
        failures.append(
            "RDMA port is inactive, below 4096 MTU, or has no requested GID"
        )

    mlx5_version = _mlx5_core_version()
    python_version = platform.python_version()
    rdma_core_version = ibv.get("libibverbs", "unknown")
    try:
        nixl_version = importlib.metadata.version("nixl-cu12")
    except importlib.metadata.PackageNotFoundError:
        nixl_version = "unknown"
    software_failed = False

    memlock = resource.getrlimit(resource.RLIMIT_MEMLOCK)[0]
    memlock_value = "unlimited" if memlock == resource.RLIM_INFINITY else str(memlock)
    missing_env = [name for name in args.require_env if not os.environ.get(name)]
    runtime_failed = (
        memlock != resource.RLIM_INFINITY and memlock < args.min_memlock_bytes
    ) or bool(missing_env)
    if memlock != resource.RLIM_INFINITY and memlock < args.min_memlock_bytes:
        failures.append(f"memlock is below {args.min_memlock_bytes} bytes")
    if missing_env:
        failures.append(
            f"required environment variables are unset: {', '.join(missing_env)}"
        )

    manifest: dict[str, object] = {
        "manifest_version": MANIFEST_VERSION,
        "run": {
            "label": args.run_label,
            "started_utc": datetime.now(UTC).isoformat(),
            "host": socket.gethostname(),
            "role": args.role,
            "git_sha": _git_value(["rev-parse", "HEAD"]),
            "git_dirty": bool(_git_value(["status", "--porcelain"])),
        },
        "numa": {
            "expected_node": args.numa_node,
            "allocations": allocations,
            "cpu_affinity": affinity,
            "cpu_affinity_node": affinity_node,
            "gate_failed": numa_failed,
        },
        "pcie": {
            "nic": args.nic,
            "nic_numa_node": nic_node,
            "nic_pci_addr": nic_pci_addr,
            "nic_cpu_socket": _nic_cpu_socket(nic_node),
            "ipu_pci_addr": args.ipu_pci_addr,
            "ipu_numa_node": args.ipu_numa_node,
            "gate_failed": pcie_failed
            or (
                args.ipu_numa_node is not None and args.ipu_numa_node != args.numa_node
            ),
        },
        "rdma": {
            "device": args.nic,
            "port_state": state,
            "link_layer": link_layer,
            "mtu_active": mtu,
            "gid_index": args.gid_index,
            "fw_ver": ibv.get("fw_ver", ""),
            "gate_failed": rdma_failed,
        },
        "software": {
            "kernel": platform.release(),
            "mlx5_core_ver": mlx5_version,
            "rdma_core_ver": rdma_core_version,
            "python_ver": python_version,
            "nixl_ver": nixl_version,
            "gate_failed": software_failed,
        },
        "runtime": {
            "cli_args": _cli_args(args),
            "env": _env_values(),
            "ulimit_memlock": memlock_value,
            "hugepages_1g_free": _hugepages_1g_free(),
            "gate_failed": runtime_failed,
        },
    }
    sections = ("numa", "pcie", "rdma", "software", "runtime")
    overall_failed = any(
        isinstance(manifest[section], dict) and manifest[section]["gate_failed"]
        for section in sections
    )
    manifest["overall_gate_failed"] = overall_failed
    manifest["gate_failures"] = failures
    return manifest


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments for manifest collection."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--role", choices=("source", "storage"), required=True)
    parser.add_argument("--numa-node", type=int, required=True)
    parser.add_argument("--nic", required=True)
    parser.add_argument("--gid-index", type=int, required=True)
    parser.add_argument("--min-memlock-bytes", type=int, default=8 * 1024**3)
    parser.add_argument("--require-env", action="append", default=[])
    parser.add_argument(
        "--alloc-region", action="append", type=parse_alloc_region, default=[]
    )
    parser.add_argument("--ipu-pci-addr")
    parser.add_argument("--ipu-numa-node", type=int)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    """Collect, persist, and print a schema-v1 run manifest."""
    args = parse_args(argv)
    try:
        manifest = collect_manifest(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"bench_manifest: collector error: {exc}", file=sys.stderr)
        return 2
    rendered = json.dumps(manifest, indent=2, sort_keys=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(f"{rendered}\n", encoding="utf-8")
    print(rendered)
    return 1 if manifest["overall_gate_failed"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
