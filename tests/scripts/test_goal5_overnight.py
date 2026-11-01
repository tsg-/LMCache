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
import socket
import subprocess
import sys
import time

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


def test_plan_excludes_unselected_initiator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A five-host matrix must neither plan nor require the excluded initiator."""
    module = controller()
    cfg = config(tmp_path)
    del cfg["hosts"]["mmgi4"]
    cfg["include_mixed"] = True
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setattr(sys, "argv", ["goal5", "plan", "--config", str(path)])
    module.main()
    plan = json.loads(capsys.readouterr().out)
    for field in ("adapter", "commands", "mixed_commands"):
        assert set(plan[field]) == set(cfg["hosts"])


def test_successful_subset_phase_resumes_without_relaunch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Complete evidence for all five selected hosts is sufficient for resumption."""
    module = controller()
    cfg = config(tmp_path)
    del cfg["hosts"]["mmgi4"]
    run = module.Run(cfg)
    attempt = run.directory / "load-if2-r1/attempt-0001"
    attempt.mkdir(parents=True)
    for host in cfg["hosts"]:
        (attempt / f"{host}.json").write_text(json.dumps(evidence(module, cfg, host)))

    def forbidden(*args: object) -> None:
        raise AssertionError("Complete selected-host evidence must not relaunch")

    monkeypatch.setattr(module, "preflight", forbidden)
    assert set(run.phase(2, 1, "load")) == set(cfg["hosts"])


def test_raw_mixed_binds_layout_and_fresh_store_prefix(tmp_path: Path) -> None:
    """Raw mixed traffic must retain the baseline geometry and owned prefix."""
    module = controller()
    cfg = config(tmp_path)
    cfg.update(backend="raw_io_uring", raw_meta_total_bytes=4 * 1024**3)
    cfg["raw_capacity_bytes"] = {"mmgi1": 64 * 1024**3}
    argv = module.benchmark_args(cfg, "mmgi1", 2, "mixed", 1)
    assert argv[argv.index("--read-write-ratio") + 1] == "5:1"
    assert argv[argv.index("--write-key-prefix") + 1] == module.write_prefix(
        cfg, "mmgi1", 2, 1
    )
    selected = json.loads(argv[argv.index("--l2-adapter") + 1])
    assert selected["meta_total_bytes"] == 4 * 1024**3
    assert selected["capacity_bytes"] == 64 * 1024**3
    assert selected["slot_bytes"] == 65536 + 4096
    assert selected["io_engine"] == "io_uring"
    assert selected["use_odirect"] and not selected["use_uring_cmd"]


