# SPDX-License-Identifier: Apache-2.0
"""Plan and provision the serial-pinned, NUMA-local six-initiator layout.

Native namespace creation destroys the cache corpus. Run provisioning separately
from benchmarks, after unmounting client arrays and disabling the old exports.
"""

# Standard
from pathlib import Path
from typing import Any
import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid

PORTS: list[dict[str, Any]] = [
    {
        "port": 1,
        "address": "200.0.5.2",
        "numa": 1,
        "hosts": ["mmgi0", "mmgi1", "mmgi5"],
        "serials": [
            "PHCP419600371P9AGN",
            "PHCP4195005J1P9AGN",
            "PHCP419600FQ1P9AGN",
            "PHCP4195003K1P9AGN",
        ],
    },
    {
        "port": 2,
        "address": "200.0.6.2",
        "numa": 1,
        "hosts": ["mmgi0", "mmgi1", "mmgi5"],
        "serials": [
            "PHCP420300541P9AGN",
            "PHCP419600B11P9AGN",
            "PHCP419600AY1P9AGN",
            "PHCP4196005Q1P9AGN",
        ],
    },
    {
        "port": 3,
        "address": "200.0.7.2",
        "numa": 0,
        "hosts": ["mmgi2", "mmgi3", "mmgi4"],
        "serials": [
            "PHCP4380000Y1P9AGN",
            "PHCP433400721P9AGN",
            "PHCP4334001N1P9AGN",
            "PHCP4334002V1P9AGN",
        ],
    },
    {
        "port": 4,
        "address": "200.0.8.2",
        "numa": 0,
        "hosts": ["mmgi2", "mmgi3", "mmgi4"],
        "serials": [
            "PHCP438000041P9AGN",
            "PHCP4321000P1P9AGN",
            "PHCP4334005X1P9AGN",
            "PHCP4321004H1P9AGN",
        ],
    },
]
PREFIX = "nqn.2026-10.io.lmcache.mmg.ns6x8"
CONFIGFS = Path("/sys/kernel/config/nvmet")


def command(args: list[str]) -> str:
    """Run a checked command and return stdout; raise on failure."""
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def inventory() -> list[dict[str, Any]]:
    """Read the allowlisted SSD identities and namespace geometry."""
    allowed = {serial for port in PORTS for serial in port["serials"]}
    rows = []
    for controller in Path("/sys/class/nvme").glob("nvme[0-9]*"):
        serial = (controller / "serial").read_text().strip()
        if serial not in allowed:
            continue
        device = f"/dev/{controller.name}"
        ctrl = json.loads(command(["nvme", "id-ctrl", device, "-o", "json"]))
        ns = json.loads(command(["nvme", "id-ns", device, "-n", "1", "-o", "json"]))
        grain = json.loads(
            command(["nvme", "id-ns-granularity", device, "-o", "json"])
        )["namespace-granularity-list"][0]
        lbaf = next(row for row in ns["lbafs"] if row["lbaf"] == ns["flbas"])
        if lbaf["ms"]:
            raise ValueError(f"{serial}: metadata LBA formats are unsupported")
        rows.append(
            {
                "serial": serial,
                "controller": device,
                "model": ctrl["mn"].strip(),
                "numa": int((controller / "device/numa_node").read_text()),
                "capacity_bytes": ctrl["tnvmcap"],
                "granularity_bytes": max(
                    grain["namespace-size-granularity"],
                    grain["namespace-capacity-granularity"],
                ),
                "lba_bytes": 1 << lbaf["ds"],
                "flbas": ns["flbas"],
                "cntlid": ctrl["cntlid"],
            }
        )
    return rows


