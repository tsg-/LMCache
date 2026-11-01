# SPDX-License-Identifier: Apache-2.0

# Standard
from pathlib import Path
import importlib
import json
import subprocess
import sys

# Third Party
import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts/ipu-poc/setup_mmgt_namespace_6x8.py"
)
SERIAL_GROUPS = [
    [
        "PHCP419600371P9AGN",
        "PHCP4195005J1P9AGN",
        "PHCP419600FQ1P9AGN",
        "PHCP4195003K1P9AGN",
    ],
    [
        "PHCP420300541P9AGN",
        "PHCP419600B11P9AGN",
        "PHCP419600AY1P9AGN",
        "PHCP4196005Q1P9AGN",
    ],
    [
        "PHCP4380000Y1P9AGN",
        "PHCP433400721P9AGN",
        "PHCP4334001N1P9AGN",
        "PHCP4334002V1P9AGN",
    ],
    [
        "PHCP438000041P9AGN",
        "PHCP4321000P1P9AGN",
        "PHCP4334005X1P9AGN",
        "PHCP4321004H1P9AGN",
    ],
]


def fixtures() -> tuple[list[dict], dict[str, str]]:
    """Return a serial-pinned lab inventory and six distinct host identities."""
    inventory = []
    for group, serials in enumerate(SERIAL_GROUPS):
        for index, serial in enumerate(serials):
            inventory.append(
                {
                    "serial": serial,
                    "controller": f"/dev/nvme{group * 4 + index + 2}",
                    "model": "SOLIDIGM SB5PH27X019T",
                    "numa": 1 if group < 2 else 0,
                    "capacity_bytes": 1920383410176,
                    "granularity_bytes": 1073741824,
                    "lba_bytes": 512,
                    "flbas": 0,
                    "cntlid": 0,
                }
            )
    return inventory, {f"mmgi{i}": f"nqn.2026-10.test:host{i}" for i in range(6)}