def test_raw_recorder_uses_public_cli_and_fails_closed_without_index_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The raw launcher must require its durable prefix guard before spawning."""
    module = controller()
    cfg = config(tmp_path)
    cfg.update(backend="raw_io_uring", raw_recorder=True)
    argv = module.benchmark_args(cfg, "mmgi1", 2, "mixed", 1)
    assert argv[argv.index("-m") + 1] == "goal5_raw_recording"
    assert json.loads(argv[argv.index("--l2-adapter") + 1])["type"] == (
        "goal5_raw_recording"
    )
    # Third Party
    import goal5_probe

    monkeypatch.setattr(goal5_probe, "DEPLOY", tmp_path)
    cfg["host_results_root"] = str(tmp_path / "bound-host-results")
    request = {
        "cfg": cfg,
        "host": "mmgi1",
        "artifact": "attempt-1",
        "cell": {"mode": "mixed", "in_flight": 2, "repetition": 1},
        "command": argv,
    }
    with pytest.raises(ValueError, match="raw.*guard"):
        goal5_probe.bench(request)
    assert (Path(cfg["host_results_root"]) / cfg["run_id"] / "launch.lock").exists()


def test_async_filesystem_command_keeps_owned_root_and_evidence_binding(
    tmp_path: Path,
) -> None:
    """Native plugin selection must preserve the same corpus and mixed ownership."""
    module = controller()
    cfg = config(tmp_path)
    cfg["filesystem_plugin"] = {
        "module_path": "goal5_async_fleet_stats",
        "class_name": "FleetRecordingClient",
        "io_depth": 32,
    }
    original_base = module.adapter(config(tmp_path), "mmgi2")["base_path"]
    selected = module.adapter(cfg, "mmgi2")
    assert selected["type"] == "native_plugin"
    assert selected["adapter_params"]["base_path"] == original_base
    assert selected["adapter_params"]["io_depth"] == 32
    assert module.filesystem_base(cfg, "mmgi2") == original_base
    row = evidence(module, cfg, "mmgi2")
    assert not module.validate_evidence(row, cfg, "mmgi2", 2, 1, "load")
    row["native"] = {
        "samples": 600,
        "terminal": {
            "pending_reads": 0,
            "submitted_reads": 560,
            "completed_reads": 560,
            "max_worker_pending": 32,
            "max_total_pending": 56,
        },
    }
    row["sampled"] = {"samples": 600}
    assert module.validate_evidence(row, cfg, "mmgi2", 2, 1, "load")
    row["result"]["metrics"]["config"]["l2_adapter_json"] = json.dumps(
        module.adapter(config(tmp_path), "mmgi2")
    )
    with pytest.raises(ValueError, match="configuration"):
        module.validate_evidence(row, cfg, "mmgi2", 2, 1, "load")
    argv = module.benchmark_args(cfg, "mmgi2", 2, "mixed", 1)
    assert argv[argv.index("--write-key-prefix") + 1] == module.write_prefix(
        cfg, "mmgi2", 2, 1
    )


def test_continuation_selects_cells_without_renumbering_repetitions(
    tmp_path: Path,
) -> None:
    """A fresh continuation must retain the original measurement labels."""
    module = controller()
    cfg = config(tmp_path)
    cfg["measurement_cells"] = [[2, 3], [4, 3], [8, 3]]
    assert module.cells(cfg) == [(2, 3), (4, 3), (8, 3)]


@pytest.mark.parametrize(
    "selection", [[], [[2, 3], [2, 3]], [[2, 4]], [[3, 3]], [[2, True]]]
)
def test_continuation_rejects_cells_outside_the_bound_matrix(
    tmp_path: Path,
    selection: list,
) -> None:
    module = controller()
    cfg = config(tmp_path)
    cfg["measurement_cells"] = selection
    with pytest.raises(ValueError, match="measurement_cells"):
        module.cells(cfg)


def test_mixed_only_continuation_preserves_preconditioning_and_reclamation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Completing a mixed cell must not repeat an already accepted read curve."""
    module = controller()
    cfg = config(tmp_path)
    cfg.update(include_read=False, include_mixed=True, measurement_cells=[[2, 3]])
    run = module.Run(cfg)
    for mode in ("canary", "store", "verify"):
        attempt = run.directory / f"{mode}-if1-r0/attempt-0001"
        attempt.mkdir(parents=True)
        for host in cfg["hosts"]:
            row = evidence(module, cfg, host)
            row["cell"] = {"in_flight": 1, "repetition": 0, "mode": mode}
            row["command"] = module.benchmark_args(cfg, host, 1, mode)
            metrics = row["result"]["metrics"]
            metrics["config"].update(mode="rounds", in_flight=1)
            count = (1 if mode == "canary" else 256) * 56
            metrics["op_0"].update(
                operation="Store" if mode == "store" else "Load",
                total_keys=count,
                total_success=count,
            )
            module.write_json(attempt / f"{host}.json", row)
    phases = []

    def phase(in_flight: int, repetition: int, mode: str, fresh: bool = False) -> dict:
        phases.append((in_flight, repetition, mode, fresh))
        rows = {host: evidence(module, cfg, host) for host in cfg["hosts"]}
        for host, row in rows.items():
            row["cell"].update(repetition=repetition, mode=mode)
            row["command"] = module.benchmark_args(
                cfg, host, in_flight, mode, repetition
            )
            metrics = row["result"]["metrics"]
            metrics["config"].update(
                warmup_sec=0,
                read_write_ratio_requested="5:1",
                write_key_prefix=module.write_prefix(cfg, host, in_flight, repetition),
            )
            store = deepcopy(metrics["op_0"])
            store.update(
                operation="Store",
                total_keys=112,
                total_success=112,
                total_success_bytes=7340032,
            )
            metrics["op_1"] = store
        return rows

    monkeypatch.setattr(run, "phase", phase)
    monkeypatch.setattr(module, "remote", lambda host, request: {"host": host})
    run.execute(None)
    assert phases == [(2, 3, "precondition", True), (2, 3, "mixed", False)]
    assert len(list(run.directory.glob("reclaim-if2-r3-*.json"))) == 6
    assert len(json.loads((run.directory / "mixed-summary.json").read_text())) == 1
    assert json.loads(run.status_path.read_text())["state"] == "complete"


