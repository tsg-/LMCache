# SPDX-License-Identifier: Apache-2.0
"""Smoke tests for the generic model-geometry benchmark helpers."""

# Future
from __future__ import annotations

# Standard
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[2]
SCRIPTS = ROOT / "scripts" / "ipu-poc"
RUNNER = SCRIPTS / "run_model_geometry.sh"
FANOUT = SCRIPTS / "run_geom_multi.sh"
READBACK = SCRIPTS / "geom_readback.py"
README = SCRIPTS / "README-model-geometry.md"
PROFILES = sorted((SCRIPTS / "models").glob("*.yaml"))

# An adapter type the registry cannot build, so a run that gets past argument
# validation fails fast instead of touching storage.
UNBUILDABLE_ADAPTER = '{"type": "no_such_adapter_for_tests"}'


def _run_script(
    script: Path,
    *args: str,
    **env: str,
) -> subprocess.CompletedProcess[str]:
    """Run a geometry helper against this checkout."""
    environment = os.environ | {
        "PYTHON": sys.executable,
        "PYTHONPATH": str(ROOT),
    }
    # The helpers read their whole configuration from the environment, so an
    # inherited value would silently change what these tests exercise.
    for name in (
        "BASE_PATH",
        "PREFIX",
        "WRITE_PREFIX",
        "L2_ADAPTER",
        "NUM_WORKERS",
        "WORKERS_TOTAL",
        "WORKERS_PER",
        "INITIATORS",
        "IN_FLIGHT",
        "ROUNDS",
        "WARMUP_ROUNDS",
        "DURATION_SEC",
        "WARMUP_SEC",
        "READ_WRITE_RATIO",
        "OUTPUT",
        "OUT",
    ):
        environment.pop(name, None)
    environment |= env
    return subprocess.run(
        ["bash", str(script), *args],
        check=False,
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=300,
    )


def _run_runner(*args: str) -> subprocess.CompletedProcess[str]:
    """Run the single-process geometry helper against this checkout."""
    return _run_script(RUNNER, *args)


def _run_readback(
    base_path: Path,
    key_prefix: str,
    profile: Path,
) -> subprocess.CompletedProcess[str]:
    """Byte-verify the first submit slot of a corpus."""
    return subprocess.run(
        [
            sys.executable,
            str(READBACK),
            "--base-path",
            str(base_path),
            "--key-prefix",
            key_prefix,
            "--profile",
            str(profile),
            "--submits",
            "1",
        ],
        check=False,
        cwd=ROOT,
        env=os.environ | {"PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        timeout=300,
    )


def _total_success(stdout: str) -> int:
    """Read the bench's successful-key count out of its result table."""
    line = next(
        line for line in stdout.splitlines() if line.startswith("Total success:")
    )
    return int(line.removeprefix("Total success:").strip())


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


def test_key_namespace_is_scoped_to_the_profile() -> None:
    """Each profile reads and writes a distinct key namespace under one prefix.

    Object keys carry no page size and a short read of a larger stored object
    counts as a hit, so an unscoped prefix would let one profile report another
    profile's corpus as its own.
    """
    namespaces = set()
    for profile in PROFILES:
        result = _run_script(
            RUNNER,
            "load",
            str(profile),
            PREFIX="shared-prefix",
            L2_ADAPTER=UNBUILDABLE_ADAPTER,
        )

        line = next(
            line
            for line in result.stdout.splitlines()
            if line.startswith("key namespace: ")
        )
        namespace = line.removeprefix("key namespace: ")
        assert namespace.startswith("shared-prefix-")
        namespaces.add(namespace)

    assert len(namespaces) == len(PROFILES)


def test_base_path_is_required_only_for_the_default_adapter() -> None:
    """A complete L2_ADAPTER JSON replaces fs_native, including its base path."""
    profile = str(PROFILES[0])

    without_adapter = _run_script(RUNNER, "load", profile, PREFIX="probe")
    assert without_adapter.returncode == 2
    assert "BASE_PATH must be set" in without_adapter.stderr

    with_adapter = _run_script(
        RUNNER,
        "load",
        profile,
        PREFIX="probe",
        L2_ADAPTER=UNBUILDABLE_ADAPTER,
    )
    assert "BASE_PATH must be set" not in with_adapter.stderr


def test_store_load_and_readback_agree_on_one_namespace(tmp_path: Path) -> None:
    """Store, load, and readback resolve the same namespace from one PREFIX.

    The helper scopes keys by profile SHA, so the operator-facing PREFIX stays
    the only value that has to be carried between the three tools.
    """
    profile = SCRIPTS / "models" / "deepseek_v3_fp8.yaml"
    shared = {"BASE_PATH": str(tmp_path), "PREFIX": "roundtrip"}

    store = _run_script(RUNNER, "store", str(profile), **shared)
    assert store.returncode == 0, store.stderr

    load = _run_script(RUNNER, "load", str(profile), **shared)
    assert load.returncode == 0, load.stderr
    assert _total_success(load.stdout) > 0

    readback = _run_readback(tmp_path, "roundtrip", profile)
    assert readback.returncode == 0, readback.stdout + readback.stderr
    assert "readback OK" in readback.stdout


def test_readback_rejects_an_unscoped_corpus(tmp_path: Path) -> None:
    """A corpus stored outside the helper is not silently accepted.

    Profiles that share a page size also share fill bytes over their common key
    range, so falling back to the unscoped namespace would let one profile
    report another profile's corpus as verified.
    """
    profile = SCRIPTS / "models" / "llama3_405b_fp8.yaml"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "lmcache.cli.main",
            "bench",
            "l2",
            "--l2-adapter",
            json.dumps(
                {
                    "type": "fs_native",
                    "base_path": str(tmp_path),
                    "use_odirect": True,
                    "num_workers": 8,
                }
            ),
            "--kvcache-shape-profile",
            str(profile),
            "--key-prefix",
            "unscoped",
            "--in-flight",
            "1",
            "--l1-align-bytes",
            "4096",
            "--only",
            "store",
            "--rounds",
            "1",
            "--warmup-rounds",
            "0",
        ],
        check=True,
        cwd=ROOT,
        env=os.environ | {"PYTHONPATH": str(ROOT)},
        capture_output=True,
        timeout=300,
    )

    # Same page size as the stored profile, fewer objects per submit.
    other = SCRIPTS / "models" / "mixtral_8x22b_fp8.yaml"
    for candidate in (profile, other):
        result = _run_readback(tmp_path, "unscoped", candidate)

        assert result.returncode == 1, result.stdout
        assert "readback OK" not in result.stdout


