# SPDX-License-Identifier: Apache-2.0
"""Failure-path tests for the fs_native io_uring read engine.

These need an extension built with LMCACHE_FS_FAULT_INJECTION=1 (CI builds it
with ``LMCACHE_FS_FAULT_INJECTION=1 bash scripts/ipu-poc/build_lmcache_fs.sh .``)
and skip otherwise.  Every case runs in a fresh interpreter, so the
LMCACHE_FS_FAULT environment variable and RLIMIT_NOFILE changes stay isolated.
"""

# Standard
from pathlib import Path
import os
import signal
import subprocess
import sys
import textwrap

# Third Party
import pytest


def _skip_without_fault_build() -> None:
    try:
        # First Party
        from lmcache import lmcache_fs
    except ImportError as exc:
        pytest.skip(f"native FS extension is not available: {exc}")
    if not (lmcache_fs.has_io_uring() and lmcache_fs.has_fault_injection()):
        pytest.skip("needs a LMCACHE_FS_FAULT_INJECTION=1 build with io_uring")
    if not os.path.isdir("/proc/self/fd"):
        pytest.skip("needs /proc/self/fd")


@pytest.fixture(autouse=True)
def _fault_build_only() -> None:
    _skip_without_fault_build()


# Common child-side helpers: a corpus of `n` objects of `size` bytes, stored
# with the pool engine, plus wait() and fd counting.
_PRELUDE = """
import os, resource, select, time
from lmcache.lmcache_fs import LMCacheFSClient
base = os.environ["FS_TEST_DIR"]
def fds():
    return len(os.listdir("/proc/self/fd"))
def wait(c, fid, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for comp in c.drain_completions():
            if comp[0] == fid:
                return comp
        select.select([c.event_fd()], [], [], 0.05)
    raise TimeoutError
def corpus(n, size, prefix="a"):
    srcs = [bytearray(os.urandom(size)) for _ in range(n)]
    keys = [f"test_model@00000000@0@{prefix}{i:015x}" for i in range(n)]
    w = LMCacheFSClient(base, 1)
    assert wait(w, w.submit_batch_set(keys, [memoryview(s) for s in srcs]))[1]
    w.close()
    return keys, srcs
"""


