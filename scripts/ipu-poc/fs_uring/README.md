# fs_uring (PoC only)

An io_uring variant of the `fs_native` connector, used to measure how much a
completion-driven read path gains over the thread-pool reads in
`csrc/storage_backends/fs/`. It is not part of the LMCache build or CI, and
nothing in `lmcache/` imports it. It is Linux-only and needs liburing.

It carries the same three store-path changes as `fs_native`
(`num_shard_dirs`, `recycle_max_files`, `store_workers`/`delete_workers`)
plus `io_depth` for the ring.

On the MMG-400 rig (6 initiators, Mixtral 8x22B FP8, 256 KiB objects,
5:1 read:write, 64 submits in flight, 600 s, one run per cell,
2026-11-01) it reached 1,301.8 Gb/s at ~11.3 cores per host. `fs_native`
with its read pool (256 read workers) and the same store-path changes
reached 1,295.2 Gb/s at ~11.6 cores; fio on the same files reached
1,351.6 before and 1,349.4 after. The 0.5% gap at similar CPU is why
`fs_native` keeps thread-based reads and this module stays a PoC.

## Build

```bash
# Ubuntu/RHEL: liburing-dev / liburing-devel, cmake >= 3.20, pybind11
cmake -S . -B build -DPython_EXECUTABLE="$(which python)" \
  -Dpybind11_DIR="$(python -m pybind11 --cmakedir)"
cmake --build build -j
```

The build includes the shared connector headers from
`../../../csrc/storage_backends`, so build from inside an LMCache checkout.
The module lands at `build/lmcache_fs_uring*.so`.

## Use

Load it through the `native_plugin` L2 adapter; `adapter-config.json` is the
configuration used for the measurement above:

```json
{"type": "native_plugin", "module_path": "lmcache_fs_uring",
 "class_name": "LMCacheFSUringClient", "adapter_params": {...}}
```

## Test

`python tools/run_tests.py --xfs` builds a loop-mounted XFS image (needs
passwordless `sudo` for `losetup`/`mount`) and runs `tests/`.
`tools/check_recycle.py` is a standalone recycling and lanes check:
`python tools/check_recycle.py <dir on an O_DIRECT filesystem>`.
