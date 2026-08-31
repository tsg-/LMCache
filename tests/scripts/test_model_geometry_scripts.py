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

# First Party
from lmcache.cli.commands.bench.l2_adapter_bench.geometry import (
    PROFILE_MODE_LEGACY,
    resolve_geometry_profile,
    resolve_submit_geometry,
)

ROOT = Path(__file__).parents[2]
SCRIPTS = ROOT / "scripts" / "ipu-poc"
RUNNER = SCRIPTS / "run_model_geometry.sh"
FANOUT = SCRIPTS / "run_geom_multi.sh"
INVENTORY_RUNNER = SCRIPTS / "run_geometry_inventory.sh"
READBACK = SCRIPTS / "geom_readback.py"
README = SCRIPTS / "README-model-geometry.md"
PROFILES = sorted((SCRIPTS / "models").glob("*.yaml"))
# The byte-accounting consumers derive application bytes from a single page
# size, so they apply to page-burst profiles only and must refuse the rest.
PAGE_BURST_PROFILES = [
    profile
    for profile in PROFILES
    if resolve_submit_geometry(str(profile)).profile_mode == PROFILE_MODE_LEGACY
]
OBJECT_GROUP_PROFILES = [
    profile for profile in PROFILES if profile not in PAGE_BURST_PROFILES
]
MMG_INVENTORY = SCRIPTS / "inventories" / "mmg-two-initiator.env"
# A byte-different copy of mixtral_8x22b_fp8.yaml with the same resolved
# geometry. Kept outside models/ so the sweep does not offer it as a model.
PAGETEST = SCRIPTS / "models" / "fixtures" / "mixtral_8x22b_pagetest_256k.yaml"

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
        "METRICS_PORT",
        "METRICS_BIND_ADDRESS",
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


def _run_runner_without_python(
    *args: str,
) -> subprocess.CompletedProcess[str]:
    """Run the helper without selecting an interpreter explicitly."""
    environment = os.environ | {"PYTHONPATH": str(ROOT)}
    environment.pop("PYTHON", None)
    return subprocess.run(
        ["bash", str(RUNNER), *args],
        check=False,
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=300,
    )


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
    """Every checked-in profile is accepted by the generic runner.

    The two geometry forms print different shape lines, and ``show`` is where
    an operator learns which one a file uses -- a page-burst profile has one
    page size, an object-group profile has a group breakdown instead. Neither
    may print the other's line, or the output would imply a shape the profile
    does not declare.
    """
    for profile in PROFILES:
        result = _run_runner("show", str(profile))

        assert result.returncode == 0, result.stderr
        assert f"profile: {profile}" in result.stdout
        assert "objects/submit:" in result.stdout
        assert "submit:" in result.stdout
        if profile in PAGE_BURST_PROFILES:
            assert "geometry: legacy_page_burst" in result.stdout
            assert "page:" in result.stdout
            assert "object group " not in result.stdout
        else:
            assert "geometry: object_group" in result.stdout
            assert "object group 0 " in result.stdout
            assert "kv ranks/chunk:" in result.stdout
            assert "page:" not in result.stdout


def test_the_corpus_covers_both_geometry_forms() -> None:
    """Both forms must stay represented, or the split tests go vacuous."""
    assert PAGE_BURST_PROFILES
    assert OBJECT_GROUP_PROFILES


def test_runner_discovers_the_checkout_venv_without_python_override() -> None:
    """The handoff script selects an installed checkout interpreter itself."""
    expected = next(
        (
            interpreter
            for name in (".venv-bench-l2", ".venv")
            if os.access(interpreter := ROOT / name / "bin" / "python", os.X_OK)
        ),
        None,
    )
    assert expected is not None, "no checkout virtual environment to discover"

    result = _run_runner_without_python("show", str(PROFILES[0]))

    assert result.returncode == 0, result.stderr
    assert f"python: {expected}" in result.stdout


