# SPDX-License-Identifier: Apache-2.0
"""Connect private namespaces and create or restore a six-initiator cache array."""

# Standard
from pathlib import Path
from typing import Any
import argparse
import json
import subprocess
import sys
import uuid

# Third Party
from setup_mmgt_namespace_6x8 import command, validate_manifest


def private_members(manifest: dict[str, Any], host: str) -> list[dict[str, Any]]:
    """Validate ownership and return the host's eight stable namespace IDs."""
    validate_manifest(manifest)
    drives = {row["serial"]: row for row in manifest["drives"]}
    return [
        {
            **member,
            "host": host,
            "nqn": subsystem["nqn"],
            "bytes": drives[member["serial"]]["namespace_bytes"],
        }
        for subsystem in manifest["hosts"][host]["subsystems"]
        for member in subsystem["members"]
    ]


def connect(manifest: dict[str, Any], host: str) -> list[str]:
    """Connect two private RDMA subsystems and resolve all members by UUID."""
    row = manifest["hosts"][host]
    if Path("/etc/nvme/hostnqn").read_text().strip() != row["host_nqn"]:
        raise ValueError("Local host NQN differs from the manifest")
    for subsystem in row["subsystems"]:
        controllers = [
            path
            for path in Path("/sys/class/nvme").glob("nvme[0-9]*")
            if (path / "subsysnqn").read_text().strip() == subsystem["nqn"]
        ]
        if not controllers:
            command(
                [
                    "nvme",
                    "connect",
                    "-t",
                    "rdma",
                    "-a",
                    subsystem["address"],
                    "-s",
                    "4420",
                    "-n",
                    subsystem["nqn"],
                    "--nr-io-queues=128",
                    "--queue-size=128",
                    "--ctrl-loss-tmo=600",
                ]
            )
    command(["udevadm", "settle"])
    return resolve_members(private_members(manifest, host), Path("/sys/class/block"))


def resolve_members(expected: list[dict[str, Any]], block_root: Path) -> list[str]:
    """Resolve visible namespace heads by UUID, excluding hidden multipath disks.

    Raises ValueError when identities, exported ownership, or capacity differ.
    """
    devices = {}
    for path in block_root.glob("nvme*n*"):
        hidden = path / "hidden"
        if hidden.is_file() and hidden.read_text().strip() == "1":
            continue
        identity = path / "uuid"
        if not identity.is_file():
            continue
        value = identity.read_text().strip()
        if value not in {member["uuid"] for member in expected}:
            continue
        if value in devices:
            raise ValueError("Duplicate namespace UUID")
        member = next(member for member in expected if member["uuid"] == value)
        nqn = (path.resolve().parent / "subsysnqn").read_text().strip()
        if (
            nqn != member["nqn"]
            or int((path / "size").read_text()) * 512 != member["bytes"]
        ):
            raise ValueError("Connected namespace ownership or size differs")
        devices[value] = f"/dev/{path.name}"
    if len(devices) != 8:
        raise ValueError(f"Expected eight private namespaces; found {len(devices)}")
    return [devices[member["uuid"]] for member in expected]


def boot_config(manifest_path: Path, host: str, filesystem_uuid: str) -> None:
    """Persist a UUID mount and a reconnect/assemble service without formatting."""
    fstab = Path("/etc/fstab")
    lines = [
        line
        for line in fstab.read_text().splitlines()
        if line.lstrip().startswith("#")
        or len(line.split()) < 2
        or line.split()[1] != "/mnt/lmcache"
    ]
    lines.append(
        f"UUID={filesystem_uuid} /mnt/lmcache xfs defaults,noatime,noauto,nofail 0 0"
    )
    fstab.write_text("\n".join(lines) + "\n")
    config = Path("/etc/mdadm.conf")
    lines = []
    skipping = False
    for line in config.read_text().splitlines() if config.exists() else []:
        if line.startswith("ARRAY "):
            skipping = "/dev/md0 " in line or "/dev/md/0 " in line
        elif line and not line[0].isspace():
            skipping = False
        if not skipping:
            lines.append(line)
    lines.append(command(["mdadm", "--detail", "--scan", "/dev/md0"]))
    config.write_text("\n".join(lines) + "\n")
    script = Path(__file__).resolve()
    unit = Path("/etc/systemd/system/lmcache-ns6x8.service")
    unit.write_text(
        "[Unit]\nDescription=Connect and mount private LMCache namespaces\n"
        "Wants=network-online.target\nAfter=network-online.target\n"
        "StartLimitIntervalSec=0\n\n[Service]\nType=oneshot\nRemainAfterExit=yes\n"
        f"ExecStart=/usr/bin/python3 {script} restore "
        f"--manifest {manifest_path.resolve()} "
        f"--host {host}\nRestart=on-failure\nRestartSec=20\nTimeoutStartSec=180\n"
        "\n[Install]\nWantedBy=multi-user.target\n"
    )
    command(["systemctl", "daemon-reload"])
    command(["systemctl", "enable", "lmcache-ns6x8.service"])


