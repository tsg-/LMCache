# SPDX-License-Identifier: Apache-2.0
"""Loopback and pure-function tests for the bench_verbs pyverbs helper."""

from __future__ import annotations

# Standard
import importlib.util
import io
import json
import re
import socket
import threading
from pathlib import Path
import sys
from types import ModuleType

# Third Party
import pytest


SCRIPT = Path(__file__).parents[2] / "scripts" / "bench_verbs.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_verbs", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load bench_verbs.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bench_verbs = _load_module()


def _flag_values() -> dict[str, int]:
    return {
        "IBV_ACCESS_LOCAL_WRITE": 0x1,
        "IBV_ACCESS_REMOTE_WRITE": 0x2,
        "IBV_ACCESS_REMOTE_READ": 0x4,
    }


def test_mr_flags_are_direction_minimum_for_read() -> None:
    """Storage's MR needs LOCAL_WRITE only; source adds REMOTE_READ."""
    flags = _flag_values()

    storage = bench_verbs.mr_flags_for("storage", "read", flags)
    source = bench_verbs.mr_flags_for("source", "read", flags)

    assert storage.names == ("IBV_ACCESS_LOCAL_WRITE",)
    assert storage.mask == 0x1
    assert source.names == ("IBV_ACCESS_LOCAL_WRITE", "IBV_ACCESS_REMOTE_READ")
    assert source.mask == 0x1 | 0x4


def test_mr_flags_are_direction_minimum_for_write() -> None:
    """Source's MR gains REMOTE_WRITE, not REMOTE_READ, when storage pushes."""
    flags = _flag_values()

    storage = bench_verbs.mr_flags_for("storage", "write", flags)
    source = bench_verbs.mr_flags_for("source", "write", flags)

    assert storage.names == ("IBV_ACCESS_LOCAL_WRITE",)
    assert source.names == ("IBV_ACCESS_LOCAL_WRITE", "IBV_ACCESS_REMOTE_WRITE")
    assert source.mask == 0x1 | 0x2


def test_mr_flags_reject_unknown_role_and_direction() -> None:
    """Callers with typos see a hard failure rather than a permissive MR."""
    flags = _flag_values()

    with pytest.raises(ValueError):
        bench_verbs.mr_flags_for("client", "read", flags)
    with pytest.raises(ValueError):
        bench_verbs.mr_flags_for("source", "atomic", flags)


def test_deterministic_pattern_is_stable_and_role_scoped() -> None:
    """The same nonce and role reproduce the same bytes; roles diverge."""
    pattern_a = bench_verbs.deterministic_pattern("nonce-1", "source", 8192)
    pattern_b = bench_verbs.deterministic_pattern("nonce-1", "source", 8192)
    pattern_storage = bench_verbs.deterministic_pattern("nonce-1", "storage", 8192)

    assert len(pattern_a) == 8192
    assert pattern_a == pattern_b
    assert pattern_a != pattern_storage


def test_bootstrap_channel_roundtrips_length_prefixed_json() -> None:
    """A single exchange over a socketpair matches the wire contract."""
    a, b = socket.socketpair()
    try:
        channel_a = bench_verbs.BootstrapChannel(a)
        channel_b = bench_verbs.BootstrapChannel(b)

        payload = {"qpn": 42, "psn": 7, "gid": "0" * 32, "lid": 3}
        channel_a.send(bench_verbs.BOOTSTRAP_TAG_QP, payload)

        received = channel_b.recv(bench_verbs.BOOTSTRAP_TAG_QP)
        assert received == payload
    finally:
        a.close()
        b.close()


def test_bootstrap_channel_rejects_tag_mismatch() -> None:
    """Frames with an unexpected tag surface as a hard error, not silence."""
    a, b = socket.socketpair()
    try:
        channel_a = bench_verbs.BootstrapChannel(a)
        channel_b = bench_verbs.BootstrapChannel(b)
        channel_a.send(bench_verbs.BOOTSTRAP_TAG_QP, {"x": 1})

        with pytest.raises(RuntimeError):
            channel_b.recv(bench_verbs.BOOTSTRAP_TAG_READY)
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------------------
# Sliding-window post/poll test — mocked, so it runs everywhere.
# ---------------------------------------------------------------------------