def build_plan(
    devices: list[dict[str, Any]], host_nqns: dict[str, str]
) -> dict[str, Any]:
    """Validate media and generate exclusive namespace ownership.

    Args:
        devices: Allowlisted controller identities and byte geometry.
        host_nqns: Unique host NQN for each of mmgi0 through mmgi5.

    Returns:
        A JSON-serializable manifest with 48 UUIDs and two subsystems per host.

    Raises:
        ValueError: Inventory, topology, format, or host identities differ.
    """
    expected = {serial for port in PORTS for serial in port["serials"]}
    by_serial = {row["serial"]: row for row in devices}
    if set(by_serial) != expected or len(devices) != 16:
        raise ValueError("SSD serial inventory must match the 16-drive allowlist")
    hosts = {f"mmgi{i}" for i in range(6)}
    if set(host_nqns) != hosts or len(set(host_nqns.values())) != 6:
        raise ValueError("Six distinct host NQNs are required")
    if any(
        not value.startswith("nqn.") or len(value) > 223 for value in host_nqns.values()
    ):
        raise ValueError("Invalid host NQN")
    result: dict[str, Any] = {
        "schema": 1,
        "prefix": PREFIX,
        "hosts": {
            host: {"host_nqn": host_nqns[host], "subsystems": []}
            for host in sorted(hosts)
        },
        "drives": [],
    }
    for port in PORTS:
        for serial in port["serials"]:
            row = by_serial[serial]
            if row["model"] != "SOLIDIGM SB5PH27X019T":
                raise ValueError(f"{serial}: unexpected SSD model")
            if int(row["numa"]) != port["numa"]:
                raise ValueError(f"{serial}: unexpected NUMA locality")
            if row["lba_bytes"] != 512 or row["flbas"] != 0:
                raise ValueError(f"{serial}: expected 512-byte LBA format 0")
            grain = row["granularity_bytes"]
            if grain <= 0 or grain % 512:
                raise ValueError(f"{serial}: invalid byte granularity")
            if not re.fullmatch(r"/dev/nvme[0-9]+", row["controller"]):
                raise ValueError(f"{serial}: invalid controller device")
            size = row["capacity_bytes"] // (3 * grain) * grain
            if size <= 0:
                raise ValueError(f"{serial}: insufficient namespace capacity")
            result["drives"].append(
                {
                    **row,
                    "namespace_bytes": size,
                    "namespace_lbas": size // 512,
                    "owners": port["hosts"],
                    "namespace_ids": {},
                }
            )
        for host in port["hosts"]:
            members = []
            for nsid, serial in enumerate(port["serials"], 1):
                identity = str(
                    uuid.uuid5(uuid.NAMESPACE_URL, f"{PREFIX}/{serial}/{host}")
                )
                members.append({"serial": serial, "nsid": nsid, "uuid": identity})
            result["hosts"][host]["subsystems"].append(
                {
                    "nqn": f"{PREFIX}:ipu{port['port']}-{host}",
                    "port": port["port"],
                    "address": port["address"],
                    "numa": port["numa"],
                    "members": members,
                }
            )
    return result


def validate_manifest(manifest: dict[str, Any]) -> None:
    """Reject changed ownership or geometry before accessing hardware."""
    expected = build_plan(
        manifest["drives"],
        {host: row["host_nqn"] for host, row in manifest["hosts"].items()},
    )
    if manifest["hosts"] != expected["hosts"] or manifest["prefix"] != PREFIX:
        raise ValueError("Manifest ownership differs from the approved topology")
    for index, actual in enumerate(manifest["drives"]):
        planned = expected["drives"][index]
        for key in ("serial", "namespace_bytes", "namespace_lbas", "owners"):
            if actual[key] != planned[key]:
                raise ValueError(f"Manifest ownership or geometry changed: {key}")
        mappings = actual["namespace_ids"]
        if (
            not set(mappings).issubset(actual["owners"])
            or (actual.get("verified") and set(mappings) != set(actual["owners"]))
            or any(type(value) is not int or value <= 0 for value in mappings.values())
            or len(set(mappings.values())) != len(mappings)
        ):
            raise ValueError(
                "Native namespace ownership must be exclusive and complete"
            )


def save_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Atomically save provisioning progress on the OS filesystem."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(path)


def controller_for(serial: str) -> Path:
    """Resolve an allowlisted serial to its current PCIe controller."""
    matches = [
        path
        for path in Path("/sys/class/nvme").glob("nvme[0-9]*")
        if (path / "serial").read_text().strip() == serial
    ]
    if len(matches) != 1 or (matches[0] / "transport").read_text().strip() != "pcie":
        raise ValueError(f"{serial}: expected one local PCIe controller")
    return matches[0]


def allocated_ids(device: str) -> list[int]:
    """Return allocated native namespace IDs, including detached namespaces."""
    result = json.loads(command(["nvme", "list-ns", device, "-a", "-o", "json"]))
    return [row["nsid"] for row in result["nsid_list"]]


