# SPDX-License-Identifier: Apache-2.0
"""Public native issued-depth counters on a temporary file, without array I/O."""

# Standard
from pathlib import Path

# Third Party
import pytest

native = pytest.importorskip("lmcache_rust_raw_block_io")


def test_issued_stats_track_real_submissions_and_terminal_drain(tmp_path: Path) -> None:
    """A completed native read must produce a peak and drain issued/queued work."""
    path = tmp_path / "device"
    path.write_bytes(bytes(65536))
    device = native.RawBlockDevice(
        str(path), writable=True, io_engine="io_uring", iouring_queue_depth=8
    )
    try:
        assert device.io_stats()["issued_pending"] == 0
        buffers = [bytearray(4096) for _ in range(16)]
        batch = device.batched_read([i * 4096 for i in range(16)], buffers, [4096] * 16)
        assert device.wait_iouring(batch) == ([True] * 16, [])
        status = device.io_stats()
        assert status["issued_pending"] == status["outstanding_requests"] == 0
        assert 1 <= status["peak_issued_pending"] <= 16
    finally:
        device.close()
    assert device.io_stats()["issued_pending"] == 0
