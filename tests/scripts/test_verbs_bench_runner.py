# SPDX-License-Identifier: Apache-2.0
"""Unit tests for scripts/verbs_bench_runner.py.

These tests exercise the runner's pure helpers and its orchestration order
without requiring real SSH access or RDMA hardware. Live end-to-end runs
are covered separately by the two-host bench harness on bmg0/bmg1.
"""

from __future__ import annotations

# Standard
import argparse
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

# Third Party
import pytest


SCRIPTS_DIR = Path(__file__).parents[2] / "scripts"


def _load_module(name: str) -> ModuleType:
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# verbs_bench_runner imports bench_verify by bare module name; preload it.
_load_module("bench_verify")
runner = _load_module("verbs_bench_runner")


# ---------------------------------------------------------------------------
# Record parsing
# ---------------------------------------------------------------------------


def test_parse_records_only_matches_prefix() -> None:
    lines = [
        'BENCH_RDMA_DMA {"operation": "read", "page_idx": 0}\n',
        'BENCH_RDMA_DMA_EXTRA {"operation": "spoof"}\n',
        '  BENCH_RDMA_DMA {"operation": "leading-space"}\n',
        'BENCH_RDMA_DMA not-json\n',
        'other line\n',
    ]
    records = runner._parse_records(lines, "BENCH_RDMA_DMA")
    assert records == [{"operation": "read", "page_idx": 0}]


def test_dma_ms_samples_computes_elapsed_ms() -> None:
    records = [
        {"start_ns": 0, "end_ns": 2_000_000, "bytes": 4096},
        {"start_ns": 10_000_000, "end_ns": 11_500_000, "bytes": 4096},
        {"start_ns": "bad", "end_ns": 1_000_000},  # skipped
    ]
    assert runner._dma_ms_samples(records) == [2.0, 1.5]


def test_control_ms_from_records_computes_elapsed_ms() -> None:
    records = [
        {"phase": "qp_setup", "start_ns": 0, "end_ns": 5_000_000},
    ]
    assert runner._control_ms_from_records(records) == [5.0]


def test_control_ms_from_records_skips_malformed() -> None:
    records = [
        {"phase": "qp_setup", "start_ns": "bad"},
        {"phase": "qp_setup", "start_ns": 0, "end_ns": 3_000_000},
    ]
    assert runner._control_ms_from_records(records) == [3.0]


def test_verbs_command_binds_requested_numa_node() -> None:
    """The benchmark process must use the NUMA node validated by its manifest."""
    command = runner._verbs_cmd(
        role="source",
        direction="read",
        device="mlx5_1",
        numa_node=1,
        iterations=4,
        bytes_per_iter=4096,
        qd=1,
        nonce="test-nonce",
        bootstrap_flag="--bootstrap-connect 192.168.200.4:9600",
    )

    assert "numactl --cpunodebind=1 --membind=1" in command


# ---------------------------------------------------------------------------
# build_size_result: RX endpoint selection + persisted provenance
# ---------------------------------------------------------------------------


def _fake_counter_snapshot(counters: dict[str, int]):
    return runner.CounterSnapshot(counters=counters)


def _endpoint_evidence(role: str, direction: str) -> list[str]:
    source_flag = (
        "IBV_ACCESS_REMOTE_READ"
        if direction == "read"
        else "IBV_ACCESS_REMOTE_WRITE"
    )
    flags = "IBV_ACCESS_LOCAL_WRITE"
    if role == "source":
        flags = f"{flags}|{source_flag}"
    return [
        "TRANSPORT verbs rc_mlx5 device=mlx5_1 qp_num=42 gid_index=3\n",
        (
            f"TRANSPORT_MR flags={flags} direction={direction} "
            f"role={role} rkey=1 addr=0x1000 length=131072\n"
        ),
    ]


