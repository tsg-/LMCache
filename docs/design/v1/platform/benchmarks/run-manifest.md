# Benchmark Run Manifest

`scripts/bench_manifest.py` emits schema-v1 JSON before a benchmark runner
changes port state or starts a coordinator, server, or client process. It keeps
hardware provenance in benchmark infrastructure rather than the LMCache runtime.

## Interface

```bash
python scripts/bench_manifest.py \
  --run-label run_20260715T0700 --role storage \
  --numa-node 1 --nic rocep153s0f1 --gid-index 3 \
  --require-env LMCACHE_RDMA_GID_INDEX --min-memlock-bytes $((8 * 1024**3)) \
  --alloc-region l1_pool:4294967296 \
  --alloc-region qp_buffer:16777216 \
  --output /tmp/lmcache_manifest_run_20260715T0700_storage.json
```

The tool writes one object to `--output`, echoes it to stdout, and returns
zero only when `overall_gate_failed` is false. A collector failure returns two;
a completed manifest with a failed gate returns one.

Schema v1 contains `run`, `numa`, `pcie`, `rdma`, `software`, and `runtime`.
Every gated section has `gate_failed`; `overall_gate_failed` is their logical
OR and `gate_failures` contains diagnostic text. Downstream consumers such as
`tj2` may rely on these names and types. A breaking change requires a new
`manifest_version`.

## Gates

- NUMA fails if an anonymous mmap probe's own VMA has pages outside
  `--numa-node` or the process CPU affinity is not contained by that node.
- PCIe fails if the selected RDMA device is not local to `--numa-node`.
- RDMA fails unless the port is active, has an active MTU of at least 4096,
  and exposes the requested GID index.
- Software records driver and rdma-core versions when available. Unknown
  driver-version strings are warn-only because vendor builds may omit them.
- Runtime fails if finite memlock is below `--min-memlock-bytes` (8 GiB by
  default) or any `--require-env` variable is unset. Unlimited remains
  preferred but is not required above that threshold.

Git dirtiness and one-gigabyte hugepage headroom are included as provenance
only. They are intentionally warn-only because they are workload and
host-policy dependent.

## Probe Tradeoff

The tool does not own the benchmark's real L1 and queue allocations. For M1 it
uses a short-lived, page-faulted anonymous mmap `--alloc-region` probe. It
captures the mmap address and parses only that VMA from `/proc/self/numa_maps`;
unrelated Python, glibc, and heap VMAs cannot contaminate the gate. This avoids
PID coordination before a benchmark exists. It proves placement policy, not the
address range of a live allocator. A later manifest version may use
`/proc/<pid>/numa_maps` after allocation when a runner can provide an
unambiguous region address.

## Runner Contract

`nixl_bench_runner.py` and `rdma_bench_runner.py` require explicit NUMA-node
arguments and invoke the manifest on both participating hosts. A nonzero
result raises before any benchmark process starts; there is no warn-and-run
fallback. This is a deliberate breaking CLI change: existing NIXL invocations
must add `--src-numa-node` and `--dst-numa-node`; raw-RDMA invocations must add
`--server-numa-node` and `--client-numa-node`. The resulting paths are printed
for attachment to the benchmark bead.