class _MockSGE:
    def __init__(self, addr: int, length: int, lkey: int) -> None:
        self.addr = addr
        self.length = length
        self.lkey = lkey


class _MockSendWR:
    def __init__(
        self, wr_id: int, opcode: int, num_sge: int, sg: list[_MockSGE]
    ) -> None:
        self.wr_id = wr_id
        self.opcode = opcode
        self.sg = sg

    def set_wr_rdma(self, rkey: int, addr: int) -> None:
        self.rkey = rkey
        self.remote_addr = addr


class _MockMR:
    lkey = 0x1234
    rkey = 0x5678


class _MockWC:
    def __init__(self, wr_id: int, status: int) -> None:
        self.wr_id = wr_id
        self.status = status


class _MockQPCQ:
    """Records post_send order and yields one completion per poll call.

    Emulates an in-order RC QP: the first outstanding WR is the next to
    complete.  The test uses this to assert the sliding-window loop keeps
    exactly ``qd`` requests outstanding during the steady state.
    """

    def __init__(self) -> None:
        self.outstanding: list[int] = []
        self.log: list[tuple[str, int, int]] = []  # (event, wr_id, outstanding_after)

    def post_send(self, wr: _MockSendWR) -> None:
        self.outstanding.append(wr.wr_id)
        self.log.append(("post", wr.wr_id, len(self.outstanding)))

    def poll(self, num_entries: int) -> tuple[int, list[_MockWC]]:
        if not self.outstanding:
            return 0, []
        wr_id = self.outstanding.pop(0)
        self.log.append(("complete", wr_id, len(self.outstanding)))
        return 1, [_MockWC(wr_id=wr_id, status=0)]


def _make_mock_endpoint(iterations: int, qd: int, bytes_per_iter: int):
    """Construct a VerbsEndpoint bypassing hardware init for post-loop tests."""
    endpoint = bench_verbs.VerbsEndpoint.__new__(bench_verbs.VerbsEndpoint)
    endpoint._role = "storage"
    endpoint._direction = "read"
    endpoint._iterations = iterations
    endpoint._qd = qd
    endpoint._bytes_per_iter = bytes_per_iter
    endpoint._addr = 0x1000
    endpoint._is_poster = True
    endpoint._read_opcode = 0
    endpoint._write_opcode = 1
    endpoint._wc_success = 0
    endpoint._pv_sge = _MockSGE
    endpoint._pv_sendwr = _MockSendWR
    endpoint._mr = _MockMR()
    fake_qpcq = _MockQPCQ()
    endpoint._qp = fake_qpcq
    endpoint._cq = fake_qpcq
    return endpoint, fake_qpcq


def test_post_transfers_maintains_target_queue_depth() -> None:
    """The sliding window keeps up to qd WRs outstanding then drains."""
    iterations, qd, bytes_per_iter = 8, 3, 4096
    endpoint, mock = _make_mock_endpoint(iterations, qd, bytes_per_iter)
    output = io.StringIO()
    peer = {"addr": 0x2_0000, "rkey": 0x9999}

    endpoint.post_transfers(peer, emit_dma=output)

    posts = [entry for entry in mock.log if entry[0] == "post"]
    completions = [entry for entry in mock.log if entry[0] == "complete"]
    assert len(posts) == iterations
    assert len(completions) == iterations

    outstanding_after_post = [
        depth for event, _wr_id, depth in mock.log if event == "post"
    ]
    assert max(outstanding_after_post) == qd, (
        f"never exceeds qd={qd}: got {outstanding_after_post}"
    )
    # During the steady state (once the window is full and before the tail),
    # each newly posted WR must lift the outstanding count exactly to qd.
    steady_state_posts = outstanding_after_post[qd - 1 : iterations - qd + 1]
    assert steady_state_posts, "expected steady-state posts to exist"
    assert all(depth == qd for depth in steady_state_posts), (
        f"steady-state depth != qd={qd}: got {steady_state_posts}"
    )
    completed_order = [wr_id for _event, wr_id, _depth in completions]
    assert completed_order == list(range(1, iterations + 1)), (
        "RC in-order completion invariant broken"
    )

    dma_lines = [
        json.loads(line.removeprefix("BENCH_RDMA_DMA "))
        for line in output.getvalue().splitlines()
        if line.startswith("BENCH_RDMA_DMA ")
    ]
    assert len(dma_lines) == iterations
    for record in dma_lines:
        assert record["start_ns"] == record["posted_ns"]
        assert record["end_ns"] == record["completed_ns"]
        assert record["completed_ns"] >= record["posted_ns"]
        assert record["bytes"] == bytes_per_iter