def test_mixed_has_fresh_cell_prefix_and_explicit_read_precondition(
    tmp_path: Path,
) -> None:
    """Mixed writes cannot reuse a prior cell or issue unaccounted warmup stores."""
    module = controller()
    cfg = config(tmp_path)
    first = module.benchmark_args(cfg, "mmgi5", 2, "mixed", 1)
    second = module.benchmark_args(cfg, "mmgi5", 2, "mixed", 2)
    assert first[first.index("--read-write-ratio") + 1] == "5:1"
    assert (
        first[first.index("--write-key-prefix") + 1]
        != second[second.index("--write-key-prefix") + 1]
    )
    assert "--only" not in first
    assert "--warmup-sec" not in first
    assert "--no-skip-verify" in first
    warm = module.benchmark_args(cfg, "mmgi5", 2, "precondition")
    assert warm[warm.index("--duration-sec") + 1] == "90"
    assert warm[warm.index("--warmup-sec") + 1] == "0"


def test_mixed_rejects_failed_stores_and_reports_each_direction(tmp_path: Path) -> None:
    """Read success cannot hide Store errors or a changed achieved payload ratio."""
    module = controller()
    cfg = config(tmp_path)
    rows = {h: evidence(module, cfg, h) for h in cfg["hosts"]}
    for host, row in rows.items():
        row["cell"]["mode"] = "mixed"
        row["command"] = module.benchmark_args(cfg, host, 2, "mixed", 1)
        metrics = row["result"]["metrics"]
        metrics["config"].update(
            warmup_sec=0,
            read_write_ratio_requested="5:1",
            write_key_prefix=module.write_prefix(cfg, host, 2, 1),
        )
        store = deepcopy(metrics["op_0"])
        store.update(
            operation="Store",
            total_keys=112,
            total_success=112,
            total_success_bytes=7340032,
        )
        metrics["op_1"] = store
    summary = module.summarize(rows, cfg, 2, 1, "mixed")
    assert summary["read_write_ratio_achieved"] == 5
    assert set(summary["hosts"]["mmgi5"]["operations"]) == {"Load", "Store"}
    rows["mmgi5"]["result"]["metrics"]["op_1"]["total_success"] -= 1
    with pytest.raises(ValueError, match="Failed"):
        module.summarize(rows, cfg, 2, 1, "mixed")


@pytest.mark.parametrize(
    "mode", ["load", "store", "verify", "canary", "mixed", "precondition"]
)
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
    argv = module.benchmark_args(config(tmp_path), "mmgi0", 1, mode, 1)
    args = parser.parse_args(argv[argv.index("l2") + 1 :])
    assert args.only == (
        None
        if mode in ("canary", "mixed")
        else ("store" if mode == "store" else "load")
    )
    assert args.skip_verify is (mode not in ("canary", "mixed"))


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


def test_metrics_port_probe_rejects_live_listener() -> None:
    """Socket reuse must still reject an active metrics server."""
    module = probe()
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        assert module.metrics_port_busy(listener.getsockname()[1])


