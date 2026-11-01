# SPDX-License-Identifier: Apache-2.0
"""Preserve private XFS history before its separately authorized raw conversion."""

# Standard
from pathlib import Path
from typing import Any
import fcntl
import gzip
import hashlib
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import time

# Third Party
from goal5_overnight import config_digest, validate_host, write_json
from goal5_probe import command, inspect

MOUNT = Path("/mnt/lmcache")
STAGE = Path("/home/goal5-overnight/raw-uring-fleet-20261019-1807")
REFERENCE = (
    Path("/home/goal5-overnight/results")
    / "goal5-20261019-183000-kernel-xfs-mixtral64k-raw-reference"
)
REFERENCE_DIGEST = "65e07b9896663891c8f5d634e08a25f793135dd6e434d4c82efaeed03ed512c4"
RESTORE = "lmcache-ns6x8.service"


def private_fstab(text: str, uuid: str) -> str:
    """Comment only the single private UUID/XFS entry, preserving all other text.

    Args:
        text: Original fstab contents.
        uuid: Independently verified private array's XFS UUID.

    Returns:
        Contents with only the authorized mount entry commented.

    Raises:
        ValueError: The private entry is absent, duplicated or names another device.
    """
    lines = text.splitlines(keepends=True)
    matches = []
    for index, line in enumerate(lines):
        fields = line.split("#", 1)[0].split()
        if len(fields) >= 2 and fields[1] == str(MOUNT):
            matches.append(index)
            if len(fields) < 6 or fields[0] != f"UUID={uuid}" or fields[2] != "xfs":
                raise ValueError("Unexpected private mount entry")
    if len(matches) != 1:
        raise ValueError("Absent or duplicate private mount entry")
    index = matches[0]
    lines[index] = "# Goal5 raw owner: " + lines[index]
    return "".join(lines)


def inventory(host: str) -> dict[str, list[Any]]:
    """Inventory historical files and internal symlinks without following links."""
    result = {}
    for directory, subdirs, files in os.walk(MOUNT, followlinks=False):
        base = Path(directory)
        if base == MOUNT / host:
            subdirs[:] = [name for name in subdirs if name != "fio"]
        for name in subdirs + files:
            path = base / name
            info = path.lstat()
            if info.st_dev != MOUNT.stat().st_dev:
                raise ValueError(f"Unexpected historical filesystem: {path}")
            if stat.S_ISDIR(info.st_mode):
                continue
            if stat.S_ISLNK(info.st_mode):
                target = path.resolve()
                if not target.is_relative_to(MOUNT.resolve()) or target.is_relative_to(
                    (MOUNT / host / "fio").resolve()
                ):
                    raise ValueError(f"Unexpected historical link target: {path}")
                result[str(path.relative_to(MOUNT))] = [
                    0,
                    info.st_mtime_ns,
                    info.st_ino,
                    os.readlink(path),
                ]
                continue
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Unexpected historical entry: {path}")
            result[str(path.relative_to(MOUNT))] = [
                info.st_size,
                info.st_mtime_ns,
                info.st_ino,
            ]
    return result


