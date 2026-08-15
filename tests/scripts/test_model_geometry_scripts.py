# SPDX-License-Identifier: Apache-2.0
"""Smoke tests for the generic model-geometry benchmark helpers."""

# Future
from __future__ import annotations

# Standard
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[2]
SCRIPTS = ROOT / "scripts" / "ipu-poc"
RUNNER = SCRIPTS / "run_model_geometry.sh"
README = SCRIPTS / "README-model-geometry.md"
PROFILES = sorted((SCRIPTS / "models").glob("*.yaml"))


def _run_runner(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the geometry helper against this checkout."""
    environment = os.environ | {
        "PYTHON": sys.executable,
        "PYTHONPATH": str(ROOT),
    }
    return subprocess.run(
        ["bash", str(RUNNER), *args],
        check=False,
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
    )


def test_profiles_command_lists_every_profile() -> None:
    """The runner exposes every checked-in geometry profile."""
    result = _run_runner("profiles")

    assert result.returncode == 0, result.stderr
    for profile in PROFILES:
        assert profile.name in result.stdout


def test_show_resolves_every_profile() -> None:
    """Every checked-in profile is accepted by the generic runner."""
    for profile in PROFILES:
        result = _run_runner("show", str(profile))

        assert result.returncode == 0, result.stderr
        assert f"profile: {profile}" in result.stdout
        assert "objects/submit:" in result.stdout
        assert "page:" in result.stdout
        assert "submit:" in result.stdout


def test_readme_includes_an_example_for_every_profile() -> None:
    """The setup guide documents each profile the repository ships."""
    guide = README.read_text()

    for profile in PROFILES:
        assert profile.name in guide
