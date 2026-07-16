#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Bench-scoped raw pyverbs helper for storage-owned CX7 baselines.

One binary drives both sides of a two-node RC-QP transfer:

    # storage side (poster; pulls with RDMA_READ, or pushes with RDMA_WRITE)
    python scripts/bench_verbs.py --role storage --direction read \\
        --device mlx5_1 --gid-index 3 --iterations 128 --bytes-per-iter 262144 \\
        --nonce bench01 --bootstrap-listen 0.0.0.0:9600

    # source side (producer for read, consumer for write)
    python scripts/bench_verbs.py --role source --direction read \\
        --device mlx5_1 --gid-index 3 --iterations 128 --bytes-per-iter 262144 \\
        --nonce bench01 --bootstrap-connect 192.168.200.4:9600

Both sides emit the transport-assertion evidence expected by
``scripts/bench_verify.py``:

  * ``TRANSPORT verbs rc_mlx5 device=<hca> qp_num=<n> gid_index=<n>``
  * ``TRANSPORT_MR flags=<name-set> direction=<read|write> role=<source|storage>
    rkey=<n> addr=0x<hex> length=<n>``
  * ``BENCH_RDMA_PRODUCER {...}`` on the producer side.
  * ``BENCH_RDMA_CONSUMER {...}`` on the consumer side.
  * ``BENCH_RDMA_DMA {"operation":..., "page_idx":..., "start_ns":...,
    "end_ns":..., "bytes":...}`` on the poster (storage), one line per iteration.

The poster is always the storage side, matching the storage-owned pull-model
milestone plan (M1 raw CX7 baselines). Direction is what storage does:

  * ``read``  — storage posts RDMA_READ pulling from source.  Source's MR
                needs REMOTE_READ + LOCAL_WRITE; storage's MR needs
                LOCAL_WRITE only.
  * ``write`` — storage posts RDMA_WRITE pushing into source.  Source's MR
                needs REMOTE_WRITE + LOCAL_WRITE; storage's MR needs
                LOCAL_WRITE only.

MR access flags are the direction minimum. The assertion log line reports the
resolved flag names so a mismatch is auditable after the fact.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import mmap
import random
import socket
import struct
import sys
import time
from dataclasses import dataclass
from typing import Iterable


BOOTSTRAP_TAG_QP = "qp_info"
BOOTSTRAP_TAG_READY = "ready"
BOOTSTRAP_TAG_POST_DONE = "post_done"
BOOTSTRAP_TAG_DIGEST = "digest"

_MAX_MESSAGE_BYTES = 1 << 16
_CONNECT_RETRY_INTERVAL_S = 0.2
_CONNECT_RETRY_DEADLINE_S = 30.0
_ACCEPT_DEADLINE_S = 30.0


def digest_algorithm() -> str:
    """Return the required digest algorithm for publishable raw verbs runs.

    M1's standalone validation record is fail-closed on BLAKE3; the general
    verifier's BLAKE2b fallback exists only for diagnostic records.
    """
    try:
        import blake3  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "blake3 is required for publishable raw-verbs validation"
        ) from exc
    return "blake3"


def digest_bytes(payload: bytes) -> str:
    """Return the BLAKE3 hex digest of ``payload``."""
    try:
        import blake3
    except ImportError as exc:
        raise RuntimeError(
            "blake3 is required for publishable raw-verbs validation"
        ) from exc
    return blake3.blake3(payload).hexdigest()


def deterministic_pattern(nonce: str, role: str, length: int) -> bytes:
    """Return a deterministic byte pattern seeded by ``nonce`` and ``role``.

    Producers use this to fill their buffer so the receiving side can verify
    the digest offline without any post-run channel other than the log
    records.
    """
    seed = hashlib.blake2b(f"{nonce}:{role}".encode(), digest_size=8).digest()
    rng = random.Random(int.from_bytes(seed, "big"))
    chunk = bytes(rng.getrandbits(8) for _ in range(4096))
    if length <= len(chunk):
        return chunk[:length]
    repeats = (length + len(chunk) - 1) // len(chunk)
    return (chunk * repeats)[:length]


@dataclass(frozen=True)
class MrFlagPlan:
    """Direction-minimum MR access-flag plan for one endpoint.

    Attributes:
        mask: The raw ``IBV_ACCESS_*`` bitmask to hand to ``ibv_reg_mr``.
        names: Sorted human-readable flag names for evidence logging.
    """

    mask: int
    names: tuple[str, ...]


