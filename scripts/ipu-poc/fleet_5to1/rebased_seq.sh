#!/bin/bash
# Rebased-tree io_uring gate: fio -> fs_native read pool 256 -> 512 -> fs_uring -> fio.
set -u
BASE_GB=3350
settle() {
  for i in $(seq 1 120); do
    u=$(ssh -o BatchMode=yes -o ControlPath=none mmgt /home/ssd-span-duration-runner/nuse.sh | sed 's/used_GB=\([0-9.]*\).*/\1/')
    python3 -c "import sys; sys.exit(0 if float('$u') <= $BASE_GB else 1)" && { echo "settled used_GB=$u"; return 0; }
    sleep 15
  done
  echo "did not settle used_GB=$u"; return 1
}
FSN='{"type":"fs_native","base_path":"@BASE@","num_workers":64,"use_odirect":true,"num_read_workers":RW,"num_shard_dirs":256,"recycle_max_files":200000,"store_workers":64,"delete_workers":16}'
FSU='{"type":"native_plugin","module_path":"lmcache_fs_uring","class_name":"LMCacheFSUringClient","adapter_params":{"base_path":"@BASE@","num_workers":64,"use_odirect":true,"io_depth":64,"num_shard_dirs":256,"recycle_max_files":200000,"store_workers":64,"delete_workers":16}}'
RB=/root/dig5to1/lmc_rebased
settle || exit 1; /tmp/dig5to1/fleet.sh fio ${TAG:-rb}_fio_a 600
settle || exit 1; PYPATH=$RB /tmp/dig5to1/fleet.sh lmc ${TAG:-rb}_fsn_rw256 600 "${FSN/RW/256}" 64 20000 100000
settle || exit 1; PYPATH=$RB /tmp/dig5to1/fleet.sh lmc ${TAG:-rb}_fsn_rw512 600 "${FSN/RW/512}" 64 20000 100000
settle || exit 1; PYPATH=$RB:/root/dig5to1/fsu_rebased /tmp/dig5to1/fleet.sh lmc ${TAG:-rb}_fsu 600 "$FSU" 64 20000 100000
settle || exit 1; /tmp/dig5to1/fleet.sh fio ${TAG:-rb}_fio_b 600
echo SEQ DONE