def namespace_device(controller: Path, nsid: int) -> Path:
    """Resolve a native namespace by sysfs NSID rather than device numbering."""
    matches = [
        path
        for path in controller.glob(f"{controller.name}n*")
        if (path / "nsid").is_file() and int((path / "nsid").read_text()) == nsid
    ]
    if len(matches) != 1:
        raise ValueError(f"{controller.name}: native NSID {nsid} is absent")
    return Path("/dev") / matches[0].name


def partition(manifest_path: Path, serial: str, destroy_corpus: bool) -> None:
    """Replace one allowlisted SSD's namespaces and verify reset persistence.

    Requires explicit corpus destruction, no exports, and no mounted devices or
    holders. Saves each allocated ID before proceeding. A partial operation
    requires inspection; rerunning refuses to delete the newly recorded IDs.
    """
    allowed = {value for port in PORTS for value in port["serials"]}
    if serial not in allowed:
        raise ValueError("SSD serial is outside the data-drive allowlist")
    if not destroy_corpus:
        raise ValueError("Namespace creation requires --destroy-corpus")
    manifest = json.loads(manifest_path.read_text())
    validate_manifest(manifest)
    drive = next(row for row in manifest["drives"] if row["serial"] == serial)
    if drive["namespace_ids"]:
        raise ValueError(f"{serial}: provisioning already started; inspect progress")
    controller = controller_for(serial)
    device = f"/dev/{controller.name}"
    ctrl = json.loads(command(["nvme", "id-ctrl", device, "-o", "json"]))
    if (
        ctrl["mn"].strip() != drive["model"]
        or ctrl["tnvmcap"] != drive["capacity_bytes"]
        or ctrl["cntlid"] != drive["cntlid"]
        or int((controller / "device/numa_node").read_text()) != drive["numa"]
    ):
        raise ValueError(f"{serial}: live controller identity changed")
    for export in CONFIGFS.glob("subsystems/*/namespaces/*"):
        if (export / "enable").read_text().strip() == "1":
            backing = Path((export / "device_path").read_text().strip())
            if backing.resolve().name.startswith(controller.name + "n"):
                raise ValueError(f"{serial}: namespace is still exported")
    for block in controller.glob(f"{controller.name}n*"):
        if not (block / "nsid").is_file():
            continue
        if list((block / "holders").iterdir()) or list(block.glob(block.name + "p*")):
            raise ValueError(f"{serial}: namespace has holders or partitions")
        mounts = json.loads(
            command(["lsblk", "-J", "-o", "NAME,MOUNTPOINTS", f"/dev/{block.name}"])
        )
        if any(
            value
            for row in mounts["blockdevices"]
            for value in row.get("mountpoints", [])
        ):
            raise ValueError(f"{serial}: namespace is mounted")
        command(["wipefs", "--all", f"/dev/{block.name}"])
    for nsid in allocated_ids(device):
        command(
            ["nvme", "detach-ns", device, "-n", str(nsid), "-c", str(ctrl["cntlid"])]
        )
        command(["nvme", "delete-ns", device, "-n", str(nsid)])
    for owner in drive["owners"]:
        before = set(allocated_ids(device))
        command(
            [
                "nvme",
                "create-ns",
                device,
                "--nsze",
                str(drive["namespace_lbas"]),
                "--ncap",
                str(drive["namespace_lbas"]),
                "--flbas",
                "0",
            ]
        )
        added = set(allocated_ids(device)) - before
        if len(added) != 1:
            raise ValueError(f"{serial}: creation did not yield one namespace")
        nsid = added.pop()
        drive["namespace_ids"][owner] = nsid
        save_manifest(manifest_path, manifest)
        command(
            ["nvme", "attach-ns", device, "-n", str(nsid), "-c", str(ctrl["cntlid"])]
        )
    command(["udevadm", "settle"])
    patterns = {}
    for owner, nsid in drive["namespace_ids"].items():
        ns = json.loads(
            command(["nvme", "id-ns", device, "-n", str(nsid), "-o", "json"])
        )
        if (
            ns["nsze"] != drive["namespace_lbas"]
            or ns["ncap"] != drive["namespace_lbas"]
            or ns["flbas"] != 0
        ):
            raise ValueError(f"{serial}/{owner}: namespace geometry differs")
        block = namespace_device(controller, nsid)
        pattern = (f"{serial}/{owner}".encode() * 256)[:4096].ljust(4096, b"\0")
        patterns[nsid] = pattern
        fd = os.open(block, os.O_RDWR | os.O_SYNC)
        try:
            for offset in (0, drive["namespace_bytes"] - 4096):
                if os.pwrite(fd, pattern, offset) != 4096:
                    raise ValueError("Short namespace write")
        finally:
            os.close(fd)
    command(["nvme", "reset", device])
    time.sleep(2)
    command(["udevadm", "settle"])
    if set(allocated_ids(device)) != set(patterns):
        raise ValueError(f"{serial}: namespace IDs changed after reset")
    for nsid, pattern in patterns.items():
        block = namespace_device(controller, nsid)
        command(["blockdev", "--flushbufs", str(block)])
        fd = os.open(block, os.O_RDONLY)
        try:
            for offset in (0, drive["namespace_bytes"] - 4096):
                if os.pread(fd, 4096, offset) != pattern:
                    raise ValueError(
                        f"{serial}: namespace isolation/reset check failed"
                    )
        finally:
            os.close(fd)
    drive["verified"] = True
    save_manifest(manifest_path, manifest)
    print(f"{serial}: three namespaces verified across controller reset", flush=True)


