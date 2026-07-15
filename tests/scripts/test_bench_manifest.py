# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the benchmark provenance manifest parsers."""

# Standard
import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "bench_manifest.py"
FIXTURES = Path(__file__).parent / "fixtures" / "manifest"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_manifest", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load bench_manifest.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_numa_maps_aggregates_nodes() -> None:
    """The NUMA parser aggregates all resident-page counters per node."""
    module = _load_module()

    pages = module.parse_numa_maps((FIXTURES / "numa_maps.txt").read_text())

    assert pages == {0: 4, 1: 8}


def test_parse_numa_maps_for_address_ignores_other_vmas() -> None:
    """Probe residency excludes unrelated interpreter mappings."""
    module = _load_module()
    contents = (FIXTURES / "numa_maps_contaminated.txt").read_text()

    pages = module.parse_numa_maps_for_address(contents, 0x7F2D00004000)

    assert pages == {1: 8}


def test_parse_ibv_devinfo_extracts_required_fields() -> None:
    """The RDMA parser preserves the fields used by schema-v1 gates."""
    module = _load_module()

    fields = module.parse_ibv_devinfo((FIXTURES / "ibv_devinfo.txt").read_text())

    assert fields["state"] == "PORT_ACTIVE (4)"
    assert fields["active_mtu"] == "4096 (5)"
    assert fields["link_layer"] == "Ethernet"
    assert fields["fw_ver"] == "20.43.1014"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("l1_pool:4096", ("l1_pool", 4096)), ("qp_buffer:1", ("qp_buffer", 1))],
)
def test_parse_alloc_region(value: str, expected: tuple[str, int]) -> None:
    """Allocation specifications accept named positive byte counts."""
    module = _load_module()

    assert module.parse_alloc_region(value) == expected


@pytest.mark.parametrize("value", ["missing", "name:0", "name:-1", ":4", "name:bad"])
def test_parse_alloc_region_rejects_invalid_values(value: str) -> None:
    """Allocation specifications reject malformed or non-positive byte counts."""
    module = _load_module()

    with pytest.raises(Exception, match="allocation"):
        module.parse_alloc_region(value)


def test_cli_args_are_json_serializable(tmp_path: Path) -> None:
    """Manifest runtime arguments convert paths and allocation tuples to JSON."""
    module = _load_module()
    args = module.parse_args(
        [
            "--run-label",
            "fixture",
            "--role",
            "storage",
            "--numa-node",
            "1",
            "--nic",
            "mlx5_0",
            "--gid-index",
            "3",
            "--min-memlock-bytes",
            "4096",
            "--alloc-region",
            "l1_pool:4096",
            "--output",
            str(tmp_path / "manifest.json"),
        ]
    )

    rendered = json.dumps(module._cli_args(args))

    assert "manifest.json" in rendered
    assert '"size_bytes": 4096' in rendered
    assert '"min_memlock_bytes": 4096' in rendered