def _base_storage_lines(
    bytes_per_iter: int, iterations: int, direction: str
) -> list[str]:
    lines = [
        *_endpoint_evidence("storage", direction),
        (
            'BENCH_RDMA_CONTROL {"phase": "qp_setup", "role": "storage", '
            '"start_ns": 0, "end_ns": 1000000}\n'
        ),
    ]
    for page in range(iterations):
        start = 2_000_000 + page * 500_000
        end = start + 500_000
        record = {
            "operation": direction,
            "page_idx": page,
            "start_ns": start,
            "end_ns": end,
            "bytes": bytes_per_iter,
        }
        lines.append(f"BENCH_RDMA_DMA {json.dumps(record, sort_keys=True)}\n")
    return lines


def _digest_record(role: str, kind: str, bytes_total: int) -> str:
    payload = {
        "role": role,
        "direction": "read",
        "kind": kind,
        "iterations": 4,
        "bytes_per_iter": bytes_total // 4,
        "bytes_total": bytes_total,
        "digest_algorithm": "blake3-test",
        "digest": "cafef00d",
        "nonce": "test-nonce",
    }
    return f"BENCH_RDMA_{kind.upper()} {json.dumps(payload, sort_keys=True)}\n"


def test_build_size_result_read_uses_storage_rx_counters(tmp_path: Path) -> None:
    """direction=read must feed storage before/after into the verifier."""
    bytes_per_iter = 4096
    iterations = 4
    bytes_total = bytes_per_iter * iterations
    storage_lines = _base_storage_lines(bytes_per_iter, iterations, "read")
    storage_lines.append(_digest_record("storage", "consumer", bytes_total))
    source_lines = [
        *_endpoint_evidence("source", "read"),
        _digest_record("source", "producer", bytes_total),
    ]

    source_before = _fake_counter_snapshot({"rx_bytes_phy": 0})
    source_after = _fake_counter_snapshot({"rx_bytes_phy": 9_999_999})  # ignored
    storage_before = _fake_counter_snapshot({"rx_bytes_phy": 100})
    storage_after = _fake_counter_snapshot({"rx_bytes_phy": 100 + bytes_total})

    size_result = runner.SizeResult(
        bytes_per_iter=bytes_per_iter,
        iterations=iterations,
        qd=1,
        direction="read",
    )
    runner.build_size_result(
        size_result=size_result,
        storage_lines=storage_lines,
        source_lines=source_lines,
        source_before=source_before,
        source_after=source_after,
        storage_before=storage_before,
        storage_after=storage_after,
        verification_dir=str(tmp_path),
        run_label="run-under-test",
        direction="read",
        manifest_ref={"source": "/tmp/src.json", "storage": "/tmp/dst.json"},
    )

    assert size_result.verification_path is not None
    record = json.loads(Path(size_result.verification_path).read_text())
    counters = record["verification_evidence"]["wire_counter_delta"]
    # Invariant: the RX-side counter pair fed to the verifier is the
    # storage-side pair for direction=read.
    assert counters == {"rx_bytes_phy": bytes_total}
    assert record["direction"] == "read"
    assert record["manifest_ref"]["storage"] == "/tmp/dst.json"
    # Invariant: digest match implies eligible for baseline.
    assert record["digest_match"] is True
    assert record["eligible_for_baseline"] is True
    assert size_result.eligible_for_baseline is True


def test_build_size_result_write_uses_source_rx_counters(tmp_path: Path) -> None:
    """direction=write must feed source before/after into the verifier."""
    bytes_per_iter = 4096
    iterations = 4
    bytes_total = bytes_per_iter * iterations
    storage_lines = _base_storage_lines(bytes_per_iter, iterations, "write")
    storage_lines.append(_digest_record("storage", "producer", bytes_total))
    source_lines = [
        *_endpoint_evidence("source", "write"),
        _digest_record("source", "consumer", bytes_total),
    ]

    source_before = _fake_counter_snapshot({"rx_bytes_phy": 500})
    source_after = _fake_counter_snapshot({"rx_bytes_phy": 500 + bytes_total})
    storage_before = _fake_counter_snapshot({"rx_bytes_phy": 0})
    storage_after = _fake_counter_snapshot({"rx_bytes_phy": 9_999_999})  # ignored

    size_result = runner.SizeResult(
        bytes_per_iter=bytes_per_iter,
        iterations=iterations,
        qd=1,
        direction="write",
    )
    runner.build_size_result(
        size_result=size_result,
        storage_lines=storage_lines,
        source_lines=source_lines,
        source_before=source_before,
        source_after=source_after,
        storage_before=storage_before,
        storage_after=storage_after,
        verification_dir=str(tmp_path),
        run_label="run-write",
        direction="write",
        manifest_ref={"source": "/tmp/src.json", "storage": "/tmp/dst.json"},
    )

    assert size_result.verification_path is not None
    record = json.loads(Path(size_result.verification_path).read_text())
    assert record["verification_evidence"]["wire_counter_delta"] == {
        "rx_bytes_phy": bytes_total
    }
    assert record["manifest_ref"]["source"] == "/tmp/src.json"