def export_namespaces(manifest_path: Path) -> None:
    """Create host-restricted kernel exports from a verified native manifest."""
    manifest = json.loads(manifest_path.read_text())
    validate_manifest(manifest)
    drives = {row["serial"]: row for row in manifest["drives"]}
    if any(not row.get("verified") for row in drives.values()):
        raise ValueError("All sixteen drives must pass native namespace verification")
    for host, row in manifest["hosts"].items():
        host_path = CONFIGFS / "hosts" / row["host_nqn"]
        host_path.mkdir(exist_ok=True)
        for subsystem in row["subsystems"]:
            path = CONFIGFS / "subsystems" / subsystem["nqn"]
            path.mkdir(exist_ok=True)
            (path / "attr_allow_any_host").write_text("0")
            acl = path / "allowed_hosts" / row["host_nqn"]
            if not acl.exists():
                acl.symlink_to(host_path)
            for member in subsystem["members"]:
                drive = drives[member["serial"]]
                block = namespace_device(
                    controller_for(drive["serial"]), drive["namespace_ids"][host]
                )
                if (
                    int(command(["blockdev", "--getsize64", str(block)]))
                    != drive["namespace_bytes"]
                ):
                    raise ValueError("Native namespace size differs from the manifest")
                ns = path / "namespaces" / str(member["nsid"])
                ns.mkdir(exist_ok=True)
                if (ns / "enable").read_text().strip() == "1":
                    if (ns / "device_uuid").read_text().strip() != member[
                        "uuid"
                    ] or Path(
                        (ns / "device_path").read_text().strip()
                    ).resolve() != block.resolve():
                        raise ValueError("Existing export UUID or backing differs")
                    continue
                (ns / "device_path").write_text(str(block))
                (ns / "device_uuid").write_text(member["uuid"])
                (ns / "device_nguid").write_text(str(uuid.UUID(member["uuid"])))
                (ns / "enable").write_text("1")
            port = CONFIGFS / "ports" / str(subsystem["port"])
            port.mkdir(exist_ok=True)
            for key, value in {
                "addr_trtype": "rdma",
                "addr_adrfam": "ipv4",
                "addr_traddr": subsystem["address"],
                "addr_trsvcid": "4420",
            }.items():
                if (port / key).read_text().strip() != value:
                    (port / key).write_text(value)
            link = port / "subsystems" / subsystem["nqn"]
            if not link.exists():
                link.symlink_to(path)
    print("Twelve host-restricted subsystems exported", flush=True)


def main() -> None:
    """Run the selected inventory, planning, or explicit provisioning action."""
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("inventory")
    plan = actions.add_parser("plan")
    plan.add_argument("--inventory", required=True, type=Path)
    plan.add_argument("--host-nqns", required=True, type=Path)
    provision = actions.add_parser("partition")
    provision.add_argument("--manifest", required=True, type=Path)
    provision.add_argument("--serial", required=True)
    provision.add_argument("--destroy-corpus", action="store_true")
    exports = actions.add_parser("export")
    exports.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    result: Any
    try:
        if args.action == "inventory":
            result = inventory()
        elif args.action == "plan":
            result = build_plan(
                json.loads(args.inventory.read_text()),
                json.loads(args.host_nqns.read_text()),
            )
        elif args.action == "partition":
            partition(args.manifest, args.serial, args.destroy_corpus)
            return
        else:
            export_namespaces(args.manifest)
            return
        print(json.dumps(result, indent=2))
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"ABORT: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
