# SPDX-License-Identifier: Apache-2.0
"""VerbsRdmaTransport — real libibverbs backend via pyverbs.

Activated by LMCACHE_RDMA_TRANSPORT=verbs. Requires rdma-core with Python
bindings (apt install rdma-core python3-pyverbs).
"""

from __future__ import annotations

import ctypes
import json
import mmap
import os
import random
import threading
import time
from pathlib import Path

from lmcache.logging import init_logger
from lmcache.v1.platform.ipu.rdma_transport import (
    MrInfo,
    RdmaFuture,
    RegisteredBuffer,
)

logger = init_logger(__name__)

try:
    from pyverbs.device import Context as VerbsContext
    from pyverbs.pd import PD
    from pyverbs.cq import CQ
    from pyverbs.qp import QP, QPInitAttr, QPAttr, QPCap
    from pyverbs.mr import MR
    from pyverbs.wr import SendWR, SGE
    from pyverbs.enums import (
        IBV_QPT_RC,
        IBV_QPS_INIT,
        IBV_QPS_RTR,
        IBV_QPS_RTS,
        IBV_QPS_ERR,
        IBV_WR_RDMA_READ,
        IBV_WC_SUCCESS,
        IBV_ACCESS_LOCAL_WRITE,
        IBV_ACCESS_REMOTE_READ,
        IBV_MTU_4096,
        IBV_QP_STATE,
        IBV_QP_PKEY_INDEX,
        IBV_QP_PORT,
        IBV_QP_ACCESS_FLAGS,
        IBV_QP_AV,
        IBV_QP_PATH_MTU,
        IBV_QP_DEST_QPN,
        IBV_QP_RQ_PSN,
        IBV_QP_MAX_DEST_RD_ATOMIC,
        IBV_QP_MIN_RNR_TIMER,
        IBV_QP_SQ_PSN,
        IBV_QP_MAX_QP_RD_ATOMIC,
        IBV_QP_RETRY_CNT,
        IBV_QP_RNR_RETRY,
        IBV_QP_TIMEOUT,
    )
    from pyverbs.addr import AHAttr, GlobalRoute
    HAS_PYVERBS = True
except ImportError:
    HAS_PYVERBS = False


def _parse_gid(hex_str: str) -> bytes:
    """Convert a 32-char hex GID string to 16 bytes."""
    if len(hex_str) != 32:
        raise ValueError(f"GID must be 32 hex chars, got {len(hex_str)}: {hex_str!r}")
    return bytes.fromhex(hex_str)


