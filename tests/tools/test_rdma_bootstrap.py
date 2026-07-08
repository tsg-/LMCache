# SPDX-License-Identifier: Apache-2.0
"""Tests for the RDMA rendezvous helper (scripts/rdma_bootstrap.py)."""

# Standard
from pathlib import Path
import importlib.util
import sys

# Third Party
import pytest

_SCRIPT_PATH = Path(__file__).parents[2] / "scripts" / "rdma_bootstrap.py"
_spec = importlib.util.spec_from_file_location("rdma_bootstrap", _SCRIPT_PATH)
rdma_bootstrap = importlib.util.module_from_spec(_spec)
sys.modules["rdma_bootstrap"] = rdma_bootstrap
_spec.loader.exec_module(rdma_bootstrap)


class TestPeerRole:
    def test_target_peer_is_initiator(self):
        assert rdma_bootstrap.peer_role("target") == "initiator"

    def test_initiator_peer_is_target(self):
        assert rdma_bootstrap.peer_role("initiator") == "target"


class TestEndpointPath:
    def test_includes_role_and_nonce(self):
        path = rdma_bootstrap.endpoint_path("target", "abc123", "/tmp")
        assert path == "/tmp/lmcache_rdma_target_abc123.json"


class TestBuildRemoteEnv:
    def test_target_env_points_peer_at_initiator(self):
        node = rdma_bootstrap.NodeSpec(role="target", host="kv1", command="run.py")
        env = rdma_bootstrap.build_remote_env(node, "nonce1", "/tmp")

        assert env["LMCACHE_RDMA_ROLE"] == "target"
        assert env["LMCACHE_RDMA_NONCE"] == "nonce1"
        assert (
            env["LMCACHE_RDMA_ENDPOINT_FILE"] == "/tmp/lmcache_rdma_target_nonce1.json"
        )
        assert (
            env["LMCACHE_RDMA_PEER_ENDPOINT_FILE"]
            == "/tmp/lmcache_rdma_initiator_nonce1.json"
        )


class TestBuildSshArgv:
    def test_exports_env_before_command(self):
        node = rdma_bootstrap.NodeSpec(role="initiator", host="gpu1", command="run.py")
        env = {"LMCACHE_RDMA_ROLE": "initiator", "LMCACHE_RDMA_NONCE": "n1"}

        argv = rdma_bootstrap.build_ssh_argv(node, env)

        assert argv[:2] == ["ssh", "gpu1"]
        assert "LMCACHE_RDMA_ROLE=initiator" in argv[2]
        assert "LMCACHE_RDMA_NONCE=n1" in argv[2]
        assert argv[2].endswith("run.py")


class TestGenerateNonce:
    def test_two_calls_differ(self):
        assert rdma_bootstrap.generate_nonce() != rdma_bootstrap.generate_nonce()


class TestRelayEndpointFile:
    def test_returns_false_on_download_failure(self, monkeypatch):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            returncode = 1 if argv[0] == "scp" and len(calls) == 1 else 0
            return type("Result", (), {"returncode": returncode})()

        monkeypatch.setattr(rdma_bootstrap.subprocess, "run", fake_run)

        result = rdma_bootstrap.relay_endpoint_file(
            "src", "/tmp/a.json", "dst", "/tmp/b.json"
        )

        assert result is False
        # Download attempted; upload skipped after early return.
        assert len(calls) == 1

    def test_returns_true_when_both_legs_succeed(self, monkeypatch):
        monkeypatch.setattr(
            rdma_bootstrap.subprocess,
            "run",
            lambda argv, **kwargs: type("Result", (), {"returncode": 0})(),
        )

        result = rdma_bootstrap.relay_endpoint_file(
            "src", "/tmp/a.json", "dst", "/tmp/b.json"
        )

        assert result is True


class TestWatchForMarker:
    def test_sets_event_when_marker_seen(self):
        import io
        import threading

        class FakeProc:
            stdout = io.StringIO("starting up\nVERBS_TRANSPORT: QP connected\n")

        ready = threading.Event()
        rdma_bootstrap.watch_for_marker(
            FakeProc(), rdma_bootstrap.READY_LOG_MARKER, ready
        )

        assert ready.is_set()

    def test_leaves_event_unset_without_marker(self):
        import io
        import threading

        class FakeProc:
            stdout = io.StringIO("still connecting\n")

        ready = threading.Event()
        rdma_bootstrap.watch_for_marker(
            FakeProc(), rdma_bootstrap.READY_LOG_MARKER, ready
        )

        assert not ready.is_set()


class TestParseArgs:
    def test_requires_all_four_endpoints(self):
        with pytest.raises(SystemExit):
            rdma_bootstrap.parse_args(["--initiator-host", "gpu1"])

    def test_default_nonce_is_none(self):
        args = rdma_bootstrap.parse_args(
            [
                "--initiator-host",
                "gpu1",
                "--initiator-cmd",
                "run.py",
                "--target-host",
                "kv1",
                "--target-cmd",
                "run.py",
            ]
        )
        assert args.nonce is None


class TestRunRendezvousDryRun:
    def test_dry_run_prints_commands_without_launching(self, capsys, monkeypatch):
        def fail_if_called(*args, **kwargs):
            raise AssertionError("subprocess should not be invoked in dry-run mode")

        monkeypatch.setattr(rdma_bootstrap.subprocess, "Popen", fail_if_called)
        monkeypatch.setattr(rdma_bootstrap.subprocess, "run", fail_if_called)

        initiator = rdma_bootstrap.NodeSpec(
            role="initiator", host="gpu1", command="run.py"
        )
        target = rdma_bootstrap.NodeSpec(role="target", host="kv1", command="run.py")

        exit_code = rdma_bootstrap.run_rendezvous(
            initiator, target, "nonce1", "/tmp", timeout_seconds=1.0, dry_run=True
        )

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "nonce1" in out
        assert "gpu1" in out
        assert "kv1" in out