def test_build_size_result_requires_control_record(tmp_path: Path) -> None:
    """No BENCH_RDMA_CONTROL record → must refuse to publish."""
    bytes_per_iter = 4096
    iterations = 2
    bytes_total = bytes_per_iter * iterations
    # Storage lines without the BENCH_RDMA_CONTROL line.
    storage_lines = [
        *_endpoint_evidence("storage", "read"),
    ]
    for page in range(iterations):
        record = {
            "operation": "read",
            "page_idx": page,
            "start_ns": page * 1_000_000,
            "end_ns": page * 1_000_000 + 500_000,
            "bytes": bytes_per_iter,
        }
        storage_lines.append(
            f"BENCH_RDMA_DMA {json.dumps(record, sort_keys=True)}\n"
        )
    storage_lines.append(_digest_record("storage", "consumer", bytes_total))
    source_lines = [
        *_endpoint_evidence("source", "read"),
        _digest_record("source", "producer", bytes_total),
    ]

    size_result = runner.SizeResult(
        bytes_per_iter=bytes_per_iter,
        iterations=iterations,
        qd=1,
        direction="read",
    )
    with pytest.raises(RuntimeError, match="BENCH_RDMA_CONTROL"):
        runner.build_size_result(
            size_result=size_result,
            storage_lines=storage_lines,
            source_lines=source_lines,
            source_before=_fake_counter_snapshot({"rx_bytes_phy": 0}),
            source_after=_fake_counter_snapshot({"rx_bytes_phy": 0}),
            storage_before=_fake_counter_snapshot({"rx_bytes_phy": 0}),
            storage_after=_fake_counter_snapshot({"rx_bytes_phy": bytes_total}),
            verification_dir=str(tmp_path),
            run_label="run-missing-control",
            direction="read",
            manifest_ref={"source": "/tmp/src.json", "storage": "/tmp/dst.json"},
        )


def test_build_size_result_wire_mismatch_is_warning_not_failure(
    tmp_path: Path,
) -> None:
    """Wire-byte mismatch is diagnostic: eligible stays true, warning recorded."""
    bytes_per_iter = 4096
    iterations = 2
    bytes_total = bytes_per_iter * iterations
    storage_lines = _base_storage_lines(bytes_per_iter, iterations, "read")
    storage_lines.append(_digest_record("storage", "consumer", bytes_total))
    source_lines = [
        *_endpoint_evidence("source", "read"),
        _digest_record("source", "producer", bytes_total),
    ]
    size_result = runner.SizeResult(
        bytes_per_iter=bytes_per_iter,
        iterations=iterations,
        qd=1,
        direction="read",
    )

    # Storage RX delta of 1 byte for a transfer that expected `bytes_total`.
    # Digest still matches (both sides emit the same test digest).
    runner.build_size_result(
        size_result=size_result,
        storage_lines=storage_lines,
        source_lines=source_lines,
        source_before=_fake_counter_snapshot({"rx_bytes_phy": 0}),
        source_after=_fake_counter_snapshot({"rx_bytes_phy": 1}),
        storage_before=_fake_counter_snapshot({"rx_bytes_phy": 0}),
        storage_after=_fake_counter_snapshot({"rx_bytes_phy": 1}),
        verification_dir=str(tmp_path),
        run_label="run-wire-warn",
        direction="read",
        manifest_ref={"source": "/tmp/src.json", "storage": "/tmp/dst.json"},
    )

    assert size_result.eligible_for_baseline is True
    assert "wire_bytes_within_tolerance" in size_result.warnings
    record = json.loads(Path(size_result.verification_path).read_text())
    assert record["eligible_for_baseline"] is True
    assert record["wire_bytes_within_tolerance"] is False
    assert "wire_bytes_within_tolerance" in record["diagnostic_warnings"]