def mr_flags_for(
    role: str,
    direction: str,
    flag_values: dict[str, int],
) -> MrFlagPlan:
    """Compute direction-minimum MR access flags for one endpoint.

    Args:
        role: ``source`` or ``storage``.
        direction: ``read`` (storage posts RDMA_READ) or ``write`` (storage
            posts RDMA_WRITE).
        flag_values: Mapping from ``IBV_ACCESS_LOCAL_WRITE``,
            ``IBV_ACCESS_REMOTE_READ``, and ``IBV_ACCESS_REMOTE_WRITE`` names
            to their numeric values on this rdma-core build.

    Returns:
        The flag plan for this endpoint.  Storage always uses LOCAL_WRITE
        only; source adds the single remote flag matching ``direction``.
    """
    if role not in ("source", "storage"):
        raise ValueError(f"role must be source or storage, got {role!r}")
    if direction not in ("read", "write"):
        raise ValueError(f"direction must be read or write, got {direction!r}")
    names: list[str] = ["IBV_ACCESS_LOCAL_WRITE"]
    if role == "source":
        names.append(
            "IBV_ACCESS_REMOTE_READ"
            if direction == "read"
            else "IBV_ACCESS_REMOTE_WRITE"
        )
    mask = 0
    for name in names:
        mask |= flag_values[name]
    return MrFlagPlan(mask=mask, names=tuple(sorted(names)))


def qp_access_flags_for(
    role: str,
    direction: str,
    flag_values: dict[str, int],
) -> int:
    """Compute the QP-level access permit for one endpoint.

    Kept in sync with :func:`mr_flags_for` so QP INIT permissions never
    exceed the MR-level permits emitted in the assertion log line.
    """
    return mr_flags_for(role, direction, flag_values).mask


