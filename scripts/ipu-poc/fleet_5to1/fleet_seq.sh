#!/bin/bash
# fio600 -> lmc600 -> fio600, each started only after SSD-visible use settles to baseline.
set -u
A='{"type":"native_plugin","module_path":"lmcache_fs_uring","class_name":"LMCacheFSUringClient","adapter_params":{"base_path":"@BASE@","num_workers":64,"use_odirect":true,"io_depth":64,"num_shard_dirs":256}}'
BASE_GB=3350
settle() {
  for i in $(seq 1 120); do
    u=$(ssh -o BatchMode=yes -o ControlPath=none mmgt /home/ssd-span-duration-runner/nuse.sh | sed 's/used_GB=\([0-9.]*\).*/\1/')
    python3 -c "import sys; sys.exit(0 if float('$u') <= $BASE_GB else 1)" && { echo "settled used_GB=$u"; return 0; }
    sleep 15
  done
  echo "did not settle used_GB=$u"; return 1
}
CELLS=${CELLS:-"lmc v1_if32_t30 300 32 30|lmc v2_if16_t120 300 16 120|lmc v3_if32_t120 300 32 120"}
IFS='|' read -ra LIST <<< "$CELLS"
for cell in "${LIST[@]}"; do
  set -- $cell
  settle || exit 1
  if [ "$1" = lmc ]; then
    W=${6:-64}; D=${7:-64}; SW=${8:-32}; DW=${9:-16}
    AC=$(echo "${A_OVERRIDE:-$A}" | sed "s/\"num_workers\":64/\"num_workers\":$W/; s/\"io_depth\":64/\"io_depth\":$D/; s/\"store_workers\":32/\"store_workers\":$SW/; s/\"delete_workers\":16/\"delete_workers\":$DW/")
    /tmp/dig5to1/fleet.sh lmc "$2" "$3" "$AC" "$4" 20000 "$5"
  else
    /tmp/dig5to1/fleet.sh fio "$2" "$3"
  fi
done
echo SEQ DONE