@pytest.mark.skipif(sys.platform != "linux", reason="Linux TIME_WAIT bind semantics")
def test_metrics_port_probe_accepts_retired_server_socket() -> None:
    """An idle TIME_WAIT port must remain available to the reusable metrics server."""
    module = probe()
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen()
        with socket.create_connection(("127.0.0.1", port)) as client:
            accepted, _ = listener.accept()
            with accepted:
                accepted.shutdown(socket.SHUT_WR)
                assert client.recv(1) == b""
    with socket.socket() as old_probe:
        with pytest.raises(OSError):
            old_probe.bind(("127.0.0.1", port))
    assert not module.metrics_port_busy(port)


def test_reclaim_checks_whole_store_manifest_before_removing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A count mismatch preserves owned files; successful reclamation is resumable."""
    module = probe()
    cfg = config(tmp_path)
    base = tmp_path / "corpus"
    base.mkdir()
    module_controller = controller()
    prefix = module_controller.write_prefix(cfg, "mmgi0", 2, 1)
    own = base / f"{prefix}-bench-model@key.data"
    own.write_bytes(b"payload")
    other = base / "read-corpus.data"
    other.write_bytes(b"keep")
    monkeypatch.setattr(module, "DEPLOY", tmp_path / "deploy")
    monkeypatch.setattr(
        module_controller, "adapter", lambda *_: {"base_path": str(base)}
    )
    monkeypatch.setattr(module_controller, "validate_evidence", lambda *_: True)
    monkeypatch.setitem(sys.modules, "goal5_overnight", module_controller)
    row: dict = {
        "cell": {"in_flight": 2, "repetition": 1},
        "result": {
            "metrics": {
                "op_1": {
                    "operation": "Store",
                    "total_success": 2,
                    "total_success_bytes": 7,
                }
            }
        },
    }
    request = {"cfg": cfg, "host": "mmgi0", "row": row}
    with pytest.raises(ValueError, match="manifest"):
        module.reclaim(request)
    assert own.exists()
    row["result"]["metrics"]["op_1"]["total_success"] = 1
    receipt = module.reclaim(request)
    assert receipt["bytes"] == 7
    assert not own.exists()
    assert other.read_bytes() == b"keep"
    assert module.reclaim(request) == receipt


def test_reclaim_resumes_after_interrupted_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A verified manifest survives interruption without deleting the read corpus."""
    module = probe()
    control = controller()
    cfg = config(tmp_path)
    base = tmp_path / "corpus"
    base.mkdir()
    prefix = control.write_prefix(cfg, "mmgi0", 2, 1)
    for name in ("a", "b"):
        (base / f"{prefix}-bench-model@{name}.data").write_bytes(b"payload")
    monkeypatch.setattr(module, "DEPLOY", tmp_path / "deploy")
    monkeypatch.setattr(control, "adapter", lambda *_: {"base_path": str(base)})
    monkeypatch.setattr(control, "validate_evidence", lambda *_: True)
    monkeypatch.setitem(sys.modules, "goal5_overnight", control)
    row = {
        "cell": {"in_flight": 2, "repetition": 1},
        "result": {
            "metrics": {
                "op_1": {
                    "operation": "Store",
                    "total_success": 2,
                    "total_success_bytes": 14,
                }
            }
        },
    }
    request = {"cfg": cfg, "host": "mmgi0", "row": row}
    unlink = os.unlink
    calls = []

    def interrupt(path: str) -> None:
        calls.append(path)
        if len(calls) == 2:
            raise OSError("interrupted")
        unlink(path)

    monkeypatch.setattr(module.os, "unlink", interrupt)
    with pytest.raises(OSError, match="interrupted"):
        module.reclaim(request)
    monkeypatch.setattr(module.os, "unlink", unlink)
    assert module.reclaim(request)["bytes"] == 14
    assert not list(base.iterdir())