def test_build_size_result_fails_hard_on_digest_mismatch(tmp_path: Path) -> None:
    """Digest mismatch: hard fail with audit JSON persisted (eligible=false)."""
    bytes_per_iter = 4096
    iterations = 2
    bytes_total = bytes_per_iter * iterations
    storage_lines = _base_storage_lines(bytes_per_iter, iterations, "read")
    # Storage consumer digest differs from source producer digest.
    storage_consumer = {
        "role": "storage",
        "direction": "read",
        "kind": "consumer",
        "iterations": iterations,
        "bytes_per_iter": bytes_per_iter,
        "bytes_total": bytes_total,
        "digest_algorithm": "blake3-test",
        "digest": "deadbeef",
        "nonce": "test-nonce",
    }
    storage_lines.append(
        f"BENCH_RDMA_CONSUMER {json.dumps(storage_consumer, sort_keys=True)}\n"
    )
    source_lines = [
        *_endpoint_evidence("source", "read"),
        _digest_record("source", "producer", bytes_total),  # digest=cafef00d
    ]
    size_result = runner.SizeResult(
        bytes_per_iter=bytes_per_iter,
        iterations=iterations,
        qd=1,
        direction="read",
    )

    with pytest.raises(RuntimeError, match="digest mismatch"):
        runner.build_size_result(
            size_result=size_result,
            storage_lines=storage_lines,
            source_lines=source_lines,
            source_before=_fake_counter_snapshot({"rx_bytes_phy": 0}),
            source_after=_fake_counter_snapshot({"rx_bytes_phy": bytes_total}),
            storage_before=_fake_counter_snapshot({"rx_bytes_phy": 0}),
            storage_after=_fake_counter_snapshot({"rx_bytes_phy": bytes_total}),
            verification_dir=str(tmp_path),
            run_label="run-digest-mismatch",
            direction="read",
            manifest_ref={"source": "/tmp/src.json", "storage": "/tmp/dst.json"},
        )

    # Audit JSON persisted before the raise, eligibility explicitly false.
    assert size_result.verification_path is not None
    record = json.loads(Path(size_result.verification_path).read_text())
    assert record["digest_match"] is False
    assert record["eligible_for_baseline"] is False


# ---------------------------------------------------------------------------
# Fail-closed manifest ordering
# ---------------------------------------------------------------------------


def _args_for_run(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        src_host="bmg0",
        dst_host="bmg1",
        dst_bootstrap_ip="192.168.200.4",
        src_numa_node=0,
        dst_numa_node=0,
        direction="read",
        iterations=4,
        bytes_per_iter=[4096],
        qd=1,
        timeout=10.0,
        verification_dir=str(tmp_path),
        verbose=False,
    )


def test_run_bench_aborts_before_launching_verbs_on_manifest_failure(
    tmp_path: Path,
) -> None:
    """A failing manifest must prevent any verbs launch and any ethtool call."""
    args = _args_for_run(tmp_path)

    call_order: list[str] = []

    def fake_create_manifest(host, label, role, nic, numa_node, total_bytes):
        call_order.append(f"manifest:{role}")
        if role == "storage":
            raise RuntimeError("simulated storage manifest gate failure")
        return f"/tmp/manifest_{role}.json"

    def fake_snapshot_nic(host, iface):
        call_order.append(f"ethtool:{host}")
        return runner.CounterSnapshot(counters={})

    def fake_run_one_size(**_kwargs):
        call_order.append("run_one_size")
        return [], []

    def fake_kill_port(host, port):
        call_order.append(f"kill_port:{host}:{port}")

    with mock.patch.object(
        runner, "create_manifest", side_effect=fake_create_manifest
    ), mock.patch.object(
        runner, "snapshot_nic", side_effect=fake_snapshot_nic
    ), mock.patch.object(
        runner, "run_one_size", side_effect=fake_run_one_size
    ), mock.patch.object(runner, "kill_port", side_effect=fake_kill_port):
        with pytest.raises(RuntimeError, match="storage manifest gate failure"):
            runner.run_bench(args)

    # Only the two manifest attempts should have run — no ethtool, no
    # verbs launch, no port kill.
    assert call_order == ["manifest:source", "manifest:storage"]