def plan(
    tmp_path: Path, inventory: list[dict], hosts: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run the public planning command without accessing real hardware."""
    devices = tmp_path / "inventory.json"
    identities = tmp_path / "host-nqns.json"
    devices.write_text(json.dumps(inventory))
    identities.write_text(json.dumps(hosts))
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "plan",
            "--inventory",
            str(devices),
            "--host-nqns",
            str(identities),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def test_plan_assigns_private_members_through_local_ipus(tmp_path: Path) -> None:
    """Catch cross-NUMA allocation or reuse of a namespace by two owners."""
    inventory, hosts = fixtures()
    result = plan(tmp_path, inventory, hosts)
    assert result.returncode == 0, result.stderr
    manifest = json.loads(result.stdout)
    assert len(manifest["drives"]) == 16
    uuids = set()
    for host, row in manifest["hosts"].items():
        assert len(row["subsystems"]) == 2
        members = [
            member for subsystem in row["subsystems"] for member in subsystem["members"]
        ]
        assert len(members) == 8
        assert len({member["serial"] for member in members}) == 8
        for member in members:
            assert member["uuid"] not in uuids
            uuids.add(member["uuid"])
        assert {subsystem["address"] for subsystem in row["subsystems"]} == (
            {"200.0.5.2", "200.0.6.2"}
            if host in {"mmgi0", "mmgi1", "mmgi5"}
            else {"200.0.7.2", "200.0.8.2"}
        )
    assert len(uuids) == 48


def test_plan_rounds_capacity_down_in_bytes_then_converts_to_lbas(
    tmp_path: Path,
) -> None:
    """Catch rounding up, byte/LBA confusion, or an oversized third namespace."""
    inventory, hosts = fixtures()
    result = plan(tmp_path, inventory, hosts)
    assert result.returncode == 0, result.stderr
    for drive in json.loads(result.stdout)["drives"]:
        assert drive["namespace_bytes"] == 639950127104
        assert drive["namespace_lbas"] == 1249902592
        assert drive["namespace_bytes"] * 3 <= 1920383410176


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("serial", "BOOT-SSD", "serial"),
        ("model", "WD Green SN3000 1TB", "model"),
        ("numa", 0, "NUMA"),
        ("lba_bytes", 4096, "LBA"),
        ("granularity_bytes", 0, "granularity"),
    ],
)
def test_plan_refuses_unvalidated_media(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    """Catch allowing OS media, unexpected topology, or unsupported geometry."""
    inventory, hosts = fixtures()
    inventory[0][field] = value
    result = plan(tmp_path, inventory, hosts)
    assert result.returncode != 0
    assert message.lower() in result.stderr.lower()


def test_plan_refuses_duplicate_host_identity(tmp_path: Path) -> None:
    """Catch giving another host access through a reused host NQN."""
    inventory, hosts = fixtures()
    hosts["mmgi5"] = hosts["mmgi0"]
    result = plan(tmp_path, inventory, hosts)
    assert result.returncode != 0
    assert "host" in result.stderr.lower()


def test_partition_requires_explicit_corpus_destruction(tmp_path: Path) -> None:
    """Catch allowing a destructive operation without its explicit flag."""
    inventory, hosts = fixtures()
    result = plan(tmp_path, inventory, hosts)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(result.stdout)
    partition = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "partition",
            "--manifest",
            str(manifest),
            "--serial",
            SERIAL_GROUPS[0][0],
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert partition.returncode != 0
    assert "destroy-corpus" in partition.stderr


def test_partition_rejects_foreign_serial_before_hardware_access(
    tmp_path: Path,
) -> None:
    """Catch dispatching a media operation to a device outside the allowlist."""
    inventory, hosts = fixtures()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(plan(tmp_path, inventory, hosts).stdout)
    partition = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "partition",
            "--manifest",
            str(manifest),
            "--serial",
            "BOOT-SSD",
            "--destroy-corpus",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert partition.returncode != 0
    assert "serial" in partition.stderr.lower()


def test_partition_rejects_changed_ownership_manifest(tmp_path: Path) -> None:
    """Catch accepting a forged export that assigns a member to another host."""
    inventory, hosts = fixtures()
    data = json.loads(plan(tmp_path, inventory, hosts).stdout)
    data["hosts"]["mmgi0"]["subsystems"][0]["members"][0]["serial"] = SERIAL_GROUPS[2][
        0
    ]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(data))
    partition = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "partition",
            "--manifest",
            str(manifest),
            "--serial",
            SERIAL_GROUPS[0][0],
            "--destroy-corpus",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert partition.returncode != 0
    assert "ownership" in partition.stderr.lower()


@pytest.mark.parametrize("host", [f"mmgi{i}" for i in range(6)])
def test_initiator_plan_has_eight_private_identities(tmp_path: Path, host: str) -> None:
    inventory, nqns = fixtures()
    planned = plan(tmp_path, inventory, nqns)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(planned.stdout)
    script = SCRIPT.with_name("setup_initiator_namespace_6x8.py")
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "validate",
            "--manifest",
            str(manifest),
            "--host",
            host,
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    members = json.loads(result.stdout)
    assert len(members) == 8
    assert len({row["uuid"] for row in members}) == 8
    assert {row["host"] for row in members} == {host}


def test_resolve_members_uses_heads_with_multipath_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multipath sysfs includes hidden path disks with the head's UUID."""
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    setup = importlib.import_module("setup_initiator_namespace_6x8")
    block_root = tmp_path / "block"
    block_root.mkdir()
    expected = []
    for index in range(8):
        identity = f"00000000-0000-0000-0000-{index:012d}"
        nqn = f"nqn.2026-10.test:private{index // 4}"
        expected.append({"uuid": identity, "nqn": nqn, "bytes": 639950127104})
        subsystem = tmp_path / f"nvme-subsys{index}"
        subsystem.mkdir()
        (subsystem / "subsysnqn").write_text(nqn)
        for name, hidden in (
            (f"nvme{index + 1}n1", "0"),
            (f"nvme{index + 1}c1n1", "1"),
        ):
            disk = subsystem / name
            disk.mkdir()
            (disk / "uuid").write_text(identity)
            (disk / "size").write_text("1249902592")
            (disk / "hidden").write_text(hidden)
            (block_root / name).symlink_to(disk)
    assert setup.resolve_members(expected, block_root) == [
        f"/dev/nvme{index + 1}n1" for index in range(8)
    ]


def test_verified_manifest_rejects_shared_native_namespace(tmp_path: Path) -> None:
    """Different exported UUIDs cannot make one native namespace private."""
    inventory, hosts = fixtures()
    manifest = json.loads(plan(tmp_path, inventory, hosts).stdout)
    drive = manifest["drives"][0]
    drive["verified"] = True
    drive["namespace_ids"] = dict.fromkeys(drive["owners"], 1)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT.with_name("setup_initiator_namespace_6x8.py")),
            "validate",
            "--manifest",
            str(path),
            "--host",
            "mmgi0",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "native namespace" in result.stderr.lower()