class BootstrapChannel:
    """Length-prefixed JSON exchange over a single TCP connection.

    Each frame is a 4-byte network-order unsigned integer body length followed
    by the UTF-8 JSON body.  A ``{"tag": ..., "payload": ...}`` envelope is
    used so unrelated frames cannot be silently misinterpreted.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock

    @classmethod
    def listen(cls, host: str, port: int) -> "BootstrapChannel":
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen(1)
        server.settimeout(_ACCEPT_DEADLINE_S)
        try:
            sock, _peer = server.accept()
        finally:
            server.close()
        sock.settimeout(None)
        return cls(sock)

    @classmethod
    def connect(cls, host: str, port: int) -> "BootstrapChannel":
        deadline = time.monotonic() + _CONNECT_RETRY_DEADLINE_S
        last_exc: OSError | None = None
        while time.monotonic() < deadline:
            try:
                sock = socket.create_connection((host, port), timeout=5.0)
                sock.settimeout(None)
                return cls(sock)
            except OSError as exc:
                last_exc = exc
                time.sleep(_CONNECT_RETRY_INTERVAL_S)
        raise TimeoutError(
            f"bootstrap connect to {host}:{port} timed out after "
            f"{_CONNECT_RETRY_DEADLINE_S:.1f}s: {last_exc}"
        )

    def send(self, tag: str, payload: dict[str, object]) -> None:
        body = json.dumps({"tag": tag, "payload": payload}).encode("utf-8")
        if len(body) > _MAX_MESSAGE_BYTES:
            raise ValueError(
                f"bootstrap frame too large: {len(body)} bytes exceeds "
                f"{_MAX_MESSAGE_BYTES}"
            )
        self._sock.sendall(struct.pack("!I", len(body)) + body)

    def recv(self, expected_tag: str) -> dict[str, object]:
        header = self._recv_exact(4)
        (length,) = struct.unpack("!I", header)
        if length == 0 or length > _MAX_MESSAGE_BYTES:
            raise ValueError(f"bootstrap frame length out of range: {length}")
        body = self._recv_exact(length)
        message = json.loads(body.decode("utf-8"))
        tag = message.get("tag")
        if tag != expected_tag:
            raise RuntimeError(
                f"bootstrap tag mismatch: expected {expected_tag!r}, got {tag!r}"
            )
        payload = message.get("payload")
        if not isinstance(payload, dict):
            raise RuntimeError("bootstrap payload must be an object")
        return payload

    def _recv_exact(self, n: int) -> bytes:
        chunks: list[bytes] = []
        remaining = n
        while remaining > 0:
            chunk = self._sock.recv(remaining)
            if not chunk:
                raise ConnectionError(
                    "bootstrap peer closed before expected bytes arrived"
                )
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        try:
            self._sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self._sock.close()


def _hex_to_gid(hex_str: str, gid_cls: type) -> object:
    if len(hex_str) != 32:
        raise ValueError(f"GID must be 32 hex chars, got {len(hex_str)}: {hex_str!r}")
    raw = bytes.fromhex(hex_str)
    return gid_cls(":".join(f"{raw[i]:02x}{raw[i + 1]:02x}" for i in range(0, 16, 2)))


def _gid_to_hex(gid_value: object) -> str:
    if isinstance(gid_value, (bytes, bytearray)):
        return bytes(gid_value).hex()
    return str(gid_value).replace(":", "")


class VerbsEndpoint:
    """One side of a storage-owned CX7 baseline transfer.

    Encapsulates the pyverbs context, PD, CQ, RC QP, and single MR-backed
    mmap buffer used by the bench.  The ``role`` and ``direction`` decide
    which side posts and what MR access flags are minimum-sufficient.
    """

    def __init__(
        self,
        role: str,
        direction: str,
        device: str,
        port: int,
        gid_index: int,
        iterations: int,
        bytes_per_iter: int,
        qd: int,
        nonce: str,
    ) -> None:
        if role not in ("source", "storage"):
            raise ValueError(f"role must be source or storage, got {role!r}")
        if direction not in ("read", "write"):
            raise ValueError(f"direction must be read or write, got {direction!r}")
        if iterations <= 0 or bytes_per_iter <= 0 or qd <= 0:
            raise ValueError("iterations, bytes-per-iter, and qd must be positive")

        from pyverbs.addr import AHAttr, GID, GlobalRoute
        from pyverbs.cq import CQ
        from pyverbs.device import Context
        from pyverbs.enums import (
            IBV_ACCESS_LOCAL_WRITE,
            IBV_ACCESS_REMOTE_READ,
            IBV_ACCESS_REMOTE_WRITE,
            IBV_QP_ACCESS_FLAGS,
            IBV_QP_AV,
            IBV_QP_DEST_QPN,
            IBV_QP_MAX_DEST_RD_ATOMIC,
            IBV_QP_MAX_QP_RD_ATOMIC,
            IBV_QP_MIN_RNR_TIMER,
            IBV_QP_PATH_MTU,
            IBV_QP_PKEY_INDEX,
            IBV_QP_PORT,
            IBV_QP_RETRY_CNT,
            IBV_QP_RNR_RETRY,
            IBV_QP_RQ_PSN,
            IBV_QP_SQ_PSN,
            IBV_QP_STATE,
            IBV_QP_TIMEOUT,
            IBV_QPS_INIT,
            IBV_QPS_RTR,
            IBV_QPS_RTS,
            IBV_QPT_RC,
            IBV_WC_SUCCESS,
            IBV_WR_RDMA_READ,
            IBV_WR_RDMA_WRITE,
        )
        from pyverbs.mr import MR
        from pyverbs.pd import PD
        from pyverbs.qp import QP, QPAttr, QPCap, QPInitAttr
        from pyverbs.wr import SGE, SendWR

        self._pv_ahattr = AHAttr
        self._pv_gid = GID
        self._pv_globalroute = GlobalRoute
        self._pv_mr = MR
        self._pv_qpattr = QPAttr
        self._pv_sge = SGE
        self._pv_sendwr = SendWR
        self._flag_values = {
            "IBV_ACCESS_LOCAL_WRITE": IBV_ACCESS_LOCAL_WRITE,
            "IBV_ACCESS_REMOTE_READ": IBV_ACCESS_REMOTE_READ,
            "IBV_ACCESS_REMOTE_WRITE": IBV_ACCESS_REMOTE_WRITE,
        }
        self._qp_state_masks = {
            "init": IBV_QP_STATE
            | IBV_QP_PKEY_INDEX
            | IBV_QP_PORT
            | IBV_QP_ACCESS_FLAGS,
            "rtr": IBV_QP_STATE
            | IBV_QP_AV
            | IBV_QP_PATH_MTU
            | IBV_QP_DEST_QPN
            | IBV_QP_RQ_PSN
            | IBV_QP_MAX_DEST_RD_ATOMIC
            | IBV_QP_MIN_RNR_TIMER,
            "rts": IBV_QP_STATE
            | IBV_QP_SQ_PSN
            | IBV_QP_MAX_QP_RD_ATOMIC
            | IBV_QP_RETRY_CNT
            | IBV_QP_RNR_RETRY
            | IBV_QP_TIMEOUT,
        }
        self._qp_states = {
            "init": IBV_QPS_INIT,
            "rtr": IBV_QPS_RTR,
            "rts": IBV_QPS_RTS,
        }
        self._qp_type_rc = IBV_QPT_RC
        self._wc_success = IBV_WC_SUCCESS
        self._read_opcode = IBV_WR_RDMA_READ
        self._write_opcode = IBV_WR_RDMA_WRITE

        self._role = role
        self._direction = direction
        self._device = device
        self._port = port
        self._gid_index = gid_index
        self._iterations = iterations
        self._bytes_per_iter = bytes_per_iter
        self._qd = qd
        self._nonce = nonce
        self._local_psn = random.randint(0, 0xFFFFFF)

        self._is_poster = role == "storage"
        self._is_producer = (direction == "read" and role == "source") or (
            direction == "write" and role == "storage"
        )

        total_bytes = iterations * bytes_per_iter
        self._buffer = mmap.mmap(-1, total_bytes)
        self._addr = ctypes.addressof(ctypes.c_char.from_buffer(self._buffer))
        self._length = total_bytes

        if self._is_producer:
            pattern = deterministic_pattern(nonce, role, total_bytes)
            self._buffer[:] = pattern

        self._ctx = Context(name=device)
        self._pd = PD(self._ctx)
        self._cq = CQ(self._ctx, cqe=max(16, 2 * qd))

        self._flag_plan = mr_flags_for(role, direction, self._flag_values)
        self._mr = MR(self._pd, total_bytes, self._flag_plan.mask, address=self._addr)

        qp_access = qp_access_flags_for(role, direction, self._flag_values)
        cap = QPCap(
            max_send_wr=max(64, 2 * qd),
            max_recv_wr=1,
            max_send_sge=1,
            max_recv_sge=1,
        )
        init_attr = QPInitAttr(
            qp_type=self._qp_type_rc,
            scq=self._cq,
            rcq=self._cq,
            cap=cap,
            sq_sig_all=True,
        )
        self._qp = QP(self._pd, init_attr)
        self._modify_to_init(qp_access)

        port_attr = self._ctx.query_port(port)
        self._local_lid = port_attr.lid
        self._path_mtu = port_attr.active_mtu
        self._local_gid = _gid_to_hex(self._ctx.query_gid(port, gid_index).gid)
        self._local_qpn = self._qp.qp_num
        self._closed = False

    def _modify_to_init(self, qp_access: int) -> None:
        attr = self._pv_qpattr()
        attr.qp_state = self._qp_states["init"]
        attr.pkey_index = 0
        attr.port_num = self._port
        attr.qp_access_flags = qp_access
        self._qp.modify(attr, self._qp_state_masks["init"])

    def _modify_to_rtr(
        self, remote_qpn: int, remote_psn: int, remote_gid: str, remote_lid: int
    ) -> None:
        gr = self._pv_globalroute(
            dgid=_hex_to_gid(remote_gid, self._pv_gid),
            sgid_index=self._gid_index,
            hop_limit=64,
        )
        ah_attr = self._pv_ahattr(
            dlid=remote_lid,
            sl=0,
            src_path_bits=0,
            port_num=self._port,
            is_global=1,
            gr=gr,
        )
        attr = self._pv_qpattr()
        attr.qp_state = self._qp_states["rtr"]
        attr.path_mtu = self._path_mtu
        attr.dest_qp_num = remote_qpn
        attr.rq_psn = remote_psn
        attr.max_dest_rd_atomic = 4
        attr.min_rnr_timer = 12
        attr.ah_attr = ah_attr
        self._qp.modify(attr, self._qp_state_masks["rtr"])

    def _modify_to_rts(self) -> None:
        attr = self._pv_qpattr()
        attr.qp_state = self._qp_states["rts"]
        attr.sq_psn = self._local_psn
        attr.max_rd_atomic = 4
        attr.retry_cnt = 7
        attr.rnr_retry = 7
        attr.timeout = 14
        self._qp.modify(attr, self._qp_state_masks["rts"])

    def connect(self, peer: dict[str, object]) -> None:
        self._modify_to_rtr(
            remote_qpn=int(peer["qpn"]),
            remote_psn=int(peer["psn"]),
            remote_gid=str(peer["gid"]),
            remote_lid=int(peer["lid"]),
        )
        self._modify_to_rts()

    def local_qp_info(self) -> dict[str, object]:
        return {
            "role": self._role,
            "direction": self._direction,
            "qpn": self._local_qpn,
            "psn": self._local_psn,
            "gid": self._local_gid,
            "lid": self._local_lid,
            "addr": self._addr,
            "rkey": self._mr.rkey,
            "length": self._length,
            "bytes_per_iter": self._bytes_per_iter,
            "iterations": self._iterations,
            "qd": self._qd,
            "nonce": self._nonce,
        }

    def transport_line(self) -> str:
        return (
            f"TRANSPORT verbs rc_mlx5 device={self._device} "
            f"qp_num={self._local_qpn} gid_index={self._gid_index}"
        )

    def mr_evidence_line(self) -> str:
        return (
            f"TRANSPORT_MR flags={'|'.join(self._flag_plan.names)} "
            f"direction={self._direction} role={self._role} "
            f"rkey={self._mr.rkey} addr=0x{self._addr:x} length={self._length}"
        )

    def post_transfers(
        self, peer: dict[str, object], emit_dma: Iterable = sys.stdout
    ) -> None:
        """Post transfers with at most ``qd`` requests outstanding.

        Only meaningful on the poster side.  Emits one structured
        ``BENCH_RDMA_DMA`` record per completion so ``bench_verify`` can
        recover per-page DMA timing.
        """
        if not self._is_poster:
            raise RuntimeError("post_transfers may only be called on the poster side")
        remote_base = int(peer["addr"])
        remote_rkey = int(peer["rkey"])
        opcode = self._read_opcode if self._direction == "read" else self._write_opcode
        op_name = self._direction

        pending: dict[int, tuple[int, int]] = {}
        next_page = 0
        deadline = time.monotonic() + 30.0
        while next_page < self._iterations or pending:
            while next_page < self._iterations and len(pending) < self._qd:
                offset = next_page * self._bytes_per_iter
                sge = self._pv_sge(
                    addr=self._addr + offset,
                    length=self._bytes_per_iter,
                    lkey=self._mr.lkey,
                )
                wr_id = next_page + 1
                wr = self._pv_sendwr(
                    wr_id=wr_id, opcode=opcode, num_sge=1, sg=[sge]
                )
                wr.set_wr_rdma(rkey=remote_rkey, addr=remote_base + offset)
                posted_ns = time.monotonic_ns()
                self._qp.post_send(wr)
                pending[wr_id] = (next_page, posted_ns)
                next_page += 1

            if time.monotonic() >= deadline:
                raise TimeoutError("RDMA completions did not arrive within 30s")
            npolled, wcs = self._cq.poll(num_entries=self._qd)
            if npolled:
                deadline = time.monotonic() + 30.0
            for wc in wcs[:npolled]:
                if wc.status != self._wc_success:
                    raise RuntimeError(
                        f"WR wr_id={wc.wr_id} failed with status={wc.status}"
                    )
                completion = pending.pop(wc.wr_id, None)
                if completion is None:
                    raise RuntimeError(
                        f"CQE wr_id={wc.wr_id} has no outstanding work request"
                    )
                page_idx, posted_ns = completion
                completed_ns = time.monotonic_ns()
                record = {
                    "operation": op_name,
                    "page_idx": page_idx,
                    "start_ns": posted_ns,
                    "end_ns": completed_ns,
                    "posted_ns": posted_ns,
                    "completed_ns": completed_ns,
                    "bytes": self._bytes_per_iter,
                }
                print(
                    f"BENCH_RDMA_DMA {json.dumps(record, sort_keys=True)}",
                    file=emit_dma,
                )

    def payload_digest(self) -> str:
        return digest_bytes(bytes(self._buffer[: self._length]))

    def digest_record(self, kind: str) -> dict[str, object]:
        return {
            "role": self._role,
            "direction": self._direction,
            "kind": kind,
            "iterations": self._iterations,
            "bytes_per_iter": self._bytes_per_iter,
            "bytes_total": self._length,
            "digest_algorithm": digest_algorithm(),
            "digest": self.payload_digest(),
            "nonce": self._nonce,
        }

    def is_producer(self) -> bool:
        return self._is_producer

    def is_poster(self) -> bool:
        return self._is_poster

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for attr in ("_qp", "_cq", "_mr", "_pd", "_ctx"):
            handle = getattr(self, attr, None)
            if handle is None:
                continue
            try:
                handle.close()
            except Exception:
                pass
            setattr(self, attr, None)
        try:
            self._buffer.close()
        except (BufferError, ValueError):
            pass


def _parse_hostport(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port:
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {value!r}")
    return host, int(port)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("source", "storage"), required=True)
    parser.add_argument("--direction", choices=("read", "write"), required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--port", type=int, default=1)
    parser.add_argument("--gid-index", type=int, required=True)
    parser.add_argument("--iterations", type=int, required=True)
    parser.add_argument("--bytes-per-iter", type=int, required=True)
    parser.add_argument("--qd", type=int, default=1)
    parser.add_argument("--nonce", required=True)
    listen_group = parser.add_mutually_exclusive_group(required=True)
    listen_group.add_argument("--bootstrap-listen", type=_parse_hostport)
    listen_group.add_argument("--bootstrap-connect", type=_parse_hostport)
    return parser.parse_args(argv)


def run(
    role: str,
    direction: str,
    device: str,
    port: int,
    gid_index: int,
    iterations: int,
    bytes_per_iter: int,
    qd: int,
    nonce: str,
    channel: BootstrapChannel,
    output: Iterable = sys.stdout,
) -> int:
    """Drive one side of a two-node bench run.

    Returns 0 on success. Any exception propagates after cleanup so callers
    can surface a nonzero exit status. Exposed for the loopback tests.
    """
    endpoint = VerbsEndpoint(
        role=role,
        direction=direction,
        device=device,
        port=port,
        gid_index=gid_index,
        iterations=iterations,
        bytes_per_iter=bytes_per_iter,
        qd=qd,
        nonce=nonce,
    )
    try:
        print(endpoint.transport_line(), file=output)
        print(endpoint.mr_evidence_line(), file=output)

        control_start_ns = time.monotonic_ns()
        channel.send(BOOTSTRAP_TAG_QP, endpoint.local_qp_info())
        peer = channel.recv(BOOTSTRAP_TAG_QP)

        if peer.get("nonce") != nonce:
            raise RuntimeError(
                f"peer nonce {peer.get('nonce')!r} does not match {nonce!r}"
            )
        if int(peer.get("bytes_per_iter", 0)) != bytes_per_iter:
            raise RuntimeError("peer bytes-per-iter must match local value")
        if int(peer.get("iterations", 0)) != iterations:
            raise RuntimeError("peer iterations must match local value")

        endpoint.connect(peer)
        channel.send(BOOTSTRAP_TAG_READY, {"role": role})
        channel.recv(BOOTSTRAP_TAG_READY)
        control_end_ns = time.monotonic_ns()

        if endpoint.is_poster():
            control_record = {
                "phase": "qp_setup",
                "role": role,
                "start_ns": control_start_ns,
                "end_ns": control_end_ns,
            }
            print(
                f"BENCH_RDMA_CONTROL {json.dumps(control_record, sort_keys=True)}",
                file=output,
            )

        if endpoint.is_producer():
            producer_record = endpoint.digest_record("producer")
            print(
                f"BENCH_RDMA_PRODUCER {json.dumps(producer_record, sort_keys=True)}",
                file=output,
            )

        if endpoint.is_poster():
            endpoint.post_transfers(peer, emit_dma=output)
            channel.send(BOOTSTRAP_TAG_POST_DONE, {"iterations": iterations})
            channel.recv(BOOTSTRAP_TAG_POST_DONE)
        else:
            channel.recv(BOOTSTRAP_TAG_POST_DONE)
            channel.send(BOOTSTRAP_TAG_POST_DONE, {"iterations": iterations})

        if not endpoint.is_producer():
            consumer_record = endpoint.digest_record("consumer")
            print(
                f"BENCH_RDMA_CONSUMER {json.dumps(consumer_record, sort_keys=True)}",
                file=output,
            )

        local_digest = endpoint.payload_digest()
        channel.send(BOOTSTRAP_TAG_DIGEST, {"digest": local_digest})
        peer_digest = channel.recv(BOOTSTRAP_TAG_DIGEST)
        if peer_digest.get("digest") != local_digest:
            raise RuntimeError(
                "digest mismatch between producer and consumer buffers "
                "after transfer; bench cannot be published"
            )
        return 0
    finally:
        endpoint.close()


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    if args.bootstrap_listen is not None:
        channel = BootstrapChannel.listen(*args.bootstrap_listen)
    else:
        channel = BootstrapChannel.connect(*args.bootstrap_connect)
    try:
        return run(
            role=args.role,
            direction=args.direction,
            device=args.device,
            port=args.port,
            gid_index=args.gid_index,
            iterations=args.iterations,
            bytes_per_iter=args.bytes_per_iter,
            qd=args.qd,
            nonce=args.nonce,
            channel=channel,
        )
    finally:
        channel.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