def test_a_profile_sharing_a_page_size_loads_no_objects(tmp_path: Path) -> None:
    """A same-page-size profile reads nothing from another profile's corpus.

    Llama-405B and Mixtral both use a 262144 B page and Mixtral's key range is a
    subset of Llama-405B's, so an unscoped prefix reports 56 of 56 hits here.
    Profiles with differing page sizes miss on length alone, which is why this
    pair is the one that detects a regression in the namespace scoping.
    """
    stored = SCRIPTS / "models" / "llama3_405b_fp8.yaml"
    same_page = SCRIPTS / "models" / "mixtral_8x22b_fp8.yaml"
    shared = {"BASE_PATH": str(tmp_path), "PREFIX": "equal-pages"}

    store = _run_script(RUNNER, "store", str(stored), **shared)
    assert store.returncode == 0, store.stderr
    assert _total_success(store.stdout) == 126

    load = _run_script(RUNNER, "load", str(same_page), **shared)
    assert _total_success(load.stdout) == 0


def test_readback_does_not_verify_a_corpus_from_another_profile(
    tmp_path: Path,
) -> None:
    """A profile's namespace holds only its own corpus, so readback misses."""
    stored = SCRIPTS / "models" / "deepseek_v3_fp8.yaml"
    other = SCRIPTS / "models" / "mixtral_8x22b_fp8.yaml"

    store = _run_script(
        RUNNER, "store", str(stored), BASE_PATH=str(tmp_path), PREFIX="roundtrip"
    )
    assert store.returncode == 0, store.stderr

    readback = _run_readback(tmp_path, "roundtrip", other)
    assert readback.returncode == 1
    assert "FAIL" in readback.stdout


def test_fanout_refuses_worker_budgets_it_cannot_apply() -> None:
    """Worker counts only reach the default adapter JSON, so they are rejected."""
    result = _run_script(
        FANOUT,
        "load",
        str(PROFILES[0]),
        PREFIX="probe",
        L2_ADAPTER=UNBUILDABLE_ADAPTER,
        WORKERS_TOTAL="16",
    )

    assert result.returncode == 2
    assert "cannot be applied to a supplied" in result.stderr
