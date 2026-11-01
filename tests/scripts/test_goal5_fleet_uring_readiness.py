# SPDX-License-Identifier: Apache-2.0
"""Check the restricted fleet policy and compatibility receipt contracts."""

# Standard
from pathlib import Path
from typing import Any
import importlib
import sys

# Third Party
import pytest


def test_restricted_policy_requires_aio_and_unprivileged_denial() -> None:
    """Root ring success cannot excuse failed standard AIO or broadened access."""
    sys.path.insert(0, str(Path(__file__).parents[2] / "scripts/ipu-poc"))
    module = importlib.import_module("goal5_fleet_uring_readiness")
    before = {
        "aio_setup_rc": 0,
        "aio_destroy_rc": 0,
        "uring_setup_rc": -1,
        "uring_errno": 1,
        "io_uring_disabled": "2",
        "io_uring_group": "-1",
        "uid": 0,
    }
    root = {**before, "io_uring_disabled": "1", "uring_setup_rc": 3, "uring_errno": 0}
    unprivileged = {**before, "io_uring_disabled": "1", "uid": 65534}
    module.validate_probes(before, root, unprivileged)
    module.validate_probes({**before, "uid": 65534}, root, unprivileged)
    unprivileged["aio_setup_rc"] = -11
    with pytest.raises(ValueError, match="AIO"):
        module.validate_probes(before, root, unprivileged)
    unprivileged["aio_setup_rc"] = 0
    unprivileged["uring_setup_rc"] = 3
    with pytest.raises(ValueError, match="unprivileged"):
        module.validate_probes(before, root, unprivileged)


def test_control_requires_matching_command_binding_and_achieved_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful exit must not accept a different engine or shallow control."""
    sys.path.insert(0, str(Path(__file__).parents[2] / "scripts/ipu-poc"))
    module = importlib.import_module("goal5_fleet_uring_readiness")
    monkeypatch.setattr(
        module, "summarize_fio", lambda result, mode: {"payload_gbps_decimal": 300.0}
    )
    row: dict[str, Any] = {
        "host": "mmgi2",
        "config_sha256": "bound",
        "command": ["fio", "--readonly"],
        "exit_code": 0,
        "result": {"jobs": [{"iodepth_level": {"32": 99.8}}]},
    }
    assert module.validate_control(row, "mmgi2", "bound", row["command"]) == 300.0
    row["result"]["jobs"][0]["iodepth_level"]["32"] = 98.0
    with pytest.raises(ValueError, match="depth"):
        module.validate_control(row, "mmgi2", "bound", row["command"])
    row["result"]["jobs"][0]["iodepth_level"]["32"] = 99.8
    row["config_sha256"] = "different"
    with pytest.raises(ValueError, match="binding"):
        module.validate_control(row, "mmgi2", "bound", row["command"])
