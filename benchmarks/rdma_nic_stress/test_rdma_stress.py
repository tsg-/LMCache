# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the RDMA NIC stress harness.

Tests the control-plane protocol, stats helpers, and argument parsing.
No real RDMA hardware is required.
"""

from __future__ import annotations

import ctypes
import io
import json
import socket
import struct
import threading
import time
import unittest.mock as mock

import pytest

# ---------------------------------------------------------------------------
# Module under test
# ---------------------------------------------------------------------------
import importlib, sys

# Ensure the harness directory is importable
import os
sys.path.insert(0, os.path.dirname(__file__))

import rdma_stress as harness
from rdma_stress import (
    MrDescriptor,
    _gbps,
    _p,
    _recv_json,
    _send_json,
    _parse_addr,
    _build_parser,
)


# ===========================================================================
# MrDescriptor serialisation
# ===========================================================================

class TestMrDescriptor:
    def test_roundtrip(self):
        d = MrDescriptor(index=3, addr=0xDEAD_BEEF, rkey=42, length=65536)
        assert MrDescriptor.from_dict(d.to_dict()) == d

    def test_fields_preserved(self):
        d = MrDescriptor(index=0, addr=0x1000, rkey=7, length=4096)
        dd = d.to_dict()
        assert dd["index"] == 0
        assert dd["addr"] == 0x1000
        assert dd["rkey"] == 7
        assert dd["length"] == 4096


# ===========================================================================
# Stats helpers
# ===========================================================================

class TestGbps:
    def test_basic(self):
        assert _gbps(1_000_000_000, 1.0) == pytest.approx(1.0)

    def test_zero_elapsed(self):
        assert _gbps(100, 0.0) == float("inf")

    def test_half_second(self):
        assert _gbps(500_000_000, 0.5) == pytest.approx(1.0)


class TestPercentile:
    def test_p50(self):
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert _p(data, 50) == pytest.approx(3.0)

    def test_p0_is_min(self):
        data = [5.0, 2.0, 1.0, 3.0]
        assert _p(data, 0) == pytest.approx(1.0)

    def test_p99_clamped(self):
        data = [1.0, 2.0, 3.0]
        assert _p(data, 99) == pytest.approx(3.0)

    def test_single_element(self):
        assert _p([42.0], 50) == pytest.approx(42.0)


# ===========================================================================
# Control-plane wire protocol
# ===========================================================================

def _make_pipe():
    """Return two connected socket objects (like a socketpair)."""
    a, b = socket.socketpair()
    return a, b


class TestWireProtocol:
    def test_send_recv_dict(self):
        a, b = _make_pipe()
        try:
            obj = {"status": "done", "count": 5}
            _send_json(a, obj)
            result = _recv_json(b)
            assert result == obj
        finally:
            a.close()
            b.close()

    def test_send_recv_list(self):
        a, b = _make_pipe()
        try:
            lst = [{"index": i, "addr": i * 0x1000, "rkey": i, "length": 65536}
                   for i in range(4)]
            _send_json(a, lst)
            result = _recv_json(b)
            assert result == lst
        finally:
            a.close()
            b.close()

    def test_multiple_messages_in_sequence(self):
        a, b = _make_pipe()
        try:
            for i in range(5):
                _send_json(a, {"seq": i})
            for i in range(5):
                assert _recv_json(b) == {"seq": i}
        finally:
            a.close()
            b.close()

    def test_recv_on_closed_raises(self):
        a, b = _make_pipe()
        a.close()
        with pytest.raises(Exception):
            _recv_json(b)
        b.close()


# ===========================================================================
# _parse_addr
# ===========================================================================

class TestParseAddr:
    def test_host_port(self):
        assert _parse_addr("localhost:7700") == ("localhost", 7700)

    def test_ip_port(self):
        assert _parse_addr("192.168.1.2:5001") == ("192.168.1.2", 5001)

    def test_missing_host_raises(self):
        with pytest.raises((argparse.ArgumentTypeError, ValueError)):
            _parse_addr(":7700")


import argparse  # noqa: E402 — needed for the test above


# ===========================================================================
# Argument parser
# ===========================================================================

class TestArgumentParser:
    def test_initiator_defaults(self):
        args = _build_parser().parse_args(["--role", "initiator"])
        assert args.role == "initiator"
        assert args.buf_size == 512
        assert args.num_bufs == 8
        assert args.listen == "0.0.0.0:7700"

    def test_target_defaults(self):
        args = _build_parser().parse_args(["--role", "target"])
        assert args.role == "target"
        assert args.iters == 50
        assert args.warmup == 5
        assert args.timeout_ms == 5000
        assert not args.verify

    def test_custom_buf_size(self):
        args = _build_parser().parse_args(
            ["--role", "target", "--buf-size", "256", "--num-bufs", "16"]
        )
        assert args.buf_size == 256
        assert args.num_bufs == 16

    def test_verify_flag(self):
        args = _build_parser().parse_args(["--role", "target", "--verify"])
        assert args.verify is True

    def test_missing_role_exits(self):
        with pytest.raises(SystemExit):
            _build_parser().parse_args([])


# ===========================================================================
# _bench_op — unit test with mocked transport
# ===========================================================================

class TestBenchOp:
    def _make_mock_transport(self):
        t = mock.MagicMock()
        future = mock.MagicMock()
        t.post_read.return_value = future
        t.post_write.return_value = future
        t.poll_completion.return_value = True
        return t, future

    def _make_bufs_and_descs(self, n: int = 2):
        bufs = []
        descs = []
        for i in range(n):
            mr = mock.MagicMock()
            mr.handle.lkey = 10 + i
            buf = mock.MagicMock()
            buf.addr = 0x1000 * (i + 1)
            buf.length = 65536
            buf.mr = mr
            bufs.append(buf)
            descs.append(MrDescriptor(index=i, addr=0x8000 * (i + 1), rkey=i + 1, length=65536))
        return bufs, descs

    def test_read_returns_correct_count(self):
        transport, _ = self._make_mock_transport()
        bufs, descs = self._make_bufs_and_descs(2)
        times = harness._bench_op(
            "READ", transport, bufs, descs,
            warmup=1, iters=3, timeout_ms=1000, op="read",
        )
        assert len(times) == 3

    def test_write_returns_correct_count(self):
        transport, _ = self._make_mock_transport()
        bufs, descs = self._make_bufs_and_descs(2)
        times = harness._bench_op(
            "WRITE", transport, bufs, descs,
            warmup=2, iters=5, timeout_ms=1000, op="write",
        )
        assert len(times) == 5

    def test_post_read_called_per_buf_per_iter(self):
        transport, _ = self._make_mock_transport()
        bufs, descs = self._make_bufs_and_descs(3)
        harness._bench_op(
            "READ", transport, bufs, descs,
            warmup=0, iters=2, timeout_ms=1000, op="read",
        )
        assert transport.post_read.call_count == 3 * 2  # num_bufs * iters

    def test_post_write_called_per_buf_per_iter(self):
        transport, _ = self._make_mock_transport()
        bufs, descs = self._make_bufs_and_descs(2)
        harness._bench_op(
            "WRITE", transport, bufs, descs,
            warmup=1, iters=4, timeout_ms=1000, op="write",
        )
        assert transport.post_write.call_count == 2 * (1 + 4)

    def test_timeout_raises_and_drains(self):
        transport, future = self._make_mock_transport()
        transport.poll_completion.return_value = False  # simulate timeout
        bufs, descs = self._make_bufs_and_descs(1)
        with pytest.raises(RuntimeError, match="timed out"):
            harness._bench_op(
                "READ", transport, bufs, descs,
                warmup=0, iters=1, timeout_ms=100, op="read",
            )
        transport.drain_on_timeout.assert_called_once()


# ===========================================================================
# _verify_read — unit test with mocked transport + real memory
# ===========================================================================

class TestVerifyRead:
    def test_correct_pattern_passes(self):
        transport = mock.MagicMock()
        transport.poll_completion.return_value = True

        num_bufs = 3
        buf_size = 64
        bufs = []
        descs = []
        for i in range(num_bufs):
            raw = (ctypes.c_uint8 * buf_size)(*[(i + 1) & 0xFF] * buf_size)
            buf = mock.MagicMock()
            buf.addr = ctypes.addressof(raw)
            buf.length = buf_size
            buf._raw = raw  # keep alive
            mr = mock.MagicMock()
            mr.addr = buf.addr
            mr.rkey = i + 10
            buf.mr = mr
            bufs.append(buf)
            descs.append(MrDescriptor(index=i, addr=buf.addr, rkey=i + 10, length=buf_size))

        harness._verify_read(transport, bufs, descs, timeout_ms=1000)

    def test_wrong_byte_raises(self):
        transport = mock.MagicMock()
        transport.poll_completion.return_value = True

        raw = (ctypes.c_uint8 * 16)(*[0xFF] * 16)  # wrong byte for index=0 (expected 1)
        buf = mock.MagicMock()
        buf.addr = ctypes.addressof(raw)
        buf.length = 16
        buf._raw = raw
        mr = mock.MagicMock()
        mr.addr = buf.addr
        mr.rkey = 1
        buf.mr = mr
        desc = MrDescriptor(index=0, addr=buf.addr, rkey=1, length=16)

        with pytest.raises(RuntimeError, match="verify FAILED"):
            harness._verify_read(transport, [buf], [desc], timeout_ms=1000)

    def test_read_timeout_raises(self):
        transport = mock.MagicMock()
        transport.poll_completion.return_value = False  # simulate timeout

        raw = (ctypes.c_uint8 * 16)(*[1] * 16)
        buf = mock.MagicMock()
        buf.addr = ctypes.addressof(raw)
        buf.length = 16
        buf._raw = raw
        mr = mock.MagicMock()
        buf.mr = mr
        desc = MrDescriptor(index=0, addr=buf.addr, rkey=1, length=16)

        with pytest.raises(RuntimeError, match="timed out"):
            harness._verify_read(transport, [buf], [desc], timeout_ms=100)
        transport.drain_on_timeout.assert_called_once()
