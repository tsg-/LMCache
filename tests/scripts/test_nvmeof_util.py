# SPDX-License-Identifier: Apache-2.0
"""Tests for scripts/nvmeof_util.py find-controller helper.

Covers both nvme-cli output shapes (v1 with ``Controllers[].Controller`` and
v2 with ``Paths[].Name``) via captured fixtures under
tests/scripts/fixtures/nvme_list_subsys/, plus the edge cases the shell
scripts depend on (absent NQN, malformed JSON).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


SCRIPTS_DIR = Path(__file__).parents[2] / "scripts"
FIXTURES = Path(__file__).parent / "fixtures" / "nvme_list_subsys"

_spec = importlib.util.spec_from_file_location(
    "nvmeof_util", SCRIPTS_DIR / "nvmeof_util.py"
)
assert _spec and _spec.loader
nvmeof_util = importlib.util.module_from_spec(_spec)
sys.modules["nvmeof_util"] = nvmeof_util
_spec.loader.exec_module(nvmeof_util)  # type: ignore[union-attr]


TARGET_NQN = "nqn.2026-07.io.lmcache.alt:bmg1"


# ---------------------------------------------------------------------------
# Pure Python API
# ---------------------------------------------------------------------------

def _load(name: str):
    return json.loads((FIXTURES / name).read_text())


def test_v1_controllers_shape_returns_controller_name():
    """nvme-cli 1.x wraps output as {Subsystems: [{Controllers: [...]}]}."""
    payload = _load("v1_controllers.json")
    assert nvmeof_util.find_controller(payload, TARGET_NQN) == "nvme0"


def test_v2_paths_shape_returns_path_name():
    """nvme-cli 2.x commonly reports controllers under Paths[].Name."""
    payload = _load("v2_paths.json")
    assert nvmeof_util.find_controller(payload, TARGET_NQN) == "nvme0"


def test_find_controller_returns_none_for_missing_nqn():
    for fixture in ("v1_controllers.json", "v2_paths.json", "empty.json"):
        payload = _load(fixture)
        assert (
            nvmeof_util.find_controller(payload, "nqn.does.not.exist") is None
        ), f"unexpected match in {fixture}"


def test_find_controller_skips_unrelated_subsystems():
    """v1 fixture includes nvme1 under a different NQN; must not confuse it."""
    payload = _load("v1_controllers.json")
    assert nvmeof_util.find_controller(payload, TARGET_NQN) == "nvme0"


def test_find_controller_handles_alternate_key_names():
    """Some nvme-cli versions use 'Subsystem NQN' rather than 'NQN'."""
    payload = {
        "Subsystems": [
            {
                "Subsystem NQN": TARGET_NQN,
                "Paths": [{"Name": "nvme9"}],
            }
        ]
    }
    assert nvmeof_util.find_controller(payload, TARGET_NQN) == "nvme9"


def test_find_controller_returns_none_on_empty_input():
    assert nvmeof_util.find_controller({}, TARGET_NQN) is None
    assert nvmeof_util.find_controller([], TARGET_NQN) is None
    assert nvmeof_util.find_controller(None, TARGET_NQN) is None


def test_find_controller_ignores_malformed_entries():
    """Non-dict subsystem entries and empty controller lists must not crash."""
    payload = {
        "Subsystems": [
            "not-a-dict",
            {"NQN": TARGET_NQN, "Controllers": [], "Paths": []},
            {"NQN": TARGET_NQN, "Controllers": [{"Controller": ""}]},
            {"NQN": TARGET_NQN, "Paths": [{"Name": "nvme7"}]},
        ]
    }
    assert nvmeof_util.find_controller(payload, TARGET_NQN) == "nvme7"


# ---------------------------------------------------------------------------
# CLI wiring (what the shell script actually invokes)
# ---------------------------------------------------------------------------

def _cli(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(SCRIPTS_DIR / "nvmeof_util.py"), *args],
        capture_output=True, text=True, input=stdin, check=False,
    )


def test_cli_reads_json_file():
    result = _cli(
        "find-controller",
        "--nqn", TARGET_NQN,
        "--json-file", str(FIXTURES / "v1_controllers.json"),
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "nvme0"


def test_cli_reads_json_from_stdin():
    payload = (FIXTURES / "v2_paths.json").read_text()
    result = _cli("find-controller", "--nqn", TARGET_NQN, stdin=payload)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "nvme0"


def test_cli_exits_1_when_nqn_absent():
    result = _cli(
        "find-controller",
        "--nqn", "nqn.absent.example:x",
        "--json-file", str(FIXTURES / "v1_controllers.json"),
    )
    assert result.returncode == 1
    assert result.stdout == ""


def test_cli_exits_2_on_malformed_json():
    result = _cli("find-controller", "--nqn", TARGET_NQN, stdin="{not json")
    assert result.returncode == 2
    assert "failed to read JSON" in result.stderr


def test_cli_reads_empty_subsystems_list():
    result = _cli(
        "find-controller",
        "--nqn", TARGET_NQN,
        "--json-file", str(FIXTURES / "empty.json"),
    )
    assert result.returncode == 1
    assert result.stdout == ""


def test_cli_requires_nqn_argument():
    result = _cli("find-controller", stdin="{}")
    assert result.returncode != 0
    assert "nqn" in result.stderr.lower()


@pytest.mark.parametrize("fixture", ["v1_controllers.json", "v2_paths.json"])
def test_cli_prints_only_the_device_name(fixture):
    """stdout must be exactly the controller name -- no prefix, no trailing junk."""
    result = _cli(
        "find-controller",
        "--nqn", TARGET_NQN,
        "--json-file", str(FIXTURES / fixture),
    )
    assert result.returncode == 0
    assert result.stdout == "nvme0\n"