def test_run_bench_persists_manifest_ref_from_both_hosts(tmp_path: Path) -> None:
    """A successful sweep must persist manifest_ref referencing both hosts."""
    args = _args_for_run(tmp_path)

    manifests = {
        "source": "/tmp/manifest_src.json",
        "storage": "/tmp/manifest_dst.json",
    }

    def fake_create_manifest(host, label, role, nic, numa_node, total_bytes):
        return manifests[role]

    def fake_snapshot_nic(host, iface):
        return runner.CounterSnapshot(counters={"rx_bytes_phy": 0})

    def fake_run_one_size(**kwargs):
        bytes_per_iter = kwargs["bytes_per_iter"]
        iterations = kwargs["iterations"]
        bytes_total = bytes_per_iter * iterations
        storage_lines = _base_storage_lines(bytes_per_iter, iterations, "read")
        storage_lines.append(_digest_record("storage", "consumer", bytes_total))
        source_lines = [
            *_endpoint_evidence("source", "read"),
            _digest_record("source", "producer", bytes_total),
        ]
        return storage_lines, source_lines

    def fake_snapshot_after(host, iface):
        return runner.CounterSnapshot(
            counters={"rx_bytes_phy": args.bytes_per_iter[0] * args.iterations}
        )

    call_counter = SimpleNamespace(n=0)

    def snapshot_side_effect(host, iface):
        call_counter.n += 1
        # Odd calls (1,3) are the before snapshots; even calls (2,4) are
        # after snapshots for the read path (storage side).
        if call_counter.n in (2, 4) and host == "bmg1":
            return fake_snapshot_after(host, iface)
        return fake_snapshot_nic(host, iface)

    with mock.patch.object(
        runner, "create_manifest", side_effect=fake_create_manifest
    ), mock.patch.object(
        runner, "snapshot_nic", side_effect=snapshot_side_effect
    ), mock.patch.object(
        runner, "run_one_size", side_effect=fake_run_one_size
    ), mock.patch.object(runner, "kill_port"):
        run = runner.run_bench(args)

    assert len(run.results) == 1
    verification_path = run.results[0].verification_path
    assert verification_path is not None
    record = json.loads(Path(verification_path).read_text())
    assert record["manifest_ref"] == manifests


def test_run_bench_persists_failure_audit_before_reraising(tmp_path: Path) -> None:
    """A subprocess failure must leave an ineligible audit record behind."""
    args = _args_for_run(tmp_path)
    manifests = {
        "source": "/tmp/manifest_src.json",
        "storage": "/tmp/manifest_dst.json",
    }

    with mock.patch.object(
        runner, "create_manifest", side_effect=lambda *_args: manifests[_args[2]]
    ), mock.patch.object(
        runner,
        "snapshot_nic",
        return_value=runner.CounterSnapshot(counters={"rx_bytes_phy": 0}),
    ), mock.patch.object(
        runner,
        "run_one_size",
        side_effect=runner.BenchRunError(
            "simulated verbs subprocess failure",
            ["storage failure output\n"],
            ["source failure output\n"],
        ),
    ), mock.patch.object(runner, "kill_port"):
        with pytest.raises(RuntimeError, match="simulated verbs subprocess failure"):
            runner.run_bench(args)

    records = list(tmp_path.glob("verification_*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["eligible_for_baseline"] is False
    assert record["manifest_ref"] == manifests
    assert record["direction"] == "read"
    assert record["page_bytes"] == 4096
    assert record["iterations"] == 4
    assert record["error"] == "simulated verbs subprocess failure"
    assert record["logs"] == {
        "storage": "storage failure output\n",
        "source": "source failure output\n",
    }
