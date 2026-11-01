# SPDX-License-Identifier: Apache-2.0
"""Exercise the full LMCache CLI and its real sustained slot lifecycle."""

# Standard
from pathlib import Path
from typing import Any
import hashlib
import json
import os
import subprocess
import sys
import uuid

# Third Party
import pytest


@pytest.mark.parametrize(
    ("profile_name", "size_kib"),
    [("mixtral_8x22b_fp8_64k.yaml", 64), ("mixtral_8x22b_fp8.yaml", 256)],
)
def test_bench_l2_profile_roundtrip_sustained_and_mixed(
    tmp_path: Path, profile_name: str, size_kib: int
) -> None:
    """Verify CLI profile resolution, event consumption, slot reuse and drain."""
    root = Path(__file__).resolve().parents[1]
    profile = root / "profiles" / profile_name
    identity = uuid.uuid4().hex[:12]
    config = json.loads((root / "adapter-config.json").read_text())
    config["adapter_params"]["base_path"] = str(tmp_path / "corpus")
    environment = os.environ.copy()
    environment["LMCACHE_DISABLE_BANNER"] = "1"
    environment["OMP_NUM_THREADS"] = "2"
    entry = (
        "import resource; "
        "soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE); "
        "resource.setrlimit(resource.RLIMIT_NOFILE,(max(soft,4096),hard)); "
        "from lmcache.cli.main import main; main()"
    )
    common = [
        sys.executable,
        "-c",
        entry,
        "bench",
        "l2",
        "--l2-adapter",
        json.dumps(config),
        "--kvcache-shape-profile",
        str(profile),
        "--l1-align-bytes",
        "4096",
        "--in-flight",
        "32",
        "--rounds",
        "2",
        "--warmup-rounds",
        "0",
        "--key-prefix",
        identity,
        "--format",
        "json",
    ]
    phases = {
        "roundtrip": ["--no-skip-verify", "--lookup-max-hit-rate", "1"],
        "sustained": ["--only", "load", "--duration-sec", "2", "--warmup-sec", "1"],
        "mixed": [
            "--duration-sec",
            "2",
            "--read-write-ratio",
            "5:1",
            "--write-key-prefix",
            identity + "-write",
            "--no-skip-verify",
        ],
    }
    for phase, arguments in phases.items():
        output = root / "logs" / f"bench-{size_kib}k-{identity}-{phase}.json"
        command = common + arguments + ["--output", str(output)]
        completed = subprocess.run(
            command,
            cwd=root,
            env=environment,
            text=True,
            capture_output=True,
            timeout=90,
        )
        output.with_suffix(".log").write_text(completed.stdout + completed.stderr)
        output.with_suffix(".command.json").write_text(json.dumps(command, indent=2))
        assert completed.returncode == 0, completed.stdout + completed.stderr
        metrics: dict[str, Any] = json.loads(output.read_text())["metrics"]
        assert metrics["config"]["num_keys"] == 56
        assert metrics["config"]["data_size_kb"] == size_kib
        assert metrics["config"]["in_flight"] == 32
        assert (
            metrics["geometry"]["profile_sha256"]
            == hashlib.sha256(profile.read_bytes()).hexdigest()
        )
        operations = [v for k, v in metrics.items() if k.startswith("op_")]
        assert len(operations) == {"roundtrip": 3, "sustained": 1, "mixed": 2}[phase]
        for result in operations:
            assert result["total_keys"] > 0
            assert result["total_success"] == result["total_keys"], result
            assert not result.get("timed_out", False), result
            assert result.get("rounds_timed_out", 0) == 0, result
            if phase != "roundtrip":
                assert result["submits"] > 32, result
                assert (
                    result["total_success_bytes"]
                    == result["total_keys"] * size_kib * 1024
                )
        if phase == "roundtrip":
            assert "[Verify] All 1792 keys data verified OK." in completed.stdout
        elif phase == "mixed":
            assert "[Verify] All 56 keys data verified OK." in completed.stdout
