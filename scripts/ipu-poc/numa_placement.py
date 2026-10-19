#!/usr/bin/env python3
"""Sample where a running process's threads are executing, by NUMA node.

Answers "did the worker pool spread across sockets?" with measurement rather
than inference. For each sample it reads every thread's ``last CPU`` from
``/proc/<pid>/task/<tid>/stat`` (field 39, 1-indexed) and maps it to a NUMA node
via ``/sys/devices/system/node/node*/cpulist``.

``last CPU`` is where the thread most recently ran, not a binding, so a single
sample can mislead; the tool takes many and reports the distribution. Threads
that never ran during the sampling window still report their last CPU, so idle
threads are counted where they last executed.

SMT matters for interpretation on this class of host: node0 owning
``0-31,64-95`` means CPUs 64-95 are the SMT siblings of physical cores 0-31, so
"same node" does not imply "different physical core", and a per-socket count of
*logical* CPUs is twice the core count.

Placement alone cannot attribute a throughput difference -- pair it with a
``numactl``-bound control cell, so worker count and placement vary separately.

Usage:
    numa_placement.py --pid PID [--samples 40] [--interval 0.25]
    numa_placement.py --name-contains lmcache [--samples 40]
"""

# Future
from __future__ import annotations

# Standard
import argparse
import os
import sys
import time
from collections import Counter
from pathlib import Path


def _node_map() -> dict[int, int]:
    """Map each logical CPU to its NUMA node.

    Returns:
        Mapping of CPU id to node id.

    Raises:
        RuntimeError: If no node cpulist could be read.
    """
    mapping: dict[int, int] = {}
    for node_dir in sorted(Path("/sys/devices/system/node").glob("node[0-9]*")):
        node = int(node_dir.name[4:])
        try:
            spec = (node_dir / "cpulist").read_text().strip()
        except OSError:
            continue
        for part in spec.split(","):
            if not part:
                continue
            if "-" in part:
                lo, hi = part.split("-")
                for cpu in range(int(lo), int(hi) + 1):
                    mapping[cpu] = node
            else:
                mapping[int(part)] = node
    if not mapping:
        raise RuntimeError("could not read any NUMA node cpulist")
    return mapping


def _thread_cpus(pid: int) -> dict[int, int]:
    """Return each thread's last-run CPU for *pid*.

    Args:
        pid: Process id to inspect.

    Returns:
        Mapping of thread id to last-run CPU. Threads that exit mid-scan are
        skipped rather than raising, since the pool is live while sampled.
    """
    result: dict[int, int] = {}
    task_dir = Path(f"/proc/{pid}/task")
    try:
        tids = list(task_dir.iterdir())
    except OSError:
        return result
    for entry in tids:
        try:
            raw = (entry / "stat").read_text()
        except OSError:
            continue
        # Field 2 (comm) is parenthesized and may itself contain spaces and
        # parens, so split after the LAST ')' rather than tokenizing the whole
        # line. After that split, index 0 is field 3, so field 39 is index 36.
        try:
            rest = raw[raw.rindex(")") + 1 :].split()
            last_cpu = int(rest[36])
        except (ValueError, IndexError):
            continue
        result[int(entry.name)] = last_cpu
    return result


def _find_pid(needle: str) -> int:
    """Find the newest pid whose cmdline contains *needle*.

    Args:
        needle: Substring to match against each process cmdline.

    Returns:
        The matching pid.

    Raises:
        RuntimeError: If no process matches.
    """
    # Our own cmdline contains the needle, since it arrived as an argument. Not
    # skipping self is the `pkill -f` self-match trap: the newest match would
    # always be this process.
    own = os.getpid()
    best: tuple[float, int] = (-1.0, -1)
    matched = ""
    for proc in Path("/proc").glob("[0-9]*"):
        pid = int(proc.name)
        if pid == own:
            continue
        try:
            cmdline = (proc / "cmdline").read_bytes().decode(errors="replace")
            if needle not in cmdline:
                continue
            started = (proc / "stat").stat().st_mtime
        except OSError:
            continue
        if started > best[0]:
            best = (started, pid)
            matched = cmdline.replace("\x00", " ").strip()
    if best[1] < 0:
        raise RuntimeError(f"no process matching {needle!r}")
    # Print what was matched so a wrong-process sample is auditable after the
    # fact rather than silently attributed to the bench.
    print(f"matched pid {best[1]}: {matched[:160]}")
    return best[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--pid", type=int)
    group.add_argument("--name-contains")
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--interval", type=float, default=0.25)
    args = parser.parse_args()

    nodes = _node_map()
    if args.pid:
        pid = args.pid
    else:
        try:
            pid = _find_pid(args.name_contains)
        except RuntimeError as e:
            print(f"ABORT: {e}")
            return 2

    # (tid, node) observations, so a thread that migrates is visible as such
    # rather than averaged into one node.
    per_thread: dict[int, Counter[int]] = {}
    node_samples: Counter[int] = Counter()
    cpu_samples: Counter[int] = Counter()
    taken = 0

    for _ in range(args.samples):
        cpus = _thread_cpus(pid)
        if not cpus:
            break
        for tid, cpu in cpus.items():
            node = nodes.get(cpu, -1)
            per_thread.setdefault(tid, Counter())[node] += 1
            node_samples[node] += 1
            cpu_samples[cpu] += 1
        taken += 1
        time.sleep(args.interval)

    if not taken:
        print(f"ABORT: pid {pid} had no readable threads")
        return 1

    total = sum(node_samples.values())
    print(f"pid {pid}: {taken} samples, {len(per_thread)} threads seen")
    print("--- thread-sample distribution by NUMA node ---")
    for node, count in sorted(node_samples.items()):
        print(f"  node {node}: {count:6d} samples ({count / total:6.1%})")

    # A thread observed on more than one node migrated across sockets during
    # the window, which is the specific behaviour a placement hypothesis needs.
    migrated = sum(1 for c in per_thread.values() if len(c) > 1)
    print(f"--- threads observed on >1 node: {migrated} / {len(per_thread)} ---")
    print(f"--- distinct CPUs used: {len(cpu_samples)} ---")
    busiest = ", ".join(f"{cpu}({n})" for cpu, n in cpu_samples.most_common(8))
    print(f"  busiest CPUs: {busiest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