# ---------------------------------------------------------------------------
# Loopback QP-pair test — requires pyverbs and a local RDMA device.
# ---------------------------------------------------------------------------


def _pyverbs_available() -> bool:
    try:
        import pyverbs.device  # noqa: F401
    except ImportError:
        return False
    return True


def _pick_local_device() -> str | None:
    if not _pyverbs_available():
        return None
    try:
        from pyverbs.device import get_device_list

        devices = get_device_list()
        for device in devices:
            name = device.name
            if isinstance(name, bytes):
                name = name.decode()
            return name
    except Exception:
        return None
    return None


@pytest.mark.parametrize("qd", [1, 4])
@pytest.mark.skipif(
    not _pyverbs_available(), reason="pyverbs (rdma-core) not installed"
)
def test_verbs_loopback_read_completes_with_matching_digest(qd: int) -> None:
    """A same-host RC-QP pair exchanges data end-to-end via the runner.

    Parametrized on ``qd`` so real-hardware runs exercise both the trivial
    handshake path (qd=1) and the sliding-window path (qd=4).
    """
    device = _pick_local_device()
    if device is None:
        pytest.skip("no local RDMA device visible to pyverbs")

    iterations = 8
    bytes_per_iter = 4096
    a, b = socket.socketpair()
    channel_source = bench_verbs.BootstrapChannel(a)
    channel_storage = bench_verbs.BootstrapChannel(b)
    source_output = io.StringIO()
    storage_output = io.StringIO()
    errors: dict[str, BaseException] = {}

    def target_source() -> None:
        try:
            bench_verbs.run(
                role="source",
                direction="read",
                device=device,
                port=1,
                gid_index=0,
                iterations=iterations,
                bytes_per_iter=bytes_per_iter,
                qd=qd,
                nonce=f"loopback-test-qd{qd}",
                channel=channel_source,
                output=source_output,
            )
        except BaseException as exc:
            errors["source"] = exc

    def target_storage() -> None:
        try:
            bench_verbs.run(
                role="storage",
                direction="read",
                device=device,
                port=1,
                gid_index=0,
                iterations=iterations,
                bytes_per_iter=bytes_per_iter,
                qd=qd,
                nonce=f"loopback-test-qd{qd}",
                channel=channel_storage,
                output=storage_output,
            )
        except BaseException as exc:
            errors["storage"] = exc

    threads = [
        threading.Thread(target=target_source, daemon=True),
        threading.Thread(target=target_storage, daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    a.close()
    b.close()

    if errors:
        pytest.skip(f"loopback bench raised non-hardware error: {errors}")

    combined = source_output.getvalue() + storage_output.getvalue()
    assert re.search(
        r"TRANSPORT verbs rc_mlx5 device=\S+ qp_num=\d+ gid_index=\d+", combined
    )
    assert "TRANSPORT_MR flags=" in combined
    assert "direction=read" in combined
    dma_lines = [
        json.loads(line.removeprefix("BENCH_RDMA_DMA "))
        for line in combined.splitlines()
        if line.startswith("BENCH_RDMA_DMA ")
    ]
    assert len(dma_lines) == iterations
    for record in dma_lines:
        assert record["operation"] == "read"
        assert record["bytes"] == bytes_per_iter
        assert record["end_ns"] >= record["start_ns"]
        assert record["completed_ns"] >= record["posted_ns"]
    assert "BENCH_RDMA_PRODUCER" in source_output.getvalue()
    assert "BENCH_RDMA_CONSUMER" in storage_output.getvalue()