class VerbsRdmaTransport:
    """RdmaTransport implementation backed by libibverbs via pyverbs.

    Supports both initiator (exposes registered DRAM for remote reads) and
    target (posts RDMA Reads to pull data) roles.
    """

    def __init__(
        self,
        role: str,
        device: str,
        port: int,
        gid_index: int,
        local_psn: int,
    ) -> None:
        if not HAS_PYVERBS:
            raise ImportError(
                "pyverbs (rdma-core) required for verbs transport. "
                "Install: apt install rdma-core python3-pyverbs"
            )
        if role not in ("initiator", "target"):
            raise ValueError(f"LMCACHE_RDMA_ROLE must be 'initiator' or 'target', got {role!r}")

        self._role = role
        self._port = port
        self._gid_index = gid_index
        self._local_psn = local_psn
        self._closed = False
        self._drained = False
        self._qp_lock = threading.Lock()
        self._inflight_future: RdmaFuture | None = None
        self._inflight_wr_id: int = 0
        self._next_wr_id: int = 1
        self._allocated_buffers: set[RegisteredBuffer] = set()
        self._endpoint_file: Path | None = None

        self._ctx = VerbsContext(name=device)
        self._pd = PD(self._ctx)
        self._cq = CQ(self._ctx, cqe=128)

        cap = QPCap(max_send_wr=64, max_recv_wr=1, max_send_sge=1, max_recv_sge=1)
        init_attr = QPInitAttr(
            qp_type=IBV_QPT_RC,
            send_cq=self._cq,
            recv_cq=self._cq,
            cap=cap,
            sq_sig_all=True,
        )
        self._qp = QP(self._pd, init_attr)

        self._modify_to_init()

        port_attr = self._ctx.query_port(port)
        self._local_lid = port_attr.lid
        gid = self._ctx.query_gid(port, gid_index)
        self._local_gid = gid.gid.hex() if hasattr(gid, 'gid') else gid.hex()
        self._local_qpn = self._qp.qp_num

    @classmethod
    def from_env(cls) -> "VerbsRdmaTransport":
        """Create transport from environment variables."""
        role = os.environ.get("LMCACHE_RDMA_ROLE")
        if role is None:
            raise ValueError(
                "LMCACHE_RDMA_ROLE must be set to 'initiator' or 'target'"
            )

        device = os.environ.get("LMCACHE_RDMA_DEVICE", "")
        port = int(os.environ.get("LMCACHE_RDMA_PORT", "1"))
        gid_index = int(os.environ.get("LMCACHE_RDMA_GID_INDEX", "0"))

        psn_str = os.environ.get("LMCACHE_RDMA_LOCAL_PSN")
        local_psn = int(psn_str) if psn_str else random.randint(0, 0xFFFFFF)

        transport = cls(
            role=role,
            device=device,
            port=port,
            gid_index=gid_index,
            local_psn=local_psn,
        )

        remote_qpn = os.environ.get("LMCACHE_RDMA_REMOTE_QPN")
        remote_psn = os.environ.get("LMCACHE_RDMA_REMOTE_PSN")
        remote_gid = os.environ.get("LMCACHE_RDMA_REMOTE_GID")

        if remote_qpn and remote_psn:
            remote_lid = int(os.environ.get("LMCACHE_RDMA_REMOTE_LID", "0"))
            transport.connect(
                remote_qpn=int(remote_qpn),
                remote_psn=int(remote_psn),
                remote_gid=remote_gid or "",
                remote_lid=remote_lid,
            )
        else:
            transport._rendezvous()

        return transport

    def _rendezvous(self) -> None:
        """Two-phase bootstrap using endpoint files."""
        nonce = os.environ.get("LMCACHE_RDMA_NONCE")
        if nonce is None:
            raise ValueError(
                "LMCACHE_RDMA_NONCE required for rendezvous mode "
                "(set matching nonce on both sides)"
            )

        peer_role = "initiator" if self._role == "target" else "target"
        endpoint_path = os.environ.get(
            "LMCACHE_RDMA_ENDPOINT_FILE",
            f"/tmp/lmcache_rdma_{self._role}_{nonce}.json",
        )
        peer_path = os.environ.get(
            "LMCACHE_RDMA_PEER_ENDPOINT_FILE",
            f"/tmp/lmcache_rdma_{peer_role}_{nonce}.json",
        )

        self._endpoint_file = Path(endpoint_path)
        self._write_endpoint_file(nonce)

        peer_info = self._poll_peer_file(Path(peer_path), nonce)
        self.connect(
            remote_qpn=peer_info["qpn"],
            remote_psn=peer_info["psn"],
            remote_gid=peer_info.get("gid", ""),
            remote_lid=peer_info.get("lid", 0),
        )

    def _write_endpoint_file(self, nonce: str) -> None:
        """Write local endpoint info atomically."""
        data = {
            "qpn": self._local_qpn,
            "psn": self._local_psn,
            "gid": self._local_gid,
            "lid": self._local_lid,
            "nonce": nonce,
            "pid": os.getpid(),
            "timestamp": time.time(),
        }
        tmp_path = self._endpoint_file.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(data))
        tmp_path.rename(self._endpoint_file)
        logger.info(
            "VERBS_TRANSPORT: wrote endpoint file %s (qpn=%d psn=%d)",
            self._endpoint_file, self._local_qpn, self._local_psn,
        )

    def _poll_peer_file(self, peer_path: Path, nonce: str) -> dict:
        """Poll for peer endpoint file with nonce validation."""
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if peer_path.exists():
                try:
                    data = json.loads(peer_path.read_text())
                    if data.get("nonce") == nonce:
                        logger.info(
                            "VERBS_TRANSPORT: found peer endpoint (qpn=%d psn=%d)",
                            data["qpn"], data["psn"],
                        )
                        return data
                    else:
                        peer_path.unlink(missing_ok=True)
                except (json.JSONDecodeError, KeyError, OSError):
                    pass
            time.sleep(0.1)
        raise TimeoutError(
            f"Peer endpoint file not found after 30s: {peer_path}"
        )

    def connect(
        self,
        remote_qpn: int,
        remote_psn: int,
        remote_gid: str,
        remote_lid: int = 0,
    ) -> None:
        """Transition QP from INIT -> RTR -> RTS using peer info."""
        self._modify_to_rtr(remote_qpn, remote_psn, remote_gid, remote_lid)
        self._modify_to_rts()
        logger.info(
            "VERBS_TRANSPORT: QP connected (role=%s remote_qpn=%d)",
            self._role, remote_qpn,
        )

    def _modify_to_init(self) -> None:
        access = IBV_ACCESS_LOCAL_WRITE
        if self._role == "initiator":
            access |= IBV_ACCESS_REMOTE_READ

        attr = QPAttr()
        attr.qp_state = IBV_QPS_INIT
        attr.pkey_index = 0
        attr.port_num = self._port
        attr.qp_access_flags = access

        mask = IBV_QP_STATE | IBV_QP_PKEY_INDEX | IBV_QP_PORT | IBV_QP_ACCESS_FLAGS
        self._qp.modify(attr, mask)

    def _modify_to_rtr(
        self, remote_qpn: int, remote_psn: int, remote_gid: str, remote_lid: int
    ) -> None:
        gr = GlobalRoute(
            dgid=_parse_gid(remote_gid) if remote_gid else b'\x00' * 16,
            sgid_index=self._gid_index,
            hop_limit=64,
        )
        ah_attr = AHAttr(
            dlid=remote_lid,
            sl=0,
            src_path_bits=0,
            port_num=self._port,
            is_global=1 if remote_gid else 0,
            gr=gr,
        )

        attr = QPAttr()
        attr.qp_state = IBV_QPS_RTR
        attr.path_mtu = IBV_MTU_4096
        attr.dest_qp_num = remote_qpn
        attr.rq_psn = remote_psn
        attr.max_dest_rd_atomic = 4
        attr.min_rnr_timer = 12
        attr.ah_attr = ah_attr

        mask = (
            IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU | IBV_QP_DEST_QPN
            | IBV_QP_RQ_PSN | IBV_QP_MAX_DEST_RD_ATOMIC | IBV_QP_MIN_RNR_TIMER
        )
        self._qp.modify(attr, mask)

    def _modify_to_rts(self) -> None:
        attr = QPAttr()
        attr.qp_state = IBV_QPS_RTS
        attr.sq_psn = self._local_psn
        attr.max_rd_atomic = 4
        attr.retry_cnt = 7
        attr.rnr_retry = 7
        attr.timeout = 14

        mask = (
            IBV_QP_STATE | IBV_QP_SQ_PSN | IBV_QP_MAX_QP_RD_ATOMIC
            | IBV_QP_RETRY_CNT | IBV_QP_RNR_RETRY | IBV_QP_TIMEOUT
        )
        self._qp.modify(attr, mask)

    def register_mr(self, buffer_ptr: int, length: int) -> MrInfo:
        """Register a memory region for RDMA access."""
        access = IBV_ACCESS_LOCAL_WRITE
        if self._role == "initiator":
            access |= IBV_ACCESS_REMOTE_READ
        mr_obj = MR(self._pd, length, access, address=buffer_ptr)
        mr_info = MrInfo(
            rkey=mr_obj.rkey,
            addr=buffer_ptr,
            length=length,
            handle=mr_obj,
            deregister=lambda: self._do_deregister(mr_obj),
        )
        return mr_info

    def _do_deregister(self, mr_obj: MR) -> None:
        try:
            mr_obj.close()
        except Exception as e:
            logger.warning("MR deregister failed (may leak): %s", e)

    def deregister_mr(self, mr: MrInfo) -> None:
        """Deregister a previously registered memory region."""
        if mr.deregister is not None:
            mr.deregister()
        elif mr.handle is not None:
            mr.handle.close()

    def post_read(
        self,
        local_buf: RegisteredBuffer,
        remote_addr: int,
        rkey: int,
        length: int,
    ) -> RdmaFuture:
        """Post an RDMA Read work request (target role only)."""
        if self._role != "target":
            raise RuntimeError("post_read only valid for target role")

        self._qp_lock.acquire()
        try:
            if self._closed or self._drained:
                raise RuntimeError("transport is closed or drained")

            future = RdmaFuture()
            wr_id = self._next_wr_id
            self._next_wr_id += 1
            self._inflight_wr_id = wr_id
            self._inflight_future = future

            sge = SGE(
                addr=local_buf.addr,
                length=length,
                lkey=local_buf.mr.handle.lkey,
            )
            wr = SendWR(wr_id=wr_id, opcode=IBV_WR_RDMA_READ, num_sge=1, sg=[sge])
            wr.set_wr_rdma(rkey=rkey, addr=remote_addr)
            self._qp.post_send(wr)
            return future
        except BaseException:
            self._inflight_future = None
            self._qp_lock.release()
            raise

    def poll_completion(self, future: RdmaFuture, timeout_ms: int = 5000) -> bool:
        """Busy-poll CQ for the inflight WR's completion.

        Lock release semantics:
        - On CQE (success or error status): releases _qp_lock.
        - On timeout (returns False): lock stays held — caller MUST call
          drain_on_timeout() which releases it.
        - On unexpected exception: releases _qp_lock, marks transport drained.
        """
        if future is not self._inflight_future:
            self._inflight_future = None
            self._qp_lock.release()
            raise RuntimeError("poll_completion called with non-inflight future")
        try:
            deadline = time.monotonic() + timeout_ms / 1000.0
            while time.monotonic() < deadline:
                wcs = self._cq.poll(num_entries=1)
                for wc in wcs:
                    if wc.wr_id != self._inflight_wr_id:
                        logger.warning(
                            "CQE wr_id=%d != expected %d (skipped)",
                            wc.wr_id, self._inflight_wr_id,
                        )
                        continue
                    success = wc.status == IBV_WC_SUCCESS
                    self._inflight_future.set_complete(success=success)
                    self._inflight_future = None
                    self._qp_lock.release()
                    return success
            # Timeout — lock intentionally stays held for drain_on_timeout()
            return False
        except BaseException:
            self._drained = True
            self._inflight_future = None
            self._qp_lock.release()
            raise

    def drain_on_timeout(self) -> bool:
        """Move QP to ERROR and drain CQ. Releases _qp_lock."""
        try:
            attr = QPAttr()
            attr.qp_state = IBV_QPS_ERR
            self._qp.modify(attr, IBV_QP_STATE)

            deadline = time.monotonic() + 2.0
            flushed = False
            while time.monotonic() < deadline:
                wcs = self._cq.poll(num_entries=16)
                for wc in wcs:
                    if wc.wr_id == self._inflight_wr_id:
                        flushed = True
                        if self._inflight_future is not None:
                            self._inflight_future.set_complete(success=False)
                            self._inflight_future = None
                if flushed:
                    break
                if not wcs:
                    time.sleep(0.001)

            self._drained = True
            if not flushed:
                logger.error(
                    "drain_on_timeout: WR wr_id=%d not flushed after 2s",
                    self._inflight_wr_id,
                )
            return flushed
        finally:
            self._qp_lock.release()

    def allocate_buffer(self, length: int) -> RegisteredBuffer:
        """Allocate page-aligned mmap buffer registered for RDMA."""
        aligned = (length + 4095) & ~4095
        backing = mmap.mmap(-1, aligned)
        addr = ctypes.addressof(ctypes.c_char.from_buffer(backing))
        mr = self.register_mr(addr, aligned)
        buf = RegisteredBuffer(addr=addr, length=aligned, mr=mr, backing=backing)
        self._allocated_buffers.add(buf)
        return buf

    def release_buffer_tracking(self, buf: RegisteredBuffer) -> None:
        """Transfer buffer ownership from transport to caller."""
        self._allocated_buffers.discard(buf)

    def free_buffer(self, buf: RegisteredBuffer) -> None:
        """Deregister MR and unmap backing memory."""
        self._allocated_buffers.discard(buf)
        if not self._closed:
            self.deregister_mr(buf.mr)
        if buf.backing is not None:
            buf.backing.close()

    def close(self) -> None:
        """Tear down all resources."""
        self._qp_lock.acquire()
        try:
            if self._closed:
                return
            self._closed = True
            for buf in list(self._allocated_buffers):
                self.free_buffer(buf)
            self._allocated_buffers.clear()
            if self._qp:
                self._qp.close()
                self._qp = None
            if self._cq:
                self._cq.close()
                self._cq = None
            if self._pd:
                self._pd.close()
                self._pd = None
            if self._ctx:
                self._ctx.close()
                self._ctx = None
        finally:
            self._qp_lock.release()
        self._cleanup_endpoint_file()

    def _cleanup_endpoint_file(self) -> None:
        if self._endpoint_file is not None:
            try:
                self._endpoint_file.unlink(missing_ok=True)
            except OSError:
                pass

    def __del__(self) -> None:
        # __init__ can raise before any attribute is set (missing pyverbs,
        # invalid role) — the partially-constructed instance is still
        # finalized, and close() assumes _qp_lock exists.
        if hasattr(self, "_qp_lock"):
            self.close()