def configure(manifest_path: Path, host: str, create: bool, destroy: bool) -> None:
    """Create a private array only when authorized, or restore its saved identity."""
    manifest = json.loads(manifest_path.read_text())
    private_members(manifest, host)
    if create and not destroy:
        raise ValueError("Creation requires --destroy-corpus")
    if any(not row.get("verified") for row in manifest["drives"]):
        raise ValueError("Target native namespace verification is incomplete")
    devices = connect(manifest, host)
    state_path = manifest_path.parent / f"{host}-array.json"
    md = Path("/dev/md0")
    if create:
        if md.exists() or state_path.exists():
            raise ValueError("Existing array or saved identity; refusing to format")
        for device in devices:
            path = Path("/sys/class/block") / Path(device).name
            if list((path / "holders").iterdir()):
                raise ValueError(f"{device}: namespace already has a holder")
            command(["wipefs", "--all", device])
        command(
            [
                "mdadm",
                "--create",
                str(md),
                "--metadata=1.2",
                "--level=0",
                "--raid-devices=8",
                "--chunk=512",
                f"--homehost={host}",
                "--name=lmcache",
                "--run",
                *devices,
            ]
        )
        command(
            [
                "mkfs.xfs",
                "-f",
                "-K",
                "-d",
                "agcount=32,su=512k,sw=8",
                "-l",
                "size=256m",
                "-m",
                "rmapbt=0,reflink=1",
                "-i",
                "nrext64=0",
                str(md),
            ]
        )
        identity = command(["blkid", "-s", "UUID", "-o", "value", str(md)])
        uuid.UUID(identity)
        state_path.write_text(
            json.dumps(
                {
                    "filesystem_uuid": identity,
                    "md_detail": command(["mdadm", "--detail", str(md)]),
                },
                indent=2,
            )
            + "\n"
        )
    else:
        state = json.loads(state_path.read_text())
        identity = state["filesystem_uuid"]
        if not md.exists():
            command(["mdadm", "--assemble", str(md), "--run", *devices])
        if command(["blkid", "-s", "UUID", "-o", "value", str(md)]) != identity:
            raise ValueError("Array filesystem UUID differs; refusing to mount")
    actual = {path.name for path in Path("/sys/class/block/md0/slaves").iterdir()}
    if actual != {Path(device).name for device in devices}:
        raise ValueError("Array membership differs from the private manifest")
    boot_config(manifest_path, host, identity)
    Path("/mnt/lmcache").mkdir(exist_ok=True)
    mounted = subprocess.run(
        ["findmnt", "-no", "UUID", "/mnt/lmcache"], capture_output=True, text=True
    )
    if mounted.returncode:
        command(
            ["mount", "-t", "xfs", "-o", "noatime", f"UUID={identity}", "/mnt/lmcache"]
        )
    elif mounted.stdout.strip() != identity:
        raise ValueError("Mount contains a different filesystem")
    if command(["findmnt", "-no", "UUID", "/mnt/lmcache"]) != identity:
        raise ValueError("Mounted filesystem UUID differs from the saved identity")
    print(f"{host}: eight private members, XFS UUID {identity}, mounted", flush=True)


def main() -> None:
    """Validate an offline plan, create storage, or restore it without formatting."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["validate", "create", "restore"])
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--host", required=True, choices=[f"mmgi{i}" for i in range(6)])
    parser.add_argument("--destroy-corpus", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "validate":
            print(
                json.dumps(
                    private_members(json.loads(args.manifest.read_text()), args.host)
                )
            )
        else:
            configure(
                args.manifest, args.host, args.action == "create", args.destroy_corpus
            )
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as error:
        print(f"ABORT: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
