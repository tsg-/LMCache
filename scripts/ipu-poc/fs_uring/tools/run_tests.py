# SPDX-License-Identifier: Apache-2.0
"""Run real connector tests on ext4 or a newly owned XFS loop image."""

# Standard
from pathlib import Path
import argparse
import json
import os
import subprocess
import sys
import uuid


def run(command: list[str]) -> str:
    """Run a setup command, failing before dependent steps on errors."""
    return subprocess.check_output(command, text=True).strip()


def verify_loop(loop: str, image: Path) -> None:
    """Prove a loop device still belongs to this run's new disk image."""
    listing = json.loads(
        run(
            [
                "sudo",
                "-n",
                "losetup",
                "--list",
                "--json",
                "--output",
                "NAME,BACK-FILE",
                loop,
            ]
        )
    )
    devices = listing["loopdevices"]
    if len(devices) != 1 or Path(devices[0]["back-file"]).resolve() != image:
        raise RuntimeError("loop device no longer backs the owned test image")


def main() -> int:
    """Create isolated test storage, run pytest and detach owned loop storage."""
    parser = argparse.ArgumentParser(description=__doc__)
    storage = parser.add_mutually_exclusive_group()
    storage.add_argument("--xfs", action="store_true")
    storage.add_argument("--storage-root", type=Path)
    parser.add_argument("--asan", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    identity = uuid.uuid4().hex[:12]
    work = root / "test-work"
    work.mkdir(exist_ok=True)
    mount = work / f"xfs-{identity}"
    image = work / f"xfs-{identity}.img"
    loop = ""
    mounted = False
    result = -1
    filesystem = "xfs" if args.xfs else "ext4"
    environment = os.environ.copy()
    build = "build-asan" if args.asan else "build"
    environment["PYTHONPATH"] = str(root / build) + ":" + str(root / "lmcache-runtime")
    environment.pop("LMCACHE_DEVICE_BACKEND", None)
    if args.asan:
        environment["LD_PRELOAD"] = ":".join(
            run(["g++", "-print-file-name=" + name])
            for name in ("libasan.so", "libstdc++.so")
        )
        environment["ASAN_OPTIONS"] = "detect_leaks=0:abort_on_error=1"
        environment["UBSAN_OPTIONS"] = "halt_on_error=1"
    try:
        if args.xfs:
            mount.mkdir()
            with image.open("xb") as f:
                f.truncate(4 * 1024 * 1024 * 1024)
            loop = run(["sudo", "-n", "losetup", "--find", "--show", str(image)])
            verify_loop(loop, image)
            run(["sudo", "-n", "mkfs.xfs", "-f", loop])
            run(["sudo", "-n", "mount", loop, str(mount)])
            mounted = True
            run(["sudo", "-n", "chown", f"{os.getuid()}:{os.getgid()}", str(mount)])
            basetemp = mount / "pytest"
        elif args.storage_root:
            storage_root = args.storage_root.resolve(strict=True)
            filesystem = json.loads(
                run(["findmnt", "--json", "--target", str(storage_root)])
            )["filesystems"][0]["fstype"]
            basetemp = storage_root / f"pytest-{identity}"
        else:
            basetemp = work / f"ext4-{identity}"
        tests = "tests/test_connector.py" if args.asan else "tests"
        command = [
            str(root / ".venv/bin/python"),
            "-m",
            "pytest",
            tests,
            "-q",
            "-s",
            "--basetemp",
            str(basetemp),
        ]
        completed = subprocess.run(
            command, cwd=root, env=environment, text=True, capture_output=True
        )
        result = completed.returncode
        label = ("device-" if args.storage_root else "") + filesystem
        label += "-asan" if args.asan else "-full"
        (root / "logs" / f"{label}.log").write_text(completed.stdout + completed.stderr)
        print(completed.stdout)
        if result != 0:
            print(completed.stderr)
        return result
    finally:
        if mounted:
            verify_loop(loop, image)
            run(["sudo", "-n", "umount", str(mount)])
        if loop:
            verify_loop(loop, image)
            run(["sudo", "-n", "losetup", "--detach", loop])
        report = {
            "exit_code": result,
            "filesystem": filesystem,
            "storage_root": str(args.storage_root) if args.storage_root else None,
            "sanitizers": args.asan,
            "image": str(image) if args.xfs else None,
            "loop_detached": bool(loop),
        }
        (root / "logs" / f"validation-{identity}.json").write_text(
            json.dumps(report, indent=2) + "\n"
        )


if __name__ == "__main__":
    sys.exit(main())
