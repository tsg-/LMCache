# SPDX-License-Identifier: Apache-2.0
"""Fault-test liburing calls while using real files, SQEs and kernel CQEs."""

# Standard
from pathlib import Path
import os
import subprocess
import sys

# Third Party
import pytest


@pytest.mark.parametrize("failure", ["WAIT", "SUBMIT", "SUBMIT_AFTER_FIRST"])
def test_persistent_engine_failure_retires_work_and_poisons_worker(
    tmp_path: Path, failure: str
) -> None:
    """No failed future may release live buffers or reuse a damaged ring."""
    root = Path(__file__).resolve().parents[1]
    build = root / "build-fault"
    assert build.is_dir(), "compile the LMCACHE_TEST_FAULTS build first"
    program = """
import mmap, os, select, sys, time
from pathlib import Path
from lmcache_fs_uring import LMCacheFSUringClient
p = Path(sys.argv[1])
keys = [f'test@00000000@0@{i:032x}' for i in range(56)]
for i in range(56):
    (p/f'test@0x00000000@0@{i:032x}.data').write_bytes(b'Q'*65536)
buffers = [mmap.mmap(-1, 65536) for _ in keys]
client = LMCacheFSUringClient(str(p), 1, '', True, 32)
def wait(task):
    deadline = time.monotonic()+10
    while time.monotonic()<deadline:
        ready = client.drain_completions()
        if ready:
            assert len(ready)==1 and ready[0][0]==task
            return ready[0]
        select.select([client.event_fd()], [], [], .01)
    raise AssertionError('engine failure lost its completion')
first = wait(client.submit_batch_get(keys, [memoryview(b) for b in buffers]))
assert not first[1], first
stats = client.io_stats()
assert stats['pending_reads']==0, stats
assert stats['submitted_reads']==stats['completed_reads'], stats
if os.environ.get('LMCACHE_TEST_SUBMIT_AFTER_FIRST_ERROR'):
    assert stats['submitted_reads'] == 32, stats
os.fstat(0)
second = wait(client.submit_batch_get(keys, [memoryview(b) for b in buffers]))
assert not second[1] and 'poisoned' in second[2], second
client.close()
os.fstat(0)
for b in buffers: b.close()
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(build)
    environment[f"LMCACHE_TEST_{failure}_ERROR"] = "1"
    environment["LMCACHE_TEST_REINIT_ERROR"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", program, str(tmp_path)],
        env=environment,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