def _run(
    code: str,
    tmp_path: Path,
    fault: str | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run `code` after the prelude in a fresh interpreter."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)
    env["FS_TEST_DIR"] = str(tmp_path)
    env.pop("LMCACHE_FS_FAULT", None)
    env.pop("LMCACHE_FS_FAULT_DELAY_MS", None)
    if fault is not None:
        env["LMCACHE_FS_FAULT"] = fault
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, "-c", _PRELUDE + textwrap.dedent(code)],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.mark.parametrize("fault", ["cqe_short", "cqe_eintr", "submit_partial"])
def test_resubmitted_and_partial_reads_return_the_data(
    tmp_path: Path, fault: str
) -> None:
    """A short read, an -EINTR completion, or a kernel that takes one SQE per
    submit still delivers every key with matching bytes, and returns the
    byte budget."""
    child = _run(
        """
        n, size = 48, 64 << 10
        keys, srcs = corpus(n, size)
        dsts = [bytearray(size) for _ in range(n)]
        c = LMCacheFSClient(base, 1, read_engine="io_uring", io_depth=8)
        comp = wait(c, c.submit_batch_get(keys, [memoryview(d) for d in dsts]))
        print(comp[1], sum(comp[3]), all(d == s for d, s in zip(dsts, srcs)),
              c.read_bytes_in_flight())
        c.close()
        """,
        tmp_path,
        fault=fault,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.split() == ["True", "48", "True", "0"]


def test_submit_error_mid_batch_fails_keys_and_recovers(tmp_path: Path) -> None:
    """A failed io_uring_submit with requests accepted fails the unfinished
    keys of that batch after reaping them, recreates the ring so the next batch on the
    same client succeeds, leaks no descriptor, and returns all bytes of a
    small (4-read) budget.

    O_DIRECT keeps completions asynchronous so some request is still
    accepted at the second submit; the fault fires once per ring, so batches
    repeat until one reports it.
    """
    child = _run(
        """
        n, size = 32, 256 << 10
        keys, srcs = corpus(n, size)
        bs = os.statvfs(base).f_bsize
        raw = [bytearray(size + bs) for _ in range(n)]
        dsts = []
        for r in raw:
            import ctypes
            a = ctypes.addressof(ctypes.c_char.from_buffer(r))
            off = (-a) % bs
            dsts.append(memoryview(r)[off:off + size])
        c = LMCacheFSClient(base, 1, "", True, 0, 0, 4 * size, 0, 0, 0, 0,
                            "io_uring", 4)
        if c.read_engine() != "io_uring":
            print("NOENGINE")
            raise SystemExit(0)
        baseline = fds()
        failed = None
        for attempt in range(200):
            comp = wait(c, c.submit_batch_get(keys, dsts))
            if not comp[1] or sum(comp[3] or []) != n:
                failed = (comp, attempt)
                break
        if failed is None:
            print("NOFAULT")
            raise SystemExit(0)
        comp, attempt = failed
        print("failed", comp[1], sum(comp[3] or []), c.read_bytes_in_flight())
        for d in dsts:
            d[:] = bytes(len(d))
        again = wait(c, c.submit_batch_get(keys, dsts))
        print("again", again[1], sum(again[3]),
              all(bytes(d) == bytes(s) for d, s in zip(dsts, srcs)))
        print("fds", fds() - baseline, "inflight", c.read_bytes_in_flight())
        c.close()
        """,
        tmp_path,
        fault="submit_error",
    )
    assert child.returncode == 0, child.stderr
    out = child.stdout.split("\n")
    if "NOENGINE" in out:
        pytest.skip("O_DIRECT io_uring is unavailable on this filesystem")
    assert "NOFAULT" not in out, "submit_error never fired; reads never overlapped"
    # Reads reaped before the fault keep their result; the rest fail.
    failed = [line for line in out if line.startswith("failed")]
    assert failed and failed[0].split()[1:2] == ["False"], child.stdout
    assert int(failed[0].split()[2]) < 32 and failed[0].split()[3] == "0"
    assert "again True 32 True" in out, child.stdout
    assert "fds 0 inflight 0" in out, child.stdout


def test_unrecoverable_wait_aborts(tmp_path: Path) -> None:
    """When reaping keeps failing after an engine error, the process aborts
    rather than hand back buffers the kernel may still write."""
    # wait_error_forever makes the first wait fail, entering recovery, which
    # gives up after ten seconds and aborts.
    child = _run(
        """
        n, size = 8, 64 << 10
        keys, srcs = corpus(n, size)
        dsts = [bytearray(size) for _ in range(n)]
        c = LMCacheFSClient(base, 1, read_engine="io_uring", io_depth=8)
        wait(c, c.submit_batch_get(keys, [memoryview(d) for d in dsts]),
             timeout=60)
        print("RETURNED")
        """,
        tmp_path,
        fault="wait_error_forever",
    )
    assert child.returncode == -signal.SIGABRT, (child.returncode, child.stderr)
    assert "io_uring reaping failed" in child.stderr
    assert "RETURNED" not in child.stdout


def test_close_with_reads_in_flight(tmp_path: Path) -> None:
    """close() with 64 batches queued behind slow reads returns, delivers no
    completion twice, and leaves no bytes reserved."""
    child = _run(
        """
        from collections import Counter
        n, size, batches = 8, 64 << 10, 64
        keys, srcs = corpus(n, size)
        dsts = [[bytearray(size) for _ in range(n)] for _ in range(batches)]
        c = LMCacheFSClient(base, 1, read_engine="io_uring", io_depth=4)
        ids = [c.submit_batch_get(keys, [memoryview(d) for d in ds])
               for ds in dsts]
        seen = Counter(comp[0] for comp in c.drain_completions())
        t = time.monotonic()
        c.close()
        elapsed = time.monotonic() - t
        try:
            seen.update(comp[0] for comp in c.drain_completions())
        except Exception:
            pass
        print(len(set(ids)), max(seen.values(), default=0),
              set(seen) <= set(ids), c.read_bytes_in_flight(),
              elapsed < 30)
        """,
        tmp_path,
        fault="cqe_short",
        extra_env={"LMCACHE_FS_FAULT_DELAY_MS": "20"},
    )
    assert child.returncode == 0, child.stderr
    out = child.stdout.split()
    assert out[0] == "64"
    assert int(out[1]) <= 1
    assert out[2:] == ["True", "0", "True"]


def test_error_paths_return_the_byte_budget(tmp_path: Path) -> None:
    """A missing key and EMFILE requeueing each leave zero
    bytes reserved in a small budget."""
    child = _run(
        """
        n, size = 16, 64 << 10
        keys, srcs = corpus(n, size)
        budget = 4 * size
        def client(depth):
            return LMCacheFSClient(base, 1, "", False, 0, 0, budget, 0, 0, 0,
                                   0, "io_uring", depth)
        c = client(4)
        dsts = [bytearray(size) for _ in range(n)]
        mv = [memoryview(d) for d in dsts]
        # open failure: one missing key among real ones
        bad = list(keys)
        bad[5] = "test_model@00000000@0@" + "f" * 16
        comp = wait(c, c.submit_batch_get(bad, mv))
        print("missing", sum(comp[3]), c.read_bytes_in_flight())
        c.close()
        # EMFILE requeue under the same small budget
        c = client(8)
        _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, 65536), hard))
        c = client(8)
        top = max(int(f) for f in os.listdir("/proc/self/fd"))
        resource.setrlimit(resource.RLIMIT_NOFILE, (top + 3, hard))
        comp = wait(c, c.submit_batch_get(keys, mv))
        print("emfile", comp[1], sum(comp[3]), c.read_bytes_in_flight(),
              all(d == s for d, s in zip(dsts, srcs)))
        c.close()
        """,
        tmp_path,
    )
    assert child.returncode == 0, child.stderr
    lines = child.stdout.splitlines()
    assert lines[0] == "missing 15 0"
    assert lines[1] == "emfile True 16 0 True"
