# SPDX-License-Identifier: Apache-2.0
"""Functional check of fs_uring file recycling (run on Linux with O_DIRECT)."""

# Standard
import ctypes
import mmap
import os
import select
import sys
import time

# Third Party
import lmcache_fs_uring as m

base = sys.argv[1]
SIZE = 262144


def buf(fill):
    b = mmap.mmap(-1, SIZE)
    b.write(bytes([fill]) * SIZE)
    return b


def addr(b):
    return ctypes.addressof(ctypes.c_char.from_buffer(b))


def wait(c, fid):
    while True:
        for comp in c.drain_completions():
            if comp[0] == fid:
                return comp
        select.select([c.event_fd()], [], [], 0.1)


def files(sub):
    return sorted(os.listdir(sub))


def key(i):
    return f"m@00000000@{i:032x}"


c = m.LMCacheFSUringClient(base, 4, "", True, 32, 4, 100)
a = buf(1)
assert wait(c, c.submit_batch_set([key(1)], [memoryview(a)]))[1]
path = [
    os.path.join(dp, f)
    for dp, _, fs in os.walk(base)
    for f in fs
    if f.endswith(".data")
]
assert len(path) == 1, path
ino = os.stat(path[0]).st_ino
assert wait(c, c.submit_batch_delete([key(1)]))[1]
pool = [
    os.path.join(dp, f) for dp, _, fs in os.walk(base) for f in fs if "/.pool" in dp
]
assert len(pool) == 1 and os.stat(pool[0]).st_ino == ino, pool
# Find a key in the same shard as key(1) so the pooled file is eligible.
shard_dir = os.path.dirname(path[0])
j = 2
while True:
    b = buf(2)
    assert wait(c, c.submit_batch_set([key(j)], [memoryview(b)]))[1]
    p2 = [
        os.path.join(dp, f)
        for dp, _, fs in os.walk(base)
        for f in fs
        if f.endswith(".data") and f"{j:032x}" in f
    ][0]
    if os.path.dirname(p2) == shard_dir:
        break
    j += 1
# Stored inside quarantine: must be a fresh inode, pool untouched.
assert os.stat(p2).st_ino != ino
time.sleep(1.2)
k = j + 1
while True:
    e = buf(3)
    assert wait(c, c.submit_batch_set([key(k)], [memoryview(e)]))[1]
    p3 = [
        os.path.join(dp, f)
        for dp, _, fs in os.walk(base)
        for f in fs
        if f.endswith(".data") and f"{k:032x}" in f
    ][0]
    if os.path.dirname(p3) == shard_dir:
        break
    k += 1
assert os.stat(p3).st_ino == ino, (
    "same-shard store after quarantine must reuse the pooled file"
)
r = buf(0)
assert wait(c, c.submit_batch_get([key(k)], [memoryview(r)]))[1]
assert r[:] == bytes([3]) * SIZE
c.close()
# Restart: a deleted file survives in the pool and is reused by a new process.
c = m.LMCacheFSUringClient(base, 4, "", True, 32, 4, 100)
assert wait(c, c.submit_batch_delete([key(k)]))[1]
c.close()
c = m.LMCacheFSUringClient(base, 4, "", True, 32, 4, 100)
n = k + 1
while True:
    f = buf(4)
    assert wait(c, c.submit_batch_set([key(n)], [memoryview(f)]))[1]
    p4 = [
        os.path.join(dp, x)
        for dp, _, fs in os.walk(base)
        for x in fs
        if x.endswith(".data") and f"{n:032x}" in x
    ][0]
    if os.path.dirname(p4) == shard_dir:
        break
    n += 1
assert os.stat(p4).st_ino == ino, "pool file from an earlier process must be reused"
c.close()
print("recycle ok")
# Dedicated store/delete lanes: same results through the lane workers.
lane_base = base + "-lanes"
os.makedirs(lane_base, exist_ok=True)
c = m.LMCacheFSUringClient(lane_base, 4, "", True, 32, 4, 100, 2, 2)
g = buf(7)
h8 = buf(8)
res = wait(c, c.submit_batch_set([key(500), key(501)], [memoryview(g), memoryview(h8)]))
assert res[1], res
r = buf(0)
assert (
    wait(c, c.submit_batch_get([key(500)], [memoryview(r)]))[1]
    and r[:] == bytes([7]) * SIZE
)
assert wait(c, c.submit_batch_delete([key(500)]))[1]
assert wait(c, c.submit_batch_exists([key(500), key(501)]))[3] == [False, True]
c.close()
print("lanes ok")
