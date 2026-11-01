# SPDX-License-Identifier: Apache-2.0
"""Policy changes require completed evidence and healthy standard AIO controls."""

# Standard
from pathlib import Path
from types import ModuleType
import importlib.util
import json
import sys

# Third Party
import pytest


def driver() -> ModuleType:
    """Import the readiness helper without touching a kernel setting."""
    path = Path(__file__).parents[2] / "scripts/ipu-poc/goal5_uring_readiness.py"
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("goal5_uring_readiness", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_running_diagnostic_cannot_authorize_policy_change(tmp_path: Path) -> None:
    """A permission reply cannot bypass the in-progress measurement gate."""
    module = driver()
    (tmp_path / "status.json").write_text(json.dumps({"state": "running"}))
    with pytest.raises(ValueError, match="diagnostics"):
        module.diagnostic_gate({"diagnostic_cells": []}, tmp_path)


def test_complete_label_cannot_replace_original_rows(tmp_path: Path) -> None:
    """Require bound cell identities and raw evidence behind a complete label."""
    module = driver()
    (tmp_path / "status.json").write_text(
        json.dumps({"state": "complete", "cells": 12})
    )
    (tmp_path / "summary.json").write_text(json.dumps([]))
    cfg = {"diagnostic_cells": [{"identity": f"cell-{i}"} for i in range(12)]}
    with pytest.raises(ValueError, match="identities"):
        module.diagnostic_gate(cfg, tmp_path)


def test_healthy_aio_calls_do_not_excuse_clear_payload_regression() -> None:
    """The persistence gate rejects a clear post-change control regression."""
    module = driver()
    assert module.check_control_rates([200, 210], [204, 208]) > 0.99
    with pytest.raises(ValueError, match="regression"):
        module.check_control_rates([200, 210], [150, 160])
    with pytest.raises(ValueError, match="two"):
        module.check_control_rates([200], [210, 220])