def test_terminated_wrapper_reaps_owned_child(tmp_path: Path) -> None:
    """Wrapper termination must stop its independently launched I/O process."""
    cfg = config(tmp_path)
    cfg["hosts"]["mmgi0"]["repo"] = str(tmp_path)
    directory = tmp_path / "host-results" / cfg["run_id"]
    marker = tmp_path / "unexpected-write"
    request = {
        "cfg": cfg,
        "host": "mmgi0",
        "cell": {"mode": "fio-read"},
        "artifact": "cancel",
        "config_sha256": controller().config_digest(cfg),
        "timeout": 30,
        "command": [
            sys.executable,
            "-c",
            f"import time; from pathlib import Path; time.sleep(2); "
            f"Path({str(marker)!r}).touch()",
        ],
    }
    script = (
        f"import sys; sys.path.insert(0, {str(SCRIPT.parent)!r}); "
        f"import goal5_probe; from pathlib import Path; "
        f"goal5_probe.DEPLOY=Path({str(tmp_path)!r}); "
        f"goal5_probe.bench({request!r})"
    )
    process = subprocess.Popen([sys.executable, "-c", script])
    try:
        deadline = time.monotonic() + 5
        record = directory / "cancel.pid.json"
        while not record.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert record.exists()
        pid = json.loads(record.read_text())["pid"]
        process.terminate()
        process.wait(timeout=5)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        assert not marker.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_fresh_precondition_relaunches_prior_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed mixed cell must warm reads again immediately before its writes."""
    module = controller()
    cfg = config(tmp_path)
    run = module.Run(cfg)
    attempt = run.directory / "precondition-if2-r1/attempt-0001"
    attempt.mkdir(parents=True)
    rows = {}
    for host in cfg["hosts"]:
        row = evidence(module, cfg, host)
        row["cell"]["mode"] = "precondition"
        row["command"] = module.benchmark_args(cfg, host, 2, "precondition", 1)
        row["result"]["metrics"]["config"].update(warmup_sec=0, duration_sec=90)
        row["result"]["metrics"]["op_0"].update(
            phase_end_time_seconds=190,
            window_sec=90,
        )
        rows[host] = row
        (attempt / f"{host}.json").write_text(json.dumps(row))
    calls = []
    monkeypatch.setattr(module, "preflight", lambda *_: {})

    def remote(host: str, request: dict) -> dict:
        calls.append(host)
        return rows[host]

    monkeypatch.setattr(module, "remote", remote)
    run.phase(2, 1, "precondition", fresh=True)
    assert set(calls) == set(cfg["hosts"])
    assert (attempt.parent / "attempt-0002").exists()


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


@pytest.mark.parametrize(
    ("disabled", "group", "ring_ready", "accepted"),
    [
        (1, -1, True, True),
        (2, -1, True, False),
        (1, 0, True, False),
        (1, -1, False, False),
    ],
)
def test_raw_readiness_requires_bound_policy_and_ring(
    tmp_path: Path, disabled: int, group: int, ring_ready: bool, accepted: bool
) -> None:
    """Restricted root io_uring must work without broadening the kernel policy."""
    module = controller()
    cfg = config(tmp_path)
    cfg["backend"] = "raw_io_uring"
    cfg["io_uring_policy"] = {"disabled": 1, "group": -1}
    namespaces = [f"id-{i}" for i in range(8)]
    cfg["hosts"]["mmgi0"]["namespace_uuids"] = namespaces
    row = {
        "namespace_uuids": namespaces,
        "md_members": namespaces,
        "controllers": [
            {"state": "live", "queue_count": 129, "sqsize": 127},
            {"state": "live", "queue_count": 129, "sqsize": 127},
        ],
        "raid_disks": 8,
        "chunk_bytes": 524288,
        "md_level": "raid0",
        "md_state": "clean",
        "governors": ["performance"],
        "numa": 1,
        "min_perf_pct": 100,
        "epb": ["0"],
        "ntp_synchronized": True,
        "provenance": deepcopy(cfg["hosts"]["mmgi0"]["provenance"]),
        "active_io": [],
        "metrics_busy": False,
        "mount": None,
        "restore_enabled": False,
        "restore_active": False,
        "rust_available": True,
        "results_free_bytes": 10 * 1024**3,
        "io_uring_disabled": disabled,
        "io_uring_group": group,
        "io_uring_ready": ring_ready,
    }
    if accepted:
        module.validate_host(row, cfg, "mmgi0")
    else:
        with pytest.raises(ValueError):
            module.validate_host(row, cfg, "mmgi0")