def test_runner_forwards_an_optional_metrics_port(tmp_path: Path) -> None:
    """A sustained run can expose its live counters over loopback."""
    invocation_log = tmp_path / "invocations.txt"
    fake_python = tmp_path / "python"
    fake_python.write_text(
        '#!/usr/bin/env bash\nprintf \'%s\\n\' "$*" >> "$INVOCATION_LOG"\n'
    )
    fake_python.chmod(0o755)

    result = _run_script(
        RUNNER,
        "sustained-load",
        str(PAGE_BURST_PROFILES[0]),
        PYTHON=str(fake_python),
        INVOCATION_LOG=str(invocation_log),
        BASE_PATH=str(tmp_path / "storage"),
        PREFIX="metrics",
        METRICS_PORT="9101",
    )

    assert result.returncode == 0, result.stderr
    bench_invocation = next(
        line
        for line in invocation_log.read_text().splitlines()
        if "-m lmcache.cli.main bench l2" in line
    )
    assert "--serve-metrics 9101" in bench_invocation
    assert "--metrics-bind-address 127.0.0.1" in bench_invocation


def test_inventory_is_hostnames_only() -> None:
    """The MMG example identifies the physical initiators and target only."""
    result = _run_script(INVENTORY_RUNNER, "show", str(MMG_INVENTORY))

    assert result.returncode == 0, result.stderr
    assert "initiators: mmgi0 mmgi1" in result.stdout
    assert "target: mmgt" in result.stdout


def test_inventory_accepts_quoted_target_hostnames(tmp_path: Path) -> None:
    """The target hostname may use either shell quote style."""
    for target in ("'mmgt'", '"mmgt"'):
        inventory = tmp_path / "quoted-target.env"
        inventory.write_text(f"INITIATOR_HOSTS=(mmgi0)\nTARGET_HOST={target}\n")

        result = _run_script(INVENTORY_RUNNER, "show", str(inventory))

        assert result.returncode == 0, result.stderr
        assert "target: mmgt" in result.stdout


def test_inventory_rejects_a_storage_path_setting(tmp_path: Path) -> None:
    """A storage path cannot be hidden in the host inventory."""
    inventory = tmp_path / "invalid.env"
    inventory.write_text(
        "INITIATOR_HOSTS=(mmgi0 mmgi1)\nTARGET_HOST=mmgt\nBASE_PATH=/mnt/unsafe\n"
    )

    result = _run_script(INVENTORY_RUNNER, "show", str(inventory))

    assert result.returncode == 2
    assert "hostnames only" in result.stderr


def test_inventory_is_rejected_before_it_executes(tmp_path: Path) -> None:
    """The inventory is sourced, so the contract must hold before that."""
    marker = tmp_path / "executed"
    inventory = tmp_path / "hostile.env"
    inventory.write_text(f"INITIATOR_HOSTS=(mmgi0)\nTARGET_HOST=mmgt\ntouch {marker}\n")

    result = _run_script(INVENTORY_RUNNER, "show", str(inventory))

    assert result.returncode == 2
    assert "hostnames only" in result.stderr
    assert not marker.exists()


def test_inventory_rejects_command_substitution(tmp_path: Path) -> None:
    """A hostname list cannot be computed by running a command."""
    inventory = tmp_path / "substituted.env"
    inventory.write_text("INITIATOR_HOSTS=($(hostname))\nTARGET_HOST=mmgt\n")

    result = _run_script(INVENTORY_RUNNER, "show", str(inventory))

    assert result.returncode == 2
    assert "hostnames only" in result.stderr


def test_inventory_runner_advertises_corpus_verification() -> None:
    """The coordinator exposes a separate identity gate before a sweep."""
    result = _run_script(INVENTORY_RUNNER, "--help")

    assert result.returncode == 0, result.stderr
    assert "run_geometry_inventory.sh verify INVENTORY" in result.stdout


def test_inventory_sweep_uses_the_mmg_metrics_port(tmp_path: Path) -> None:
    """The MMG coordinator exposes each initiator's sustained-load endpoint."""
    inventory = tmp_path / "inventory.env"
    inventory.write_text("INITIATOR_HOSTS=(mmgi0 mmgi1)\nTARGET_HOST=mmgt\n")
    invocation_log = tmp_path / "remote-invocations.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "host=\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in mmgt|mmgi0|mmgi1) host=$arg ;; esac\n'
        "done\n"
        "body=$(cat)\n"
        'if [ "$host" = mmgt ]; then\n'
        "  printf '%s\\n' 200.0.0.37\n"
        'elif [[ "$body" == *findmnt* ]]; then\n'
        "  printf '%s\\n' /root/LMCache\n"
        "else\n"
        '  printf \'%s\\n\' "$body" >> "$INVOCATION_LOG"\n'
        "fi\n"
    )
    fake_ssh.chmod(0o755)

    result = _run_script(
        INVENTORY_RUNNER,
        "sweep",
        str(inventory),
        PATH=f"{fake_bin}:{os.environ['PATH']}",
        INVOCATION_LOG=str(invocation_log),
        RUN_ID="test-metrics",
    )

    assert result.returncode == 0, result.stderr
    assert invocation_log.read_text().count("METRICS_PORT=9101") == 2