def _connect_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: str):
    """Return the setup module with sysfs, hostnqn and commands redirected."""
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    setup = importlib.import_module("setup_initiator_namespace_6x8")
    inventory, hosts = fixtures()
    manifest = json.loads(plan(tmp_path, inventory, hosts).stdout)
    root = tmp_path / "root"
    (root / "etc/nvme").mkdir(parents=True)
    (root / "etc/nvme/hostnqn").write_text(manifest["hosts"][host]["host_nqn"])
    (root / "sys/class/nvme").mkdir(parents=True)
    real_path = setup.Path
    monkeypatch.setattr(
        setup,
        "Path",
        lambda p: root / str(p).lstrip("/") if str(p).startswith("/") else real_path(p),
    )
    calls: list[list[str]] = []
    monkeypatch.setattr(setup, "command", lambda argv: calls.append(argv) or "")
    monkeypatch.setattr(setup, "resolve_members", lambda expected, block_root: [])
    return setup, manifest, root, calls


def test_connect_can_route_every_subsystem_through_one_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A via-address and queue count override every manifest subsystem."""
    setup, manifest, _, calls = _connect_fixture(tmp_path, monkeypatch, "mmgi3")
    setup.connect(manifest, "mmgi3", nr_io_queues=1, via_address="200.0.5.2")
    connects = [argv for argv in calls if argv[:2] == ["nvme", "connect"]]
    assert len(connects) == len(manifest["hosts"]["mmgi3"]["subsystems"])
    for argv in connects:
        assert argv[argv.index("-a") + 1] == "200.0.5.2"
        assert "--nr-io-queues=1" in argv


def test_connect_defaults_keep_manifest_addresses_and_128_queues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without overrides the production connection is unchanged."""
    setup, manifest, _, calls = _connect_fixture(tmp_path, monkeypatch, "mmgi0")
    setup.connect(manifest, "mmgi0")
    connects = [argv for argv in calls if argv[:2] == ["nvme", "connect"]]
    expected = [s["address"] for s in manifest["hosts"]["mmgi0"]["subsystems"]]
    assert [argv[argv.index("-a") + 1] for argv in connects] == expected
    assert all("--nr-io-queues=128" in argv for argv in connects)


def test_connect_rejects_mismatched_existing_controller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing controller with other queues or address is not silently reused."""
    setup, manifest, root, calls = _connect_fixture(tmp_path, monkeypatch, "mmgi0")
    subsystem = manifest["hosts"]["mmgi0"]["subsystems"][0]
    ctrl = root / "sys/class/nvme/nvme7"
    ctrl.mkdir()
    (ctrl / "subsysnqn").write_text(subsystem["nqn"])
    (ctrl / "queue_count").write_text("129")
    (ctrl / "address").write_text(f"traddr={subsystem['address']},trsvcid=4420")
    with pytest.raises(ValueError, match="differs from the requested connection"):
        setup.connect(manifest, "mmgi0", nr_io_queues=1)
    with pytest.raises(ValueError, match="differs from the requested connection"):
        setup.connect(manifest, "mmgi0", via_address="200.0.9.9")
    assert not [argv for argv in calls if argv[:2] == ["nvme", "connect"]]


@pytest.mark.parametrize("queues,address", [(0, ""), (129, ""), (8, "not-an-ip")])
def test_connect_rejects_invalid_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queues: int, address: str
) -> None:
    """Out-of-range queue counts and malformed addresses fail before connecting."""
    setup, manifest, _, calls = _connect_fixture(tmp_path, monkeypatch, "mmgi0")
    with pytest.raises(ValueError):
        setup.connect(manifest, "mmgi0", nr_io_queues=queues, via_address=address)
    assert not calls
