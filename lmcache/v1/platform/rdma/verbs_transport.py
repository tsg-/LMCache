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
from lmcache.v1.platform.rdma.rdma_transport import (
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
        IBV_WR_RDMA_WRITE,
        IBV_WC_SUCCESS,
        IBV_ACCESS_LOCAL_WRITE,
        IBV_ACCESS_REMOTE_READ,
        IBV_ACCESS_REMOTE_WRITE,
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
    from pyverbs.addr import AHAttr, GID, GlobalRoute
    HAS_PYVERBS = True
except ImportError:
    HAS_PYVERBS = False


def _parse_gid(hex_str: str) -> bytes:
    """Convert a 32-char hex GID string to 16 bytes."""
    if len(hex_str) != 32:
        raise ValueError(f"GID must be 32 hex chars, got {len(hex_str)}: {hex_str!r}")
    return bytes.fromhex(hex_str)


def _hex_to_pyverbs_gid(hex_str: str) -> "GID":
    """Convert a 32-char hex GID string to a pyverbs.addr.GID.

    GlobalRoute.dgid requires a GID object on this pyverbs version, and
    GID() itself only accepts the colon-delimited IPv6 string form (the
    same form query_gid().gid returns) -- not raw bytes.
    """
    raw = _parse_gid(hex_str)
    return GID(":".join(f"{raw[i]:02x}{raw[i + 1]:02x}" for i in range(0, 16, 2)))


def _gid_to_hex(gid: str | bytes) -> str:
    """Convert a pyverbs GID.gid value to the 32-char hex format used by
    ``_parse_gid``. Some pyverbs versions return the colon-delimited IPv6
    string form (e.g. ``"fe80:...:abcd"``); others return raw 16 bytes.
    """
    if isinstance(gid, bytes):
        return gid.hex()
    return gid.replace(":", "")


class VerbsRdmaTransport:
    """RdmaTransport implementation backed by libibverbs via pyverbs.

    Supports both initiator (exposes registered DRAM for remote reads and
    writes) and target (posts RDMA Reads to pull data, or RDMA Writes to
    push data) roles.

    Multi-QP striping (LMCache-9he): when ``qp_count > 1``, each
    ``post_read`` / ``post_write`` is dispatched round-robin across
    independent QP/CQ/lock slots.  With the default ``qp_count=1`` behaviour
    is identical to the original single-QP implementation.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        role: str,
        device: str,
        port: int,
        gid_index: int,
        local_psn: int,
        qp_count: int = 1,
    ) -> None:
        """Create transport with local resources only (all QPs in INIT state).

        Remote peer info is NOT required at construction — call
        ``connect()`` (single QP) or ``connect_all()`` (multi-QP) to
        transition QPs to RTS.

        Args:
            role: ``"initiator"`` or ``"target"``.
            device: IB/RoCE device name (e.g. ``"mlx5_0"``).
            port: IB port number (typically 1).
            gid_index: GID table index for RoCE (use 3 for RoCEv2).
            local_psn: Local packet sequence number seed.
            qp_count: Number of RC QPs to create for striping.
                Must be >= 1.  Default 1 gives single-QP behaviour
                identical to the pre-striping implementation.

        Raises:
            ImportError: If pyverbs (rdma-core) is not installed.
            ValueError: If ``role`` is not ``"initiator"`` or ``"target"``,
                or if ``qp_count < 1``.
        """
        if not HAS_PYVERBS:
            raise ImportError(
                "pyverbs (rdma-core) required for verbs transport. "
                "Install: apt install rdma-core python3-pyverbs"
            )
        if role not in ("initiator", "target"):
            raise ValueError(
                f"LMCACHE_RDMA_ROLE must be 'initiator' or 'target', got {role!r}"
            )
        if qp_count < 1:
            raise ValueError(f"qp_count must be >= 1, got {qp_count}")

        self._role = role
        self._port = port
        self._gid_index = gid_index
        self._local_psn = local_psn
        self._closed = False
        self._drained = False
        self._allocated_buffers: set[RegisteredBuffer] = set()
        self._endpoint_file: Path | None = None

        # Multi-QP striping state
        self._qp_count: int = qp_count
        self._next_qp: int = 0
        self._stripe_lock: threading.Lock = threading.Lock()

        self._ctx = VerbsContext(name=device)
        self._pd = PD(self._ctx)

        # Per-QP resource lists (index 0 is backward-compat single-QP slot)
        self._qps: list[QP] = []
        self._cqs: list[CQ] = []
        self._qp_locks: list[threading.Lock] = []
        self._inflight_futures: list[RdmaFuture | None] = []
        self._inflight_wr_ids: list[int] = []
        self._next_wr_ids: list[int] = []

        for _ in range(qp_count):
            cq = CQ(self._ctx, cqe=128)
            cap = QPCap(
                max_send_wr=64, max_recv_wr=1, max_send_sge=1, max_recv_sge=1
            )
            init_attr = QPInitAttr(
                qp_type=IBV_QPT_RC,
                scq=cq,
                rcq=cq,
                cap=cap,
                sq_sig_all=True,
            )
            qp = QP(self._pd, init_attr)
            self._qps.append(qp)
            self._cqs.append(cq)
            self._qp_locks.append(threading.Lock())
            self._inflight_futures.append(None)
            self._inflight_wr_ids.append(0)
            self._next_wr_ids.append(1)

        for qp in self._qps:
            self._modify_to_init(qp)

        port_attr = self._ctx.query_port(port)
        self._local_lid = port_attr.lid
        self._path_mtu = port_attr.active_mtu
        gid = self._ctx.query_gid(port, gid_index)
        self._local_gid = _gid_to_hex(gid.gid)
        self._local_qpns: list[int] = [qp.qp_num for qp in self._qps]
        # Backward-compat alias used by rendezvous single-QP path
        self._local_qpn: int = self._local_qpns[0]

    # ------------------------------------------------------------------
    # Backward-compat single-QP properties (slot 0)
    # ------------------------------------------------------------------

    @property
    def _qp(self) -> "QP":
        """Slot-0 QP — backward compat for single-QP callers and tests."""
        return self._qps[0]

    @_qp.setter
    def _qp(self, value: "QP | None") -> None:
        self._qps[0] = value  # type: ignore[assignment]

    @property
    def _cq(self) -> "CQ":
        """Slot-0 CQ — backward compat for single-QP callers and tests."""
        return self._cqs[0]

    @_cq.setter
    def _cq(self, value: "CQ | None") -> None:
        self._cqs[0] = value  # type: ignore[assignment]

    @property
    def _qp_lock(self) -> threading.Lock:
        """Slot-0 QP lock — backward compat for single-QP callers and tests."""
        return self._qp_locks[0]

    @property
    def _inflight_future(self) -> "RdmaFuture | None":
        """Slot-0 inflight future — backward compat for tests."""
        return self._inflight_futures[0]

    @_inflight_future.setter
    def _inflight_future(self, value: "RdmaFuture | None") -> None:
        self._inflight_futures[0] = value

    @property
    def _inflight_wr_id(self) -> int:
        """Slot-0 inflight wr_id — backward compat for tests."""
        return self._inflight_wr_ids[0]

    @_inflight_wr_id.setter
    def _inflight_wr_id(self, value: int) -> None:
        self._inflight_wr_ids[0] = value

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> "VerbsRdmaTransport":
        """Create transport from environment variables.

        Returns:
            A fully connected ``VerbsRdmaTransport`` instance.

        Raises:
            ValueError: If ``LMCACHE_RDMA_ROLE`` is unset or
                ``LMCACHE_RDMA_QP_COUNT < 1``.
        """
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

        qp_count = int(os.environ.get("LMCACHE_RDMA_QP_COUNT", "1"))
        if qp_count < 1:
            raise ValueError(
                f"LMCACHE_RDMA_QP_COUNT must be >= 1, got {qp_count}"
            )

        transport = cls(
            role=role,
            device=device,
            port=port,
            gid_index=gid_index,
            local_psn=local_psn,
            qp_count=qp_count,
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

    # ------------------------------------------------------------------
    # Bootstrap / rendezvous
    # ------------------------------------------------------------------

    def _rendezvous(self) -> None:
        """Two-phase bootstrap using endpoint files.

        Writes local endpoint (with all QP numbers) and polls for the peer
        file.  When ``qp_count > 1`` the peer file must carry a ``"qpns"``
        list whose length matches the local ``qp_count``; mismatch raises
        ``ValueError``.
        """
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

        peer_qpns: list[int] = peer_info.get("qpns", [peer_info["qpn"]])
        if len(peer_qpns) != self._qp_count:
            raise ValueError(
                f"QP count mismatch: local={self._qp_count} "
                f"remote={len(peer_qpns)}"
            )

        self.connect_all(
            remote_qpns=peer_qpns,
            remote_psn=peer_info["psn"],
            remote_gid=peer_info.get("gid", ""),
            remote_lid=peer_info.get("lid", 0),
        )

    def _write_endpoint_file(self, nonce: str) -> None:
        """Write local endpoint info atomically.

        The JSON payload includes both the legacy ``"qpn"`` field (first QP
        number, for single-QP peers) and the new ``"qpns"`` list (all QP
        numbers, for multi-QP rendezvous).

        Args:
            nonce: Session nonce shared by both sides.
        """
        data = {
            "qpn": self._local_qpns[0],
            "qpns": self._local_qpns,
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
            "VERBS_TRANSPORT: wrote endpoint file %s (qpns=%s psn=%d)",
            self._endpoint_file, self._local_qpns, self._local_psn,
        )

    def _poll_peer_file(self, peer_path: Path, nonce: str) -> dict:
        """Poll for peer endpoint file with nonce validation.

        Args:
            peer_path: Filesystem path of the peer's endpoint JSON.
            nonce: Expected nonce value; stale files are discarded.

        Returns:
            Parsed peer endpoint dict.

        Raises:
            TimeoutError: If no valid peer file appears within 30 s.
        """
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

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(
        self,
        remote_qpn: int,
        remote_psn: int,
        remote_gid: str,
        remote_lid: int = 0,
    ) -> None:
        """Transition QP[0] from INIT -> RTR -> RTS using peer info.

        This is the backward-compatible single-QP connect path.  For
        multi-QP striping use ``connect_all()``.

        Args:
            remote_qpn: Peer QP number.
            remote_psn: Peer packet sequence number.
            remote_gid: Peer GID as a 32-char hex string.
            remote_lid: Peer LID (0 for RoCE).
        """
        self._modify_to_rtr(self._qps[0], remote_qpn, remote_psn, remote_gid, remote_lid)
        self._modify_to_rts(self._qps[0])
        logger.info(
            "VERBS_TRANSPORT: QP[0] connected (role=%s remote_qpn=%d)",
            self._role, remote_qpn,
        )

    def connect_all(
        self,
        remote_qpns: list[int],
        remote_psn: int,
        remote_gid: str,
        remote_lid: int = 0,
    ) -> None:
        """Connect all QPs to the peer, pairing by index.

        Each local QP[i] is connected to ``remote_qpns[i]``.  All QPs
        share the same ``remote_psn``, ``remote_gid``, and ``remote_lid``
        (same physical peer, different QP numbers).

        Args:
            remote_qpns: List of peer QP numbers, one per local QP slot.
                Length must equal ``self._qp_count``.
            remote_psn: Peer packet sequence number (shared across QPs).
            remote_gid: Peer GID as a 32-char hex string.
            remote_lid: Peer LID (0 for RoCE).

        Raises:
            ValueError: If ``len(remote_qpns) != self._qp_count``.
        """
        if len(remote_qpns) != self._qp_count:
            raise ValueError(
                f"remote_qpns length {len(remote_qpns)} != "
                f"qp_count {self._qp_count}"
            )
        for i, remote_qpn in enumerate(remote_qpns):
            self._modify_to_rtr(
                self._qps[i], remote_qpn, remote_psn, remote_gid, remote_lid
            )
            self._modify_to_rts(self._qps[i])
            logger.info(
                "VERBS_TRANSPORT: QP[%d] connected (role=%s remote_qpn=%d)",
                i, self._role, remote_qpn,
            )

    # ------------------------------------------------------------------
    # QP state machine helpers
    # ------------------------------------------------------------------

    def _modify_to_init(self, qp: "QP") -> None:
        access = IBV_ACCESS_LOCAL_WRITE
        if self._role == "initiator":
            access |= IBV_ACCESS_REMOTE_READ | IBV_ACCESS_REMOTE_WRITE

        attr = QPAttr()
        attr.qp_state = IBV_QPS_INIT
        attr.pkey_index = 0
        attr.port_num = self._port
        attr.qp_access_flags = access

        mask = IBV_QP_STATE | IBV_QP_PKEY_INDEX | IBV_QP_PORT | IBV_QP_ACCESS_FLAGS
        qp.modify(attr, mask)

    def _modify_to_rtr(
        self,
        qp: "QP",
        remote_qpn: int,
        remote_psn: int,
        remote_gid: str,
        remote_lid: int,
    ) -> None:
        gr = GlobalRoute(
            dgid=_hex_to_pyverbs_gid(remote_gid or "0" * 32),
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
        attr.path_mtu = self._path_mtu
        attr.dest_qp_num = remote_qpn
        attr.rq_psn = remote_psn
        attr.max_dest_rd_atomic = 4
        attr.min_rnr_timer = 12
        attr.ah_attr = ah_attr

        mask = (
            IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU | IBV_QP_DEST_QPN
            | IBV_QP_RQ_PSN | IBV_QP_MAX_DEST_RD_ATOMIC | IBV_QP_MIN_RNR_TIMER
        )
        qp.modify(attr, mask)

    def _modify_to_rts(self, qp: "QP") -> None:
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
        qp.modify(attr, mask)

    # ------------------------------------------------------------------
    # Memory registration
    # ------------------------------------------------------------------

    def register_mr(self, buffer_ptr: int, length: int) -> MrInfo:
        """Register a memory region for RDMA access.

        Args:
            buffer_ptr: Virtual address of the buffer to register.
            length: Length of the buffer in bytes.

        Returns:
            An ``MrInfo`` with rkey, addr, length, handle, and deregister
            callback.
        """
        access = IBV_ACCESS_LOCAL_WRITE
        if self._role == "initiator":
            access |= IBV_ACCESS_REMOTE_READ | IBV_ACCESS_REMOTE_WRITE
        mr_obj = MR(self._pd, length, access, address=buffer_ptr)
        mr_info = MrInfo(
            rkey=mr_obj.rkey,
            addr=buffer_ptr,
            length=length,
            handle=mr_obj,
            deregister=lambda: self._do_deregister(mr_obj),
        )
        return mr_info

    def _do_deregister(self, mr_obj: "MR") -> None:
        try:
            mr_obj.close()
        except Exception as e:
            logger.warning("MR deregister failed (may leak): %s", e)

    def deregister_mr(self, mr: MrInfo) -> None:
        """Deregister a previously registered memory region.

        Args:
            mr: The ``MrInfo`` returned by ``register_mr``.
        """
        if mr.deregister is not None:
            mr.deregister()
        elif mr.handle is not None:
            mr.handle.close()

    # ------------------------------------------------------------------
    # Work requests
    # ------------------------------------------------------------------

    def post_read(
        self,
        local_buf: RegisteredBuffer,
        remote_addr: int,
        rkey: int,
        length: int,
    ) -> RdmaFuture:
        """Post an RDMA Read work request (target role only).

        Selects the next QP slot via round-robin, acquires its lock, posts
        the Read, and returns a future.  The per-slot lock is held until
        ``poll_completion`` (or ``drain_on_timeout`` on timeout) releases
        it — no further post is allowed on that slot until then.

        Args:
            local_buf: Registered local buffer to receive the read data.
            remote_addr: Virtual address of the source MR on the peer.
            rkey: Remote key authorizing access to the source MR.
            length: Number of bytes to read.

        Returns:
            An ``RdmaFuture`` that completes once the Read is acknowledged.

        Raises:
            RuntimeError: If called on the initiator role, or if the
                transport is closed or drained.
        """
        if self._role != "target":
            raise RuntimeError("post_read only valid for target role")

        with self._stripe_lock:
            slot = self._next_qp % self._qp_count
            self._next_qp += 1

        self._qp_locks[slot].acquire()
        try:
            if self._closed or self._drained:
                raise RuntimeError("transport is closed or drained")

            future = RdmaFuture()
            wr_id = self._next_wr_ids[slot]
            self._next_wr_ids[slot] += 1
            self._inflight_wr_ids[slot] = wr_id
            self._inflight_futures[slot] = future

            sge = SGE(
                addr=local_buf.addr,
                length=length,
                lkey=local_buf.mr.handle.lkey,
            )
            wr = SendWR(wr_id=wr_id, opcode=IBV_WR_RDMA_READ, num_sge=1, sg=[sge])
            wr.set_wr_rdma(rkey=rkey, addr=remote_addr)
            self._qps[slot].post_send(wr)
            return future
        except BaseException:
            self._inflight_futures[slot] = None
            self._qp_locks[slot].release()
            raise

    def post_write(
        self,
        local_buf: RegisteredBuffer,
        remote_addr: int,
        rkey: int,
        length: int,
    ) -> RdmaFuture:
        """Post an RDMA Write work request, pushing local data to the peer.

        Valid for the target role only: the target pushes bytes from
        ``local_buf`` into the initiator's registered DRAM at
        ``remote_addr``.  Selects the next QP slot via round-robin and
        acquires its lock, which is held until ``poll_completion`` (or
        ``drain_on_timeout`` on a timeout) releases it.

        Args:
            local_buf: Registered local buffer holding the source bytes.
            remote_addr: Virtual address of the destination MR on the peer.
            rkey: Remote key authorizing access to the destination MR.
            length: Number of bytes to write.

        Returns:
            An ``RdmaFuture`` that completes once the Write is acknowledged.

        Raises:
            RuntimeError: If called on the initiator role, or if the
                transport is closed or drained.
        """
        if self._role != "target":
            raise RuntimeError("post_write only valid for target role")

        with self._stripe_lock:
            slot = self._next_qp % self._qp_count
            self._next_qp += 1

        self._qp_locks[slot].acquire()
        try:
            if self._closed or self._drained:
                raise RuntimeError("transport is closed or drained")

            future = RdmaFuture()
            wr_id = self._next_wr_ids[slot]
            self._next_wr_ids[slot] += 1
            self._inflight_wr_ids[slot] = wr_id
            self._inflight_futures[slot] = future

            sge = SGE(
                addr=local_buf.addr,
                length=length,
                lkey=local_buf.mr.handle.lkey,
            )
            wr = SendWR(wr_id=wr_id, opcode=IBV_WR_RDMA_WRITE, num_sge=1, sg=[sge])
            wr.set_wr_rdma(rkey=rkey, addr=remote_addr)
            self._qps[slot].post_send(wr)
            return future
        except BaseException:
            self._inflight_futures[slot] = None
            self._qp_locks[slot].release()
            raise

    # ------------------------------------------------------------------
    # Completion
    # ------------------------------------------------------------------

    def poll_completion(self, future: RdmaFuture, timeout_ms: int = 5000) -> bool:
        """Busy-poll the CQ for the inflight WR's completion.

        Identifies the QP slot that owns ``future`` by scanning
        ``_inflight_futures``, then busy-polls the corresponding CQ.

        Lock release semantics (per slot):
        - On CQE (success or error status): releases the slot's lock.
        - On timeout (returns False): lock stays held — caller MUST call
          ``drain_on_timeout()`` which releases it.
        - On unexpected exception: releases the slot's lock, marks
          transport drained.

        Args:
            future: The ``RdmaFuture`` returned by the most recent
                ``post_read`` or ``post_write``.
            timeout_ms: Poll deadline in milliseconds (default 5000).

        Returns:
            ``True`` on success CQE, ``False`` on error CQE or timeout.

        Raises:
            RuntimeError: If ``future`` is not the current inflight future
                on any slot.
        """
        # Locate the slot that owns this future
        slot: int | None = None
        for i, f in enumerate(self._inflight_futures):
            if f is future:
                slot = i
                break

        if slot is None:
            # future is not inflight on any slot — clear the first occupied
            # slot (there should be exactly one since this is a programming
            # error path) and release its lock.
            for i, f in enumerate(self._inflight_futures):
                if f is not None:
                    self._inflight_futures[i] = None
                    self._qp_locks[i].release()
                    break
            raise RuntimeError("poll_completion called with non-inflight future")

        try:
            deadline = time.monotonic() + timeout_ms / 1000.0
            while time.monotonic() < deadline:
                npolled, wcs = self._cqs[slot].poll(num_entries=1)
                for wc in wcs[:npolled]:
                    if wc.wr_id != self._inflight_wr_ids[slot]:
                        logger.warning(
                            "CQE wr_id=%d != expected %d (skipped)",
                            wc.wr_id, self._inflight_wr_ids[slot],
                        )
                        continue
                    success = wc.status == IBV_WC_SUCCESS
                    self._inflight_futures[slot].set_complete(success=success)
                    self._inflight_futures[slot] = None
                    self._qp_locks[slot].release()
                    return success
            # Timeout — lock intentionally stays held for drain_on_timeout()
            return False
        except BaseException:
            self._drained = True
            self._inflight_futures[slot] = None
            self._qp_locks[slot].release()
            raise

    def drain_on_timeout(self) -> bool:
        """Move the timed-out QP to ERROR and drain its CQ.

        Identifies the slot with a non-``None`` inflight future (set by the
        preceding ``post_read`` / ``post_write`` that timed out), moves
        that QP to ``IBV_QPS_ERR`` to flush pending WRs, then polls until
        the inflight WR's flush CQE appears or a 2 s deadline expires.
        Releases the slot's lock before returning.

        Returns:
            ``True`` if the inflight WR's flush CQE was observed (buffer is
            safe to free).  ``False`` if the 2 s drain deadline expired
            without seeing the flush CQE (buffer must be quarantined).
        """
        # Find the slot whose future is still in-flight after a timeout
        slot: int | None = None
        for i, f in enumerate(self._inflight_futures):
            if f is not None:
                slot = i
                break

        if slot is None:
            # Nothing in flight — nothing to drain
            return True

        try:
            attr = QPAttr()
            attr.qp_state = IBV_QPS_ERR
            self._qps[slot].modify(attr, IBV_QP_STATE)

            deadline = time.monotonic() + 2.0
            flushed = False
            while time.monotonic() < deadline:
                npolled, wcs = self._cqs[slot].poll(num_entries=16)
                for wc in wcs[:npolled]:
                    if wc.wr_id == self._inflight_wr_ids[slot]:
                        flushed = True
                        if self._inflight_futures[slot] is not None:
                            self._inflight_futures[slot].set_complete(
                                success=False
                            )
                            self._inflight_futures[slot] = None
                if flushed:
                    break
                if not npolled:
                    time.sleep(0.001)

            self._drained = True
            if not flushed:
                logger.error(
                    "drain_on_timeout: WR wr_id=%d not flushed after 2s",
                    self._inflight_wr_ids[slot],
                )
            return flushed
        finally:
            self._qp_locks[slot].release()

    # ------------------------------------------------------------------
    # Buffer management
    # ------------------------------------------------------------------

    def allocate_buffer(self, length: int) -> RegisteredBuffer:
        """Allocate page-aligned mmap buffer registered for RDMA.

        Args:
            length: Requested buffer size in bytes (rounded up to 4 KiB).

        Returns:
            A ``RegisteredBuffer`` backed by an anonymous mmap.
        """
        aligned = (length + 4095) & ~4095
        backing = mmap.mmap(-1, aligned)
        addr = ctypes.addressof(ctypes.c_char.from_buffer(backing))
        mr = self.register_mr(addr, aligned)
        buf = RegisteredBuffer(addr=addr, length=aligned, mr=mr, backing=backing)
        self._allocated_buffers.add(buf)
        return buf

    def release_buffer_tracking(self, buf: RegisteredBuffer) -> None:
        """Transfer buffer ownership from transport to caller.

        Args:
            buf: Buffer to remove from the transport's tracking set.
        """
        self._allocated_buffers.discard(buf)

    def free_buffer(self, buf: RegisteredBuffer) -> None:
        """Deregister MR and unmap backing memory.

        Args:
            buf: Buffer previously allocated via ``allocate_buffer``.
        """
        self._allocated_buffers.discard(buf)
        if not self._closed:
            self.deregister_mr(buf.mr)
        if buf.backing is not None:
            buf.backing.close()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Tear down all resources.

        Acquires all per-QP locks before tearing down to ensure no
        operation is in-flight at teardown.  Idempotent.
        """
        if not hasattr(self, "_qp_locks"):
            return
        for lock in self._qp_locks:
            lock.acquire()
        try:
            if self._closed:
                return
            self._closed = True
            for buf in list(self._allocated_buffers):
                self.free_buffer(buf)
            self._allocated_buffers.clear()
            for i in range(self._qp_count):
                if self._qps[i]:
                    self._qps[i].close()
                    self._qps[i] = None  # type: ignore[assignment]
                if self._cqs[i]:
                    self._cqs[i].close()
                    self._cqs[i] = None  # type: ignore[assignment]
            if self._pd:
                self._pd.close()
                self._pd = None
            if self._ctx:
                self._ctx.close()
                self._ctx = None
        finally:
            for lock in self._qp_locks:
                lock.release()
        self._cleanup_endpoint_file()

    def _cleanup_endpoint_file(self) -> None:
        if self._endpoint_file is not None:
            try:
                self._endpoint_file.unlink(missing_ok=True)
            except OSError:
                pass

    def __del__(self) -> None:
        # __init__ can raise before _qp_locks is populated (missing pyverbs,
        # invalid role, qp_count < 1) — check for the list before calling
        # close() which iterates it.
        if hasattr(self, "_qp_locks") and self._qp_locks:
            self.close()