def test_preflight_carries_every_target_address_to_the_initiator(
    tmp_path: Path,
) -> None:
    """A target exporting two addresses reaches the initiator as one argument.

    ssh joins its arguments into a single remote command string, so a newline
    between addresses is read as a command separator and only the first address
    survives.
    """
    inventory = tmp_path / "inventory.env"
    inventory.write_text("INITIATOR_HOSTS=(mmgi0)\nTARGET_HOST=mmgt\n")
    argument_log = tmp_path / "remote-arguments.txt"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_ssh = fake_bin / "ssh"
    fake_ssh.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "host=\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in mmgt|mmgi0) host=$arg ;; esac\n'
        "done\n"
        'if [ "$host" = mmgt ]; then\n'
        "  printf '%s\\n%s\\n' 200.0.6.2 200.0.5.2\n"
        "  exit 0\n"
        "fi\n"
        "cat >/dev/null\n"
        'printf "%s\\n" "${@: -1}" >> "$ARGUMENT_LOG"\n'
        "printf '%s\\n' /root/LMCache\n"
    )
    fake_ssh.chmod(0o755)

    result = _run_script(
        INVENTORY_RUNNER,
        "preflight",
        str(inventory),
        PATH=f"{fake_bin}:{os.environ['PATH']}",
        ARGUMENT_LOG=str(argument_log),
    )

    assert result.returncode == 0, result.stderr
    delivered = argument_log.read_text().strip()
    assert "200.0.6.2" in delivered
    assert "200.0.5.2" in delivered
    assert "\n" not in delivered


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
    profile = SCRIPTS / "models" / "mixtral_8x22b_fp8.yaml"
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

    # Same page size and burst depth as the stored profile.
    for candidate in (profile, PAGETEST):
        result = _run_readback(tmp_path, "unscoped", candidate)

        assert result.returncode == 1, result.stdout
        assert "readback OK" not in result.stdout


def test_pagetest_fixture_matches_the_profile_it_shadows() -> None:
    """The gate fixture differs from mixtral_8x22b_fp8 in nothing but its SHA.

    A fixture that drifted in page size or burst depth would still make the
    mismatch test pass, but on length rather than on key scoping, which is the
    property under test.
    """
    shadowed = SCRIPTS / "models" / "mixtral_8x22b_fp8.yaml"
    stored = resolve_geometry_profile(str(shadowed))
    fixture = resolve_geometry_profile(str(PAGETEST))

    assert fixture.objects_per_submit == stored.objects_per_submit
    assert fixture.page_size_bytes == stored.page_size_bytes
    assert fixture.tokens_per_chunk == stored.tokens_per_chunk
    assert fixture.sha256 != stored.sha256


def test_a_profile_sharing_a_page_size_loads_no_objects(tmp_path: Path) -> None:
    """A geometry-identical profile reads nothing from another one's corpus.

    The fixture resolves to the same 56 objects at a 262144 B page as the stored
    profile and differs only in SHA-256, so an unscoped prefix reports 56 of 56
    hits here. Profiles with differing page sizes or burst depths miss on length
    alone, which is why this pair is the one that detects a scoping regression.
    """
    stored = SCRIPTS / "models" / "mixtral_8x22b_fp8.yaml"
    shared = {"BASE_PATH": str(tmp_path), "PREFIX": "equal-pages"}

    store = _run_script(RUNNER, "store", str(stored), **shared)
    assert store.returncode == 0, store.stderr
    assert _total_success(store.stdout) == 56

    load = _run_script(RUNNER, "load", str(PAGETEST), **shared)
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


def test_readback_refuses_an_object_group_profile(tmp_path: Path) -> None:
    """Readback addresses one object per key index, so it refuses the rest.

    An object-group store writes one object per ``(chunk, object group, kv
    rank)``, so the path this tool builds from a key index names bytes the run
    never wrote. Refusing beats reporting a mismatch the operator would read as
    a corpus problem.
    """
    result = _run_readback(tmp_path, "any-prefix", OBJECT_GROUP_PROFILES[0])

    assert result.returncode == 2
    assert "object-group profile" in result.stdout + result.stderr


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
