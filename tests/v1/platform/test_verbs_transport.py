# SPDX-License-Identifier: Apache-2.0
"""Unit tests for VerbsRdmaTransport.

All tests mock pyverbs — no RDMA hardware needed.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


@pytest.fixture(autouse=True)
def mock_pyverbs(monkeypatch):
    """Inject mock pyverbs modules so VerbsRdmaTransport can be imported."""
    mock_device = MagicMock()
    mock_pd = MagicMock()
    mock_cq = MagicMock()
    mock_qp = MagicMock()
    mock_mr = MagicMock()
    mock_wr = MagicMock()
    mock_enums = MagicMock()
    mock_addr = MagicMock()

    mock_enums.IBV_QPT_RC = 2
    mock_enums.IBV_QPS_INIT = 1
    mock_enums.IBV_QPS_RTR = 2
    mock_enums.IBV_QPS_RTS = 3
    mock_enums.IBV_QPS_ERR = 5
    mock_enums.IBV_WR_RDMA_READ = 12
    mock_enums.IBV_WC_SUCCESS = 0
    mock_enums.IBV_ACCESS_LOCAL_WRITE = 1
    mock_enums.IBV_ACCESS_REMOTE_READ = 2
    mock_enums.IBV_MTU_4096 = 5
    mock_enums.IBV_QP_STATE = 1
    mock_enums.IBV_QP_PKEY_INDEX = 2
    mock_enums.IBV_QP_PORT = 4
    mock_enums.IBV_QP_ACCESS_FLAGS = 8
    mock_enums.IBV_QP_AV = 16
    mock_enums.IBV_QP_PATH_MTU = 32
    mock_enums.IBV_QP_DEST_QPN = 64
    mock_enums.IBV_QP_RQ_PSN = 128
    mock_enums.IBV_QP_MAX_DEST_RD_ATOMIC = 256
    mock_enums.IBV_QP_MIN_RNR_TIMER = 512
    mock_enums.IBV_QP_SQ_PSN = 1024
    mock_enums.IBV_QP_MAX_QP_RD_ATOMIC = 2048
    mock_enums.IBV_QP_RETRY_CNT = 4096
    mock_enums.IBV_QP_RNR_RETRY = 8192
    mock_enums.IBV_QP_TIMEOUT = 16384

    modules = {
        "pyverbs": MagicMock(),
        "pyverbs.device": mock_device,
        "pyverbs.pd": mock_pd,
        "pyverbs.cq": mock_cq,
        "pyverbs.qp": mock_qp,
        "pyverbs.mr": mock_mr,
        "pyverbs.wr": mock_wr,
        "pyverbs.enums": mock_enums,
        "pyverbs.addr": mock_addr,
    }

    for name, mod in modules.items():
        monkeypatch.setitem(__import__("sys").modules, name, mod)

    import importlib
    import lmcache.v1.platform.ipu.verbs_transport as vt_mod
    monkeypatch.setattr(vt_mod, "HAS_PYVERBS", True)
    monkeypatch.setattr(vt_mod, "VerbsContext", mock_device.Context)
    monkeypatch.setattr(vt_mod, "PD", mock_pd.PD)
    monkeypatch.setattr(vt_mod, "CQ", mock_cq.CQ)
    monkeypatch.setattr(vt_mod, "QP", mock_qp.QP)
    monkeypatch.setattr(vt_mod, "QPInitAttr", mock_qp.QPInitAttr)
    monkeypatch.setattr(vt_mod, "QPAttr", mock_qp.QPAttr)
    monkeypatch.setattr(vt_mod, "QPCap", mock_qp.QPCap)
    monkeypatch.setattr(vt_mod, "MR", mock_mr.MR)
    monkeypatch.setattr(vt_mod, "SendWR", mock_wr.SendWR)
    monkeypatch.setattr(vt_mod, "SGE", mock_wr.SGE)
    monkeypatch.setattr(vt_mod, "AHAttr", mock_addr.AHAttr)
    monkeypatch.setattr(vt_mod, "GlobalRoute", mock_addr.GlobalRoute)
    monkeypatch.setattr(vt_mod, "IBV_QPT_RC", 2)
    monkeypatch.setattr(vt_mod, "IBV_QPS_INIT", 1)
    monkeypatch.setattr(vt_mod, "IBV_QPS_RTR", 2)
    monkeypatch.setattr(vt_mod, "IBV_QPS_RTS", 3)
    monkeypatch.setattr(vt_mod, "IBV_QPS_ERR", 5)
    monkeypatch.setattr(vt_mod, "IBV_WR_RDMA_READ", 12)
    monkeypatch.setattr(vt_mod, "IBV_WC_SUCCESS", 0)
    monkeypatch.setattr(vt_mod, "IBV_ACCESS_LOCAL_WRITE", 1)
    monkeypatch.setattr(vt_mod, "IBV_ACCESS_REMOTE_READ", 2)
    monkeypatch.setattr(vt_mod, "IBV_MTU_4096", 5)
    monkeypatch.setattr(vt_mod, "IBV_QP_STATE", 1)
    monkeypatch.setattr(vt_mod, "IBV_QP_PKEY_INDEX", 2)
    monkeypatch.setattr(vt_mod, "IBV_QP_PORT", 4)
    monkeypatch.setattr(vt_mod, "IBV_QP_ACCESS_FLAGS", 8)
    monkeypatch.setattr(vt_mod, "IBV_QP_AV", 16)
    monkeypatch.setattr(vt_mod, "IBV_QP_PATH_MTU", 32)
    monkeypatch.setattr(vt_mod, "IBV_QP_DEST_QPN", 64)
    monkeypatch.setattr(vt_mod, "IBV_QP_RQ_PSN", 128)
    monkeypatch.setattr(vt_mod, "IBV_QP_MAX_DEST_RD_ATOMIC", 256)
    monkeypatch.setattr(vt_mod, "IBV_QP_MIN_RNR_TIMER", 512)
    monkeypatch.setattr(vt_mod, "IBV_QP_SQ_PSN", 1024)
    monkeypatch.setattr(vt_mod, "IBV_QP_MAX_QP_RD_ATOMIC", 2048)
    monkeypatch.setattr(vt_mod, "IBV_QP_RETRY_CNT", 4096)
    monkeypatch.setattr(vt_mod, "IBV_QP_RNR_RETRY", 8192)
    monkeypatch.setattr(vt_mod, "IBV_QP_TIMEOUT", 16384)

    yield vt_mod


def _make_transport(mock_pyverbs, role="target", connect=True):
    """Create a VerbsRdmaTransport with mocked verbs objects."""
    vt_mod = mock_pyverbs

    ctx_mock = MagicMock()
    port_attr = MagicMock()
    port_attr.lid = 1
    ctx_mock.query_port.return_value = port_attr
    gid_mock = MagicMock()
    gid_mock.gid = b"\xfe\x80" + b"\x00" * 14
    ctx_mock.query_gid.return_value = gid_mock
    vt_mod.VerbsContext.return_value = ctx_mock

    qp_mock = MagicMock()
    qp_mock.qp_num = 42
    vt_mod.QP.return_value = qp_mock

    transport = vt_mod.VerbsRdmaTransport(
        role=role, device="mlx5_0", port=1, gid_index=0, local_psn=100
    )

    if connect:
        transport.connect(
            remote_qpn=99, remote_psn=200,
            remote_gid="fe80000000000000" + "0" * 16, remote_lid=2,
        )

    return transport


class TestFromEnv:
    def test_missing_role_raises(self, mock_pyverbs, monkeypatch):
        monkeypatch.delenv("LMCACHE_RDMA_ROLE", raising=False)
        with pytest.raises(ValueError, match="LMCACHE_RDMA_ROLE"):
            mock_pyverbs.VerbsRdmaTransport.from_env()

    def test_parses_all_vars(self, mock_pyverbs, monkeypatch, tmp_path):
        monkeypatch.setenv("LMCACHE_RDMA_ROLE", "target")
        monkeypatch.setenv("LMCACHE_RDMA_DEVICE", "mlx5_0")
        monkeypatch.setenv("LMCACHE_RDMA_PORT", "1")
        monkeypatch.setenv("LMCACHE_RDMA_GID_INDEX", "0")
        monkeypatch.setenv("LMCACHE_RDMA_LOCAL_PSN", "500")
        monkeypatch.setenv("LMCACHE_RDMA_REMOTE_QPN", "42")
        monkeypatch.setenv("LMCACHE_RDMA_REMOTE_PSN", "200")
        monkeypatch.setenv("LMCACHE_RDMA_REMOTE_GID", "fe80" + "0" * 28)
        monkeypatch.setenv("LMCACHE_RDMA_REMOTE_LID", "2")

        ctx_mock = MagicMock()
        port_attr = MagicMock()
        port_attr.lid = 1
        ctx_mock.query_port.return_value = port_attr
        gid_mock = MagicMock()
        gid_mock.gid = b"\xfe\x80" + b"\x00" * 14
        ctx_mock.query_gid.return_value = gid_mock
        mock_pyverbs.VerbsContext.return_value = ctx_mock

        qp_mock = MagicMock()
        qp_mock.qp_num = 42
        mock_pyverbs.QP.return_value = qp_mock

        transport = mock_pyverbs.VerbsRdmaTransport.from_env()
        assert transport._role == "target"
        assert transport._local_psn == 500

    def test_import_error_without_pyverbs(self, mock_pyverbs):
        mock_pyverbs.HAS_PYVERBS = False
        with pytest.raises(ImportError, match="pyverbs"):
            mock_pyverbs.VerbsRdmaTransport(
                role="target", device="mlx5_0", port=1,
                gid_index=0, local_psn=100,
            )


class TestRoleValidation:
    def test_invalid_role_raises(self, mock_pyverbs):
        with pytest.raises(ValueError, match="initiator.*target"):
            mock_pyverbs.VerbsRdmaTransport(
                role="invalid", device="mlx5_0", port=1,
                gid_index=0, local_psn=100,
            )

    def test_initiator_post_read_raises(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="initiator")
        from lmcache.v1.platform.ipu.rdma_transport import RegisteredBuffer, MrInfo
        buf = RegisteredBuffer(addr=0x1000, length=4096,
                              mr=MrInfo(rkey=1, addr=0x1000, length=4096, handle=MagicMock()))
        with pytest.raises(RuntimeError, match="target role"):
            transport.post_read(buf, remote_addr=0x2000, rkey=5, length=4096)


class TestPostRead:
    def test_returns_future(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        from lmcache.v1.platform.ipu.rdma_transport import RegisteredBuffer, MrInfo, RdmaFuture
        mr = MrInfo(rkey=1, addr=0x1000, length=4096, handle=MagicMock())
        buf = RegisteredBuffer(addr=0x1000, length=4096, mr=mr)
        future = transport.post_read(buf, remote_addr=0x2000, rkey=5, length=4096)
        assert isinstance(future, RdmaFuture)
        # Release the lock post_read() left held, else __del__ -> close()
        # deadlocks re-acquiring it when transport is garbage collected.
        wc = MagicMock()
        wc.wr_id = transport._inflight_wr_id
        wc.status = 0  # IBV_WC_SUCCESS
        transport._cq.poll.return_value = (1, [wc])
        transport.poll_completion(future, timeout_ms=100)

    def test_concurrent_blocks(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        from lmcache.v1.platform.ipu.rdma_transport import RegisteredBuffer, MrInfo
        mr = MrInfo(rkey=1, addr=0x1000, length=4096, handle=MagicMock())
        buf = RegisteredBuffer(addr=0x1000, length=4096, mr=mr)

        future = transport.post_read(buf, remote_addr=0x2000, rkey=5, length=4096)

        blocked = threading.Event()
        acquired = threading.Event()

        def try_acquire():
            blocked.set()
            transport._qp_lock.acquire()
            acquired.set()
            transport._qp_lock.release()

        t = threading.Thread(target=try_acquire)
        t.start()
        blocked.wait(timeout=1.0)
        time.sleep(0.05)
        assert not acquired.is_set()

        transport._inflight_future.set_complete(success=True)
        transport._inflight_future = None
        transport._qp_lock.release()

        t.join(timeout=1.0)
        assert acquired.is_set()


class TestPollCompletion:
    def test_success(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        from lmcache.v1.platform.ipu.rdma_transport import RegisteredBuffer, MrInfo
        mr = MrInfo(rkey=1, addr=0x1000, length=4096, handle=MagicMock())
        buf = RegisteredBuffer(addr=0x1000, length=4096, mr=mr)

        future = transport.post_read(buf, remote_addr=0x2000, rkey=5, length=4096)

        wc = MagicMock()
        wc.wr_id = transport._inflight_wr_id
        wc.status = 0  # IBV_WC_SUCCESS
        transport._cq.poll.return_value = (1, [wc])

        result = transport.poll_completion(future, timeout_ms=100)
        assert result is True

    def test_timeout_returns_false(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        from lmcache.v1.platform.ipu.rdma_transport import RegisteredBuffer, MrInfo
        mr = MrInfo(rkey=1, addr=0x1000, length=4096, handle=MagicMock())
        buf = RegisteredBuffer(addr=0x1000, length=4096, mr=mr)

        future = transport.post_read(buf, remote_addr=0x2000, rkey=5, length=4096)
        transport._cq.poll.return_value = (0, [])

        result = transport.poll_completion(future, timeout_ms=50)
        assert result is False
        # Clean up: release the lock held after timeout
        flush_wc = MagicMock()
        flush_wc.wr_id = transport._inflight_wr_id
        transport._cq.poll.return_value = (1, [flush_wc])
        transport.drain_on_timeout()

    def test_releases_lock_on_success(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        from lmcache.v1.platform.ipu.rdma_transport import RegisteredBuffer, MrInfo
        mr = MrInfo(rkey=1, addr=0x1000, length=4096, handle=MagicMock())
        buf = RegisteredBuffer(addr=0x1000, length=4096, mr=mr)

        future = transport.post_read(buf, remote_addr=0x2000, rkey=5, length=4096)
        wc = MagicMock()
        wc.wr_id = transport._inflight_wr_id
        wc.status = 0
        transport._cq.poll.return_value = (1, [wc])

        transport.poll_completion(future, timeout_ms=100)
        assert transport._qp_lock.acquire(blocking=False)
        transport._qp_lock.release()

    def test_skips_mismatched_wr_id(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        from lmcache.v1.platform.ipu.rdma_transport import RegisteredBuffer, MrInfo
        mr = MrInfo(rkey=1, addr=0x1000, length=4096, handle=MagicMock())
        buf = RegisteredBuffer(addr=0x1000, length=4096, mr=mr)

        future = transport.post_read(buf, remote_addr=0x2000, rkey=5, length=4096)

        stale_wc = MagicMock()
        stale_wc.wr_id = 9999
        stale_wc.status = 0

        good_wc = MagicMock()
        good_wc.wr_id = transport._inflight_wr_id
        good_wc.status = 0

        transport._cq.poll.side_effect = [(1, [stale_wc]), (1, [good_wc])]
        result = transport.poll_completion(future, timeout_ms=1000)
        assert result is True

    def test_wrong_future_raises(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        from lmcache.v1.platform.ipu.rdma_transport import RegisteredBuffer, MrInfo, RdmaFuture
        mr = MrInfo(rkey=1, addr=0x1000, length=4096, handle=MagicMock())
        buf = RegisteredBuffer(addr=0x1000, length=4096, mr=mr)
        transport.post_read(buf, remote_addr=0x2000, rkey=5, length=4096)

        wrong_future = RdmaFuture()
        with pytest.raises(RuntimeError, match="non-inflight"):
            transport.poll_completion(wrong_future, timeout_ms=100)
        # Lock should be released by poll_completion on wrong-future path
        assert transport._qp_lock.acquire(blocking=False)
        transport._qp_lock.release()

    def test_error_wc_returns_false(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        from lmcache.v1.platform.ipu.rdma_transport import RegisteredBuffer, MrInfo
        mr = MrInfo(rkey=1, addr=0x1000, length=4096, handle=MagicMock())
        buf = RegisteredBuffer(addr=0x1000, length=4096, mr=mr)

        future = transport.post_read(buf, remote_addr=0x2000, rkey=5, length=4096)
        wc = MagicMock()
        wc.wr_id = transport._inflight_wr_id
        wc.status = 5  # not SUCCESS
        transport._cq.poll.return_value = (1, [wc])

        result = transport.poll_completion(future, timeout_ms=100)
        assert result is False

    def test_cq_exception_releases_lock(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        from lmcache.v1.platform.ipu.rdma_transport import RegisteredBuffer, MrInfo
        mr = MrInfo(rkey=1, addr=0x1000, length=4096, handle=MagicMock())
        buf = RegisteredBuffer(addr=0x1000, length=4096, mr=mr)

        future = transport.post_read(buf, remote_addr=0x2000, rkey=5, length=4096)
        transport._cq.poll.side_effect = RuntimeError("CQ destroyed")

        with pytest.raises(RuntimeError, match="CQ destroyed"):
            transport.poll_completion(future, timeout_ms=100)
        # Lock must be released despite exception
        assert transport._qp_lock.acquire(blocking=False)
        transport._qp_lock.release()
        assert transport._drained is True


class TestDrainOnTimeout:
    def test_returns_true_on_flush(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        from lmcache.v1.platform.ipu.rdma_transport import RegisteredBuffer, MrInfo
        mr = MrInfo(rkey=1, addr=0x1000, length=4096, handle=MagicMock())
        buf = RegisteredBuffer(addr=0x1000, length=4096, mr=mr)

        future = transport.post_read(buf, remote_addr=0x2000, rkey=5, length=4096)
        transport._cq.poll.return_value = (0, [])
        transport.poll_completion(future, timeout_ms=10)

        flush_wc = MagicMock()
        flush_wc.wr_id = transport._inflight_wr_id
        transport._cq.poll.return_value = (1, [flush_wc])

        result = transport.drain_on_timeout()
        assert result is True
        assert transport._drained is True

    def test_returns_false_on_no_flush(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        from lmcache.v1.platform.ipu.rdma_transport import RegisteredBuffer, MrInfo
        mr = MrInfo(rkey=1, addr=0x1000, length=4096, handle=MagicMock())
        buf = RegisteredBuffer(addr=0x1000, length=4096, mr=mr)

        future = transport.post_read(buf, remote_addr=0x2000, rkey=5, length=4096)
        transport._cq.poll.return_value = (0, [])
        transport.poll_completion(future, timeout_ms=10)

        transport._cq.poll.return_value = (0, [])

        with patch("time.sleep"):
            with patch("time.monotonic", side_effect=[0.0, 0.0, 3.0]):
                result = transport.drain_on_timeout()
        assert result is False

    def test_releases_lock(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        from lmcache.v1.platform.ipu.rdma_transport import RegisteredBuffer, MrInfo
        mr = MrInfo(rkey=1, addr=0x1000, length=4096, handle=MagicMock())
        buf = RegisteredBuffer(addr=0x1000, length=4096, mr=mr)

        future = transport.post_read(buf, remote_addr=0x2000, rkey=5, length=4096)
        transport._cq.poll.return_value = (0, [])
        transport.poll_completion(future, timeout_ms=10)

        flush_wc = MagicMock()
        flush_wc.wr_id = transport._inflight_wr_id
        transport._cq.poll.return_value = (1, [flush_wc])
        transport.drain_on_timeout()

        assert transport._qp_lock.acquire(blocking=False)
        transport._qp_lock.release()

    def test_sets_qp_error(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        from lmcache.v1.platform.ipu.rdma_transport import RegisteredBuffer, MrInfo
        mr = MrInfo(rkey=1, addr=0x1000, length=4096, handle=MagicMock())
        buf = RegisteredBuffer(addr=0x1000, length=4096, mr=mr)

        future = transport.post_read(buf, remote_addr=0x2000, rkey=5, length=4096)
        transport._cq.poll.return_value = (0, [])
        transport.poll_completion(future, timeout_ms=10)

        flush_wc = MagicMock()
        flush_wc.wr_id = transport._inflight_wr_id
        transport._cq.poll.return_value = (1, [flush_wc])
        transport.drain_on_timeout()

        transport._qp.modify.assert_called()


class TestRegisterMr:
    def test_target_local_write_only(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        mr_mock = MagicMock()
        mr_mock.rkey = 100
        mock_pyverbs.MR.return_value = mr_mock

        mr_info = transport.register_mr(0x5000, 4096)
        call_args = mock_pyverbs.MR.call_args
        access = call_args[0][2]  # MR(pd, length, access, address=...)
        assert access == 1  # LOCAL_WRITE only

    def test_initiator_includes_remote_read(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="initiator")
        mr_mock = MagicMock()
        mr_mock.rkey = 100
        mock_pyverbs.MR.return_value = mr_mock

        mr_info = transport.register_mr(0x5000, 4096)
        call_args = mock_pyverbs.MR.call_args
        access = call_args[0][2]  # MR(pd, length, access, address=...)
        assert access == 3  # LOCAL_WRITE | REMOTE_READ

    def test_deregister_callback_set(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        mr_mock = MagicMock()
        mr_mock.rkey = 100
        mock_pyverbs.MR.return_value = mr_mock

        mr_info = transport.register_mr(0x5000, 4096)
        assert mr_info.deregister is not None


class TestAllocateBuffer:
    def test_page_aligned(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        mr_mock = MagicMock()
        mr_mock.rkey = 100
        mock_pyverbs.MR.return_value = mr_mock

        buf = transport.allocate_buffer(1000)
        assert buf.addr % 4096 == 0
        assert buf.length == 4096

    def test_free_buffer_deregisters_and_unmaps(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        mr_mock = MagicMock()
        mr_mock.rkey = 100
        mock_pyverbs.MR.return_value = mr_mock

        buf = transport.allocate_buffer(4096)
        backing = buf.backing
        transport.free_buffer(buf)
        assert backing.closed

    def test_release_tracking(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        mr_mock = MagicMock()
        mr_mock.rkey = 100
        mock_pyverbs.MR.return_value = mr_mock

        buf = transport.allocate_buffer(4096)
        assert buf in transport._allocated_buffers
        transport.release_buffer_tracking(buf)
        assert buf not in transport._allocated_buffers


class TestRendezvous:
    def test_endpoint_file_written(self, mock_pyverbs, monkeypatch, tmp_path):
        monkeypatch.setenv("LMCACHE_RDMA_ROLE", "target")
        monkeypatch.setenv("LMCACHE_RDMA_DEVICE", "mlx5_0")
        monkeypatch.setenv("LMCACHE_RDMA_LOCAL_PSN", "100")
        monkeypatch.setenv("LMCACHE_RDMA_NONCE", "test123")

        endpoint_file = tmp_path / "target.json"
        peer_file = tmp_path / "initiator.json"
        monkeypatch.setenv("LMCACHE_RDMA_ENDPOINT_FILE", str(endpoint_file))
        monkeypatch.setenv("LMCACHE_RDMA_PEER_ENDPOINT_FILE", str(peer_file))

        peer_data = {
            "qpn": 99, "psn": 200,
            "gid": "fe80" + "0" * 28, "lid": 2,
            "nonce": "test123",
        }
        peer_file.write_text(json.dumps(peer_data))

        ctx_mock = MagicMock()
        port_attr = MagicMock()
        port_attr.lid = 1
        ctx_mock.query_port.return_value = port_attr
        gid_mock = MagicMock()
        gid_mock.gid = b"\xfe\x80" + b"\x00" * 14
        ctx_mock.query_gid.return_value = gid_mock
        mock_pyverbs.VerbsContext.return_value = ctx_mock

        qp_mock = MagicMock()
        qp_mock.qp_num = 42
        mock_pyverbs.QP.return_value = qp_mock

        transport = mock_pyverbs.VerbsRdmaTransport.from_env()

        assert endpoint_file.exists()
        data = json.loads(endpoint_file.read_text())
        assert data["nonce"] == "test123"
        assert data["qpn"] == 42
        assert "pid" in data

    def test_stale_peer_file_rejected(self, mock_pyverbs, monkeypatch, tmp_path):
        monkeypatch.setenv("LMCACHE_RDMA_ROLE", "target")
        monkeypatch.setenv("LMCACHE_RDMA_DEVICE", "mlx5_0")
        monkeypatch.setenv("LMCACHE_RDMA_LOCAL_PSN", "100")
        monkeypatch.setenv("LMCACHE_RDMA_NONCE", "fresh_nonce")

        endpoint_file = tmp_path / "target.json"
        peer_file = tmp_path / "initiator.json"
        monkeypatch.setenv("LMCACHE_RDMA_ENDPOINT_FILE", str(endpoint_file))
        monkeypatch.setenv("LMCACHE_RDMA_PEER_ENDPOINT_FILE", str(peer_file))

        stale_data = {
            "qpn": 99, "psn": 200,
            "gid": "fe80" + "0" * 28, "lid": 2,
            "nonce": "old_stale_nonce",
        }
        peer_file.write_text(json.dumps(stale_data))

        ctx_mock = MagicMock()
        port_attr = MagicMock()
        port_attr.lid = 1
        ctx_mock.query_port.return_value = port_attr
        gid_mock = MagicMock()
        gid_mock.gid = b"\xfe\x80" + b"\x00" * 14
        ctx_mock.query_gid.return_value = gid_mock
        mock_pyverbs.VerbsContext.return_value = ctx_mock

        qp_mock = MagicMock()
        qp_mock.qp_num = 42
        mock_pyverbs.QP.return_value = qp_mock

        with pytest.raises(TimeoutError):
            with patch("time.sleep"):
                with patch("time.monotonic", side_effect=[0.0, 0.0, 31.0]):
                    mock_pyverbs.VerbsRdmaTransport.from_env()


class TestClose:
    def test_tears_down_resources(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        transport.close()
        assert transport._closed is True
        assert transport._qp is None
        assert transport._cq is None
        assert transport._pd is None
        assert transport._ctx is None

    def test_post_read_after_close_raises(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        transport.close()

        from lmcache.v1.platform.ipu.rdma_transport import RegisteredBuffer, MrInfo
        mr = MrInfo(rkey=1, addr=0x1000, length=4096, handle=MagicMock())
        buf = RegisteredBuffer(addr=0x1000, length=4096, mr=mr)
        with pytest.raises(RuntimeError, match="closed or drained"):
            transport.post_read(buf, remote_addr=0x2000, rkey=5, length=4096)

    def test_close_idempotent(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        transport.close()
        transport.close()

    def test_close_frees_tracked_buffers(self, mock_pyverbs):
        transport = _make_transport(mock_pyverbs, role="target")
        mr_mock = MagicMock()
        mr_mock.rkey = 100
        mock_pyverbs.MR.return_value = mr_mock

        buf = transport.allocate_buffer(4096)
        assert buf in transport._allocated_buffers
        transport.close()
        assert len(transport._allocated_buffers) == 0