def sha256(path: Path) -> str:
    """Hash a potentially large retained artifact without loading it into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024**2), b""):
            digest.update(chunk)
    return digest.hexdigest()


def preserve(host: str, directory: Path) -> dict[str, Any]:
    """Archive historical payloads and links; validate sizes, targets and gzip CRC."""
    entries = inventory(host)
    write_json(directory / "historical-kv-inventory.json", entries)
    archive = directory / "historical-kv.tar.gz"
    if archive.exists():
        raise ValueError("Historical archive identity already exists")
    with (directory / "archive.log").open("x") as log:
        compressor = "pigz -1 -p 8" if shutil.which("pigz") else "gzip -1"
        process = subprocess.Popen(
            [
                "tar",
                "--one-file-system",
                f"--exclude=./{host}/fio",
                "-I",
                compressor,
                "-cf",
                str(archive),
                "-C",
                str(MOUNT),
                ".",
            ],
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
        try:
            while process.poll() is None:
                fs = os.statvfs(directory)
                if fs.f_bavail * fs.f_frsize < 20 * 1024**3:
                    raise ValueError(
                        "Historical archive reached the OS free-space reserve"
                    )
                time.sleep(1)
            if process.returncode:
                raise ValueError(
                    "Historical archive failed; inspect retained archive.log"
                )
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=30)
    archived = {}
    # Streaming through the entire gzip also checks its CRC, including file payloads.
    with gzip.open(archive, "rb") as decompressed:
        with tarfile.open(fileobj=decompressed, mode="r|") as stream:
            for member in stream:
                if member.isdir():
                    continue
                name = member.name.removeprefix("./")
                if (
                    name not in entries
                    or name in archived
                    or member.size != entries[name][0]
                    or (
                        len(entries[name]) == 4
                        and (not member.issym() or member.linkname != entries[name][3])
                    )
                    or (len(entries[name]) == 3 and not member.isfile())
                ):
                    raise ValueError("Historical archive differs from its inventory")
                archived[name] = member.size
        while decompressed.read(8 * 1024**2):
            pass
    if len(archived) != len(entries) or inventory(host) != entries:
        raise ValueError("Historical files changed during preservation")
    receipt = {
        "host": host,
        "archive": str(archive),
        "sha256": sha256(archive),
        "compressed_bytes": archive.stat().st_size,
        "files": sum(len(row) == 3 for row in entries.values()),
        "symlinks": sum(len(row) == 4 for row in entries.values()),
        "payload_bytes": sum(row[0] for row in entries.values()),
        "inventory_sha256": sha256(directory / "historical-kv-inventory.json"),
        "excluded": f"{MOUNT}/{host}/fio",
        "scope": "All historical KV files and internal symlinks retained without "
        "following links; FIO scratch excluded. Paths/sizes/link targets and "
        "complete gzip integrity checked against an unchanged "
        "source inventory under the private array lock.",
    }
    write_json(directory / "preservation-receipt.json", receipt)
    return receipt


def conversion(request: dict[str, Any]) -> dict[str, Any]:
    """Archive, then unmount only the bound private XFS without changing MD/NVMe.

    Args:
        request: Bound raw configuration, host identity and explicit conversion scope.

    Returns:
        Before/after topology and preservation receipts.

    Raises:
        ValueError: Authorization, reference audit, ownership or runtime gates differ.
        subprocess.CalledProcessError: Archiving or a required OS operation fails.
    """
    cfg, host = request["cfg"], request["host"]
    if request.get("authorization") != "raw-private-md0-20261019-1442":
        raise ValueError("Exact private raw conversion authorization absent")
    audit = json.loads((REFERENCE / "validated-audit.json").read_text())
    if (
        audit["state"] != "validated"
        or audit["config_sha256"] != REFERENCE_DIGEST
        or audit["measurement_cells"] != 8
        or audit["measured_host_rows"] != 48
        or audit["reclamation_receipts"] != 24
        or sha256(REFERENCE / "validated-audit.json") != cfg["reference_audit_sha256"]
    ):
        raise ValueError("Matched reference validation incomplete")
    directory = STAGE / "conversion" / cfg["run_id"]
    directory.mkdir(parents=True, exist_ok=False)
    if (
        STAGE.resolve() != STAGE
        or directory.stat().st_dev == MOUNT.stat().st_dev
        or not stat.S_ISBLK(Path("/dev/md0").stat().st_mode)
    ):
        raise ValueError(
            "Historical archive must reside outside the private block array"
        )
    before = inspect(cfg)
    if (
        Path("/etc/nvme/hostnqn").read_text().strip()
        != cfg["manifest"]["hosts"][host]["host_nqn"]
    ):
        raise ValueError("Conversion host identity differs")
    # Raw sources are already bound, but XFS must still be intact at this gate.
    validate_host(before, {**cfg, "backend": "xfs"}, host)
    if (
        Path("/proc/sys/kernel/io_uring_disabled").read_text().strip() != "1"
        or Path("/proc/sys/kernel/io_uring_group").read_text().strip() != "-1"
    ):
        raise ValueError("Restricted io_uring policy differs")
    restore = command(["systemctl", "cat", RESTORE])
    if "ExecStop=" in restore or "ExecStopPost=" in restore:
        raise ValueError("Restore service has an unexpected teardown action")
    mount_uuid = before["filesystem_uuid"]
    fstab = Path("/etc/fstab")
    original = fstab.read_text()
    changed = private_fstab(original, mount_uuid)
    if command(["findmnt", "-n", "-o", "SOURCE", "--target", str(MOUNT)]) != "/dev/md0":
        raise ValueError("Private mount does not resolve to md0")
    md_size = int(command(["blockdev", "--getsize64", "/dev/md0"]))
    if cfg["raw_capacity_bytes"][host] != md_size - 512 * 1024**3:
        raise ValueError("Raw capacity does not reserve the bound FIO tail")
    with Path("/dev/md0").open("rb", buffering=0) as device:
        if device.read(4) != b"XFSB":
            raise ValueError("Private array no longer has the expected XFS header")
    write_json(directory / "before.json", before)
    (directory / "fstab.original").write_text(original)
    (directory / "restore.original").write_text(restore)
    preserved = preserve(host, directory)
    # Recheck all ownership/idle conditions immediately before the authorized unmount.
    current = inspect(cfg)
    validate_host(current, {**cfg, "backend": "xfs"}, host)
    if fstab.read_text() != original:
        raise ValueError("fstab changed during preservation")
    subprocess.run(["systemctl", "disable", "--now", RESTORE], check=True)
    temporary = fstab.with_name(f"fstab.goal5-{cfg['run_id']}.tmp")
    with temporary.open("x") as stream:
        stream.write(changed)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(stat.S_IMODE(fstab.stat().st_mode))
    temporary.replace(fstab)
    subprocess.run(["restorecon", str(fstab)], check=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["umount", str(MOUNT)], check=True)
    after = inspect(cfg)
    # The raw wrapper separately verifies the imported module and actual ring creation.
    after["io_uring_group"] = -1
    after["io_uring_ready"] = request["ring_ready"]
    validate_host(after, cfg, host)
    receipt = {
        "host": host,
        "config_sha256": config_digest(cfg),
        "reference_audit_sha256": sha256(REFERENCE / "validated-audit.json"),
        "authorization": request["authorization"],
        "preservation": preserved,
        "before": before,
        "after": after,
        "fstab_original_sha256": sha256(directory / "fstab.original"),
        "fstab_result_sha256": sha256(fstab),
        "raw_array_initialized": False,
    }
    write_json(directory / "conversion-receipt.json", receipt)
    return receipt


def main() -> None:
    """Serialize the exact private conversion and retain evidence on every failure."""
    request = json.load(sys.stdin)
    cfg, host = request["cfg"], request["host"]
    os.environ.update(cfg["hosts"][host]["bench_env"])
    # Third Party
    from goal5_raw_probe import gate
    from goal5_uring_readiness import probe

    gate(cfg, host)
    request["ring_ready"] = probe()["uring_setup_rc"] >= 0
    if not request["ring_ready"]:
        raise ValueError("Actual restricted root ring creation failed")
    with Path("/run/lock/lmcache-goal5-md0.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = conversion(request)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
