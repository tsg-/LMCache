# SPDX-License-Identifier: Apache-2.0
"""Contracts for the six-initiator namespace benchmark controller."""

# Standard
from copy import deepcopy
from pathlib import Path
from types import ModuleType
import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys

# Third Party
import pytest

SCRIPT = Path(__file__).parents[2] / "scripts/ipu-poc/goal5_overnight.py"


def controller() -> ModuleType:
    """Load the controller without accessing lab hosts."""
    if not SCRIPT.exists():
        pytest.fail("Goal5 controller is not implemented")
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("goal5_overnight", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def config(tmp_path: Path) -> dict:
    """Return one matched 64 KiB-object run, with a fixed corpus key universe."""
    return {
        "run_id": "goal5-20261018-221200-kernel-xfs-mixtral64k",
        "results_dir": str(tmp_path / "results"),
        "backend": "xfs",
        "target": "kernel",
        "profile": "scripts/ipu-poc/models/mixtral_8x22b_fp8_64k.yaml",
        "geometry": {
            "profile_sha256": "profile-sha",
            "objects_per_submit": 56,
            "max_object_bytes": 65536,
            "task_size_bytes": 3670016,
        },
        "in_flights": [1, 2, 4, 8],
        "dataset_submits": 256,
        "repetitions": 3,
        "warmup_sec": 90,
        "duration_sec": 60,
        "load_workers": 32,
        "iouring_queue_depth": 256,
        "max_phase_skew_sec": 5,
        "hosts": {
            f"mmgi{i}": {
                "repo": "/root/anthropic/LMCache" if i < 4 else "/root/LMCache",
                "python": "/usr/bin/python3",
                "numa": 1,
                "metrics_port": 9101 + i,
                "provenance": {"files": {"runner.py": "reviewed"}, "git_sha": "sha"},
            }
            for i in range(6)
        },
    }


def evidence(module: ModuleType, cfg: dict, host: str, successful: bool = True) -> dict:
    """Return a measured Load with successful-payload and wall-clock bounds."""
    return {
        "config_sha256": module.config_digest(cfg),
        "host": host,
        "cell": {"in_flight": 2, "repetition": 1, "mode": "load"},
        "command": module.benchmark_args(cfg, host, 2, "load"),
        "exit_code": 0,
        "result": {
            "metrics": {
                "config": {
                    "mode": "sustained",
                    "in_flight": 2,
                    "warmup_sec": 90,
                    "duration_sec": 60,
                    "l2_adapter_json": json.dumps(module.adapter(cfg, host)),
                },
                "geometry": {"profile_sha256": "profile-sha"},
                "op_0": {
                    "operation": "Load",
                    "total_keys": 560,
                    "total_success": 560 if successful else 559,
                    "total_success_bytes": 36700160,
                    "phase_start_time_seconds": 100,
                    "phase_end_time_seconds": 160,
                    "window_sec": 60,
                    "submit_latency_count": 10,
                    "submit_latency_p50_ms": 1.2,
                    "submit_latency_p99_ms": 2.5,
                },
            }
        },
    }


def test_all_in_flights_read_the_same_seeded_key_universe(tmp_path: Path) -> None:
    """Changing IF must not silently shrink or expand the corpus."""
    module = controller()
    cfg = config(tmp_path)
    for value, rounds in ((1, "256"), (2, "128"), (4, "64"), (8, "32")):
        argv = module.benchmark_args(cfg, "mmgi5", value, "load")
        assert argv[argv.index("--rounds") + 1] == rounds
        assert argv[argv.index("--in-flight") + 1] == str(value)
        assert argv[argv.index("--l1-align-bytes") + 1] == "4096"
        assert "--duration-sec" in argv
    cfg["in_flights"] = [3]
    with pytest.raises(ValueError, match="dataset"):
        module.cells(cfg)


@pytest.mark.parametrize("mode", ["load", "store", "verify", "canary"])
def test_commands_use_the_public_cli_parser(tmp_path: Path, mode: str) -> None:
    """Reject invented mode flags and check the byte-checking canary's CLI contract."""
    module = controller()
    source = SCRIPT.parents[2] / (
        "lmcache/cli/commands/bench/l2_adapter_bench/command.py"
    )
    spec = importlib.util.spec_from_file_location("bench_command", source)
    assert spec and spec.loader
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    parser = argparse.ArgumentParser()
    cli.add_l2_arguments(parser)
    parser.add_argument("--format")
    argv = module.benchmark_args(config(tmp_path), "mmgi0", 1, mode)
    args = parser.parse_args(argv[argv.index("l2") + 1 :])
    assert args.only == (
        None if mode == "canary" else ("store" if mode == "store" else "load")
    )
    assert args.skip_verify is (mode != "canary")


def test_partial_preparation_never_rewrites_an_existing_namespace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An interrupted store needs a fresh run rather than a destructive retry."""
    module = controller()
    cfg = config(tmp_path)

    def forbidden(*args: object) -> None:
        raise AssertionError("Preparation retry must not contact hosts")

    monkeypatch.setattr(module, "remote", forbidden)
    run = module.Run(cfg)
    attempt = Path(cfg["results_dir"]) / "store-if1-r0/attempt-0001"
    attempt.mkdir(parents=True)
    with pytest.raises(ValueError, match="fresh"):
        run.phase(1, 0, "store")


def test_canary_selects_load_after_store_and_lookup(tmp_path: Path) -> None:
    """The real CLI's three-operation result must accept a byte-checked canary."""
    module = controller()
    cfg = config(tmp_path)
    row = evidence(module, cfg, "mmgi0")
    row["cell"] = {"in_flight": 1, "repetition": 0, "mode": "canary"}
    row["command"] = module.benchmark_args(cfg, "mmgi0", 1, "canary")
    metrics = row["result"]["metrics"]
    metrics["config"].update(mode="rounds", in_flight=1)
    load = metrics.pop("op_0")
    load.update(total_keys=56, total_success=56)
    metrics.update(
        op_0={**load, "operation": "Store"},
        op_1={**load, "operation": "Lookup"},
        op_2=load,
    )
    assert module.validate_evidence(row, cfg, "mmgi0", 1, 0, "canary")


@pytest.mark.skipif(
    sys.platform != "linux", reason="Process ownership uses Linux /proc"
)
def test_cleanup_refuses_foreign_pid_and_stops_only_owned_process() -> None:
    """A child survives a foreign token and stops for its owner."""
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location(
        "goal5_probe", SCRIPT.with_name("goal5_probe.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        start_new_session=True,
        env={**os.environ, "GOAL5_TOKEN": "owned-test"},
    )
    try:
        with pytest.raises(ValueError, match="owned"):
            module.stop_owned(child.pid, "foreign-test")
        assert child.poll() is None
        assert module.stop_owned(child.pid, "owned-test")
        assert child.wait(timeout=5) != 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


@pytest.mark.parametrize("backend", ["raw_posix", "raw_io_uring"])
def test_raw_raid_uses_normal_io_and_reserves_header_space(
    tmp_path: Path, backend: str
) -> None:
    module = controller()
    cfg = config(tmp_path)
    cfg["backend"] = backend
    adapter = module.adapter(cfg, "mmgi0")
    assert adapter["device_path"] == "/dev/md0"
    assert adapter["slot_bytes"] == 69632
    assert adapter["use_odirect"] is True
    assert adapter["use_uring_cmd"] is False
    assert adapter["io_engine"] == ("posix" if backend == "raw_posix" else "io_uring")
    assert adapter["num_load_workers"] == 32


def test_xfs_paths_are_owned_by_run_and_host(tmp_path: Path) -> None:
    module = controller()
    cfg = config(tmp_path)
    path = module.adapter(cfg, "mmgi5")["base_path"]
    assert path == (
        "/mnt/lmcache/mmgi5/goal5/goal5-20261018-221200-kernel-xfs-mixtral64k"
    )
    assert module.adapter(cfg, "mmgi0")["base_path"] != path


@pytest.mark.parametrize("successful", [True, False])
def test_mismatched_evidence_is_preserved_even_after_failed_io(
    tmp_path: Path, successful: bool
) -> None:
    module = controller()
    cfg = config(tmp_path)
    row = evidence(module, cfg, "mmgi0", successful)
    row["result"]["metrics"]["config"]["in_flight"] = 256
    with pytest.raises(ValueError, match="configuration"):
        module.validate_evidence(row, cfg, "mmgi0", 2, 1, "load")
    row["result"]["metrics"]["config"]["in_flight"] = 2
    assert module.validate_evidence(row, cfg, "mmgi0", 2, 1, "load") is successful


def test_configuration_change_cannot_reuse_results_directory(tmp_path: Path) -> None:
    module = controller()
    cfg = config(tmp_path)
    module.Run(cfg)
    path = Path(cfg["results_dir"]) / "status.json"
    before = path.read_bytes()
    cfg["backend"] = "raw_posix"
    with pytest.raises(ValueError, match="configuration"):
        module.Run(cfg)
    assert path.read_bytes() == before


def test_fleet_summary_uses_successful_payload_and_does_not_average_p99(
    tmp_path: Path,
) -> None:
    module = controller()
    cfg = config(tmp_path)
    rows = {host: evidence(module, cfg, host) for host in cfg["hosts"]}
    summary = module.summarize(rows, cfg, 2, 1)
    assert summary["payload_gbps_decimal"] == pytest.approx(0.029360128)
    assert "p99_ms" not in summary
    assert summary["hosts"]["mmgi0"]["p99_submit_ms"] == 2.5
    rows["mmgi5"]["result"]["metrics"]["op_0"]["phase_start_time_seconds"] = 106
    with pytest.raises(ValueError, match="phase"):
        module.summarize(rows, cfg, 2, 1)


@pytest.mark.parametrize("failure", ["timeout", "short_window"])
def test_completed_successes_do_not_accept_an_incomplete_window(
    tmp_path: Path, failure: str
) -> None:
    """Completed-key counts cannot hide outstanding work or a shortened window."""
    module = controller()
    cfg = config(tmp_path)
    row = evidence(module, cfg, "mmgi0")
    op = row["result"]["metrics"]["op_0"]
    if failure == "timeout":
        op["timed_out"] = True
    else:
        op["window_sec"] = 1
    assert not module.validate_evidence(row, cfg, "mmgi0", 2, 1, "load")


@pytest.mark.parametrize("sampled", [False, True])
def test_summary_exposes_quantile_observation_scope(
    tmp_path: Path, sampled: bool
) -> None:
    """A rolling p99 must carry its retained and total observation counts."""
    module = controller()
    cfg = config(tmp_path)
    rows = {host: evidence(module, cfg, host) for host in cfg["hosts"]}
    for row in rows.values():
        op = row["result"]["metrics"]["op_0"]
        if sampled:
            op["submit_latency_count"] = 10000
            op["submit_latency_sample_count"] = 4096
    summary = module.summarize(rows, cfg, 2, 1)
    host = summary["hosts"]["mmgi0"]
    assert host["submit_latency_count"] == (10000 if sampled else 10)
    assert host["submit_latency_sample_count"] == (4096 if sampled else 10)
    assert host["latency_quantile_scope"] == (
        "latest completions" if sampled else "all completions"
    )


@pytest.mark.parametrize("during_preflight", [False, True])
def test_preparation_stop_blocks_subsequent_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, during_preflight: bool
) -> None:
    """Cancellation before preparation or during its probe prevents host writes."""
    module = controller()
    cfg = config(tmp_path)
    run = module.Run(cfg)

    def readiness(_cfg: dict) -> dict:
        (run.directory / "STOP").touch()
        return {}

    def forbidden(*args: object) -> None:
        raise AssertionError("Cancelled preparation must not launch a host command")

    monkeypatch.setattr(module, "preflight", readiness)
    monkeypatch.setattr(module, "remote", forbidden)
    if not during_preflight:
        (run.directory / "STOP").touch()
    args = argparse.Namespace(action="prepare", ack_raw_overwrite=False)
    with pytest.raises(ValueError, match="stopped"):
        module.dispatch(run, args)
    assert json.loads(run.status_path.read_text())["state"] == "stopped-incomplete"


def probe() -> ModuleType:
    """Load the host helper without querying hardware."""
    sys.path.insert(0, str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location(
        "goal5_probe", SCRIPT.with_name("goal5_probe.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cleaned_host_refuses_a_delayed_benchmark_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A delayed SSH request cannot start after host cleanup has returned."""
    module = probe()
    monkeypatch.setattr(module, "DEPLOY", tmp_path)
    cfg = config(tmp_path)
    assert module.cleanup(cfg) == []
    output = tmp_path / "unexpected-write"
    request = {
        "cfg": cfg,
        "host": "mmgi0",
        "artifact": "delayed",
        "command": [
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(output)!r}).touch()",
        ],
    }
    with pytest.raises(ValueError, match="stopped"):
        module.bench(request)
    assert not output.exists()


@pytest.mark.parametrize("backend", ["xfs", "raw_posix", "raw_io_uring"])
def test_provenance_freezes_helpers_and_resolved_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    """Binary, payload-provider and deployed-helper drift changes provenance."""
    module = probe()
    repo = tmp_path / "repo"
    deployed = tmp_path / "deploy"
    deployed.mkdir()
    monkeypatch.setattr(module, "DEPLOY", deployed)
    for name in (
        "goal5_overnight.py",
        "goal5_probe.py",
        "setup_initiator_namespace_6x8.py",
        "setup_mmgt_namespace_6x8.py",
    ):
        (deployed / name).write_text("# deployed\n")
    files = (
        [
            "lmcache/cli/commands/bench/l2_adapter_bench/" + name + ".py"
            for name in ("command", "runner", "metrics", "result", "geometry", "data")
        ]
        + [
            "lmcache/v1/distributed/l2_adapters/" + name + ".py"
            for name in (
                "fs_native_l2_adapter",
                "fs_l2_adapter",
                "native_connector_l2_adapter",
            )
        ]
        + ["lmcache/__init__.py", "lmcache/lmcache_fs.py", "profile.yaml"]
    )
    extension = "lmcache.lmcache_fs"
    if backend != "xfs":
        files += [
            "lmcache/v1/distributed/l2_adapters/raw_block_l2_adapter.py",
            "lmcache/v1/storage_backend/raw_block/core.py",
            "lmcache/v1/storage_backend/raw_block/key_codec.py",
            "lmcache_rust_raw_block_io.py",
        ]
        extension = "lmcache_rust_raw_block_io"
    for name in files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# initial\n")

    def run_command(argv: list[str], cwd: str | None = None) -> str:
        if argv == ["git", "rev-parse", "HEAD"]:
            return "fixture-revision"
        return subprocess.check_output(argv, text=True, cwd=cwd).strip()

    monkeypatch.setattr(module, "command", run_command)
    before = module.provenance(str(repo), sys.executable, "profile.yaml", backend)
    binary = repo / (extension.replace(".", "/") + ".py")
    binary.write_text("# changed extension\n")
    after = module.provenance(str(repo), sys.executable, "profile.yaml", backend)
    assert before["runtime"][extension] != after["runtime"][extension]
    assert after["runtime"][extension]["path"] == str(binary)
    assert (
        after["runtime"][extension]["sha256"]
        == hashlib.sha256(binary.read_bytes()).hexdigest()
    )
    data = repo / "lmcache/cli/commands/bench/l2_adapter_bench/data.py"
    data.write_text("# changed provider\n")
    changed = module.provenance(str(repo), sys.executable, "profile.yaml", backend)
    assert after["files"] != changed["files"]
    (deployed / "goal5_probe.py").write_text("# changed helper\n")
    assert (
        changed["helpers"]
        != module.provenance(str(repo), sys.executable, "profile.yaml", backend)[
            "helpers"
        ]
    )


@pytest.mark.parametrize(
    "change",
    [
        "controllers",
        "membership",
        "cpu",
        "code",
        "mounted_raw",
        "restore_raw",
        "uring",
        "disk",
    ],
)
def test_host_readiness_rejects_incompatible_runtime(
    tmp_path: Path, change: str
) -> None:
    module = controller()
    cfg = config(tmp_path)
    expected = [f"id-{i}" for i in range(8)]
    cfg["hosts"]["mmgi0"]["namespace_uuids"] = expected
    row = {
        "namespace_uuids": expected,
        "controllers": [
            {"state": "live", "queue_count": 129, "sqsize": 127},
            {"state": "live", "queue_count": 129, "sqsize": 127},
        ],
        "md_members": expected,
        "raid_disks": 8,
        "chunk_bytes": 524288,
        "md_state": "clean",
        "md_level": "raid0",
        "governors": ["performance"],
        "numa": 1,
        "min_perf_pct": 100,
        "epb": ["0"],
        "ntp_synchronized": True,
        "provenance": deepcopy(cfg["hosts"]["mmgi0"]["provenance"]),
        "mount": {"source": "/dev/md0", "uuid": "fs", "noatime": True},
        "filesystem_uuid": "fs",
        "restore_enabled": True,
        "restore_active": True,
        "io_uring_disabled": 0,
        "rust_available": True,
        "active_io": [],
        "metrics_busy": False,
        "results_free_bytes": 10 * 1024**3,
    }
    if change.startswith(("mounted", "restore", "uring")):
        cfg["backend"] = "raw_io_uring"
        row["mount"] = None
        row["restore_enabled"] = False
        row["restore_active"] = False
    if change == "controllers":
        row["controllers"][0]["queue_count"] = 65
    elif change == "membership":
        row["md_members"] = expected[:-1] + ["foreign"]
    elif change == "cpu":
        row["governors"] = ["schedutil"]
    elif change == "code":
        row["provenance"]["files"]["runner.py"] = "different"
    elif change == "mounted_raw":
        row["mount"] = {"source": "/dev/md0"}
    elif change == "restore_raw":
        row["restore_active"] = True
    elif change == "uring":
        row["io_uring_disabled"] = 2
    elif change == "disk":
        row["results_free_bytes"] = 512 * 1024**2
    with pytest.raises(ValueError):
        module.validate_host(row, cfg, "mmgi0")
