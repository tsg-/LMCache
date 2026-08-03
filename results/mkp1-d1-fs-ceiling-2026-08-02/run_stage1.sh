#!/bin/bash
#
# D1 FS-ceiling preflight v10 (revised after Codex ninth-pass review 2026-08-02):
# NOTE: This is NOT plan §7 Stage 1 evidence. It is a filesystem-ceiling
# preflight for D1. Do not treat its output as Stage 1 T1/T2b completion.
#   Matched raw baseline (FIO on raw remote namespaces) + XFS+md0 ceiling
#   on MKP1↔MKP2 100 GbE RoCEv2 fabric. Read-focused.
#
# SCOPE: filesystem + RAID 0 ceiling on the wire. Does NOT validate
# LocalDiskBackend, per-file overhead, or Python contention.
#
# SAFETY: NVMe device names are unstable across reconnects; this script
# resolves the two remote namespaces by NQN, verifies size/mount/holders
# recursively, refuses to run if any check fails, and installs an EXIT
# trap for failure-path teardown.

set -euo pipefail

BASE=/root/mkp1-d1/stage1
LOG=$BASE/logs/run.log
NQN1=mkp2-nvme1
NQN2=mkp2-nvme2
MD=/dev/md0
MNT=/mnt/md0
CHUNK_KB=256
XFS_SIZE_GIB=1024
NUMJOBS_PRIMARY=1
NUMJOBS_SANITY=4
NVMEOF_UTIL=$BASE/nvmeof_util.py

# --- 0. Directory prep (Codex LOW fix) ---------------------------------------
mkdir -p $BASE/{logs,manifest,fio_raw,fio_xfs,populate_out,pmu,before,after}

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a $LOG; }

# --- EXIT trap (Codex HIGH fix: failure-path teardown) -----------------------
CLEANUP_ARMED=0
MD_CREATED=0
MD_CREATE_ATTEMPTED=0
cleanup() {
    local rc=$?
    if [ $CLEANUP_ARMED -eq 0 ]; then
        exit $rc
    fi
    log "=== EXIT trap: safe teardown (rc=$rc) ==="
    # Only unmount if it is OUR md at OUR mountpoint
    if findmnt -rn -o SOURCE --mountpoint "$MNT" 2>/dev/null | grep -qFx "$MD"; then
        umount $MNT 2>&1 | tee -a $LOG || log "  warn: umount failed"
    fi
    # Stop the array if WE created it (or attempted to and it exists).
    # This handles the case where mdadm --create started the array then errored;
    # in that case MD_CREATED=0 but /dev/md0 exists and is ours.
    if [ ${MD_CREATED:-0} -eq 1 ] || [ ${MD_CREATE_ATTEMPTED:-0} -eq 1 ]; then
        if [ -e $MD ]; then
            mdadm --stop $MD 2>&1 | tee -a $LOG || log "  warn: mdadm --stop failed"
        fi
    fi
    log "=== EXIT trap done (rc=$rc) ==="
    exit $rc
}
trap cleanup EXIT

log "===== D1 FS-ceiling preflight (was: Stage 1 v10) starting at $(date -Iseconds) ====="

# --- MD/MNT ownership preflight (Codex round-3 BLOCKER/HIGH fix) -------------
if [ -e $MD ]; then
    log "FATAL: $MD already exists (may be an unrelated array). Refusing to touch."
    log "If you know it is safe: sudo mdadm --stop $MD, then rerun."
    exit 2
fi
if findmnt -rn --mountpoint "$MNT" >/dev/null 2>&1; then
    log "FATAL: $MNT is already a mountpoint. Refusing to touch."
    exit 2
fi

# --- 1. Preflight: helper exists (Codex BLOCKER fix) --------------------------
if [ ! -f $NVMEOF_UTIL ]; then
    log "FATAL: $NVMEOF_UTIL missing. Copy scripts/nvmeof_util.py from the LMCache repo. Aborting."
    exit 2
fi
python3 -c "import sys; sys.path.insert(0, '$BASE'); import nvmeof_util" 2>&1 | tee -a $LOG || {
    log "FATAL: nvmeof_util.py failed to import. Aborting."
    exit 2
}

# --- 2. Resolve devices by NQN (Codex BLOCKER fix) ---------------------------
log "=== Resolving remote namespaces by NQN ==="
nvme list-subsys -o json > $BASE/manifest/nvme_list_subsys.json 2>&1
# Atomic selector: return the controller name IFF the NQN has exactly ONE
# deduped rdma+traddr path (Codex round-6 HIGH fix). No separate
# find-controller + verify — one function does both.
select_rdma_controller() {
    local nqn=$1
    local expected_addr=$2
    python3 - <<PYINNER
import json, sys
d = json.load(open("$BASE/manifest/nvme_list_subsys.json"))

def _iter_subs(p):
    if isinstance(p, dict): entries = p.get("Subsystems") or []
    elif isinstance(p, list): entries = p
    else: return
    for e in entries:
        if not isinstance(e, dict): continue
        nested = e.get("Subsystems")
        if isinstance(nested, list):
            for sub in nested:
                if isinstance(sub, dict): yield sub
        else: yield e

target_nqn = "$nqn"
target_addr = "$expected_addr"
matches = set()  # dedup by controller name
for sub in _iter_subs(d):
    if (sub.get("NQN") or sub.get("Subsystem NQN") or sub.get("SubsystemNQN")) != target_nqn:
        continue
    # nvme-cli emits Controllers or Paths (or both aliases) — dedup by name
    for key in ("Controllers", "Paths"):
        entries = sub.get(key) or []
        for entry in entries:
            if not isinstance(entry, dict): continue
            name = entry.get("Controller") or entry.get("Name")
            if not (isinstance(name, str) and name):
                continue
            transport = (entry.get("Transport") or entry.get("transport") or "").lower()
            addr = entry.get("Address") or entry.get("address") or ""
            # Parse NVMe address attributes: "traddr=200.0.0.37,trsvcid=4420,..."
            attrs = {}
            for pair in addr.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    attrs[k.strip()] = v.strip()
            if transport == "rdma" and attrs.get("traddr") == target_addr:
                matches.add(name)

if len(matches) != 1:
    sys.stderr.write(f"NQN {target_nqn}: expected exactly 1 rdma+{target_addr} controller, found {len(matches)}: {sorted(matches)}\n")
    sys.exit(1)
print(next(iter(matches)))
PYINNER
}

CTRL1=$(select_rdma_controller $NQN1 200.0.0.37) || { log "FATAL: could not uniquely resolve $NQN1 to rdma+200.0.0.37"; exit 2; }
CTRL2=$(select_rdma_controller $NQN2 200.0.0.37) || { log "FATAL: could not uniquely resolve $NQN2 to rdma+200.0.0.37"; exit 2; }
log "Resolved: $NQN1 -> $CTRL1, $NQN2 -> $CTRL2 (both rdma+200.0.0.37, unique)"
NS1=/dev/${CTRL1}n1
NS2=/dev/${CTRL2}n1
if [ ! -b $NS1 ] || [ ! -b $NS2 ]; then
    log "FATAL: $NS1 or $NS2 is not a block device. Aborting."
    exit 2
fi

# Resolve stable by-id paths
find_by_id() {
    local dev=$1
    local link
    for link in /dev/disk/by-id/nvme-*; do
        [ -e "$link" ] || continue
        # exclude *-part* entries and *_1 duplicate wwn entries would still resolve to same; take first
        [[ "$link" == *-part* ]] && continue
        if [ "$(readlink -f $link 2>/dev/null)" = "$dev" ]; then
            echo $link
            return
        fi
    done
}
BYID1=$(find_by_id $NS1)
BYID2=$(find_by_id $NS2)
if [ -z "$BYID1" ] || [ -z "$BYID2" ]; then
    log "FATAL: could not find /dev/disk/by-id/nvme-* link for $NS1 ($BYID1) or $NS2 ($BYID2)."
    log "       Refusing to fall back to unstable device names for destructive operations."
    exit 2
fi
# Require two DISTINCT by-id paths
if [ "$BYID1" = "$BYID2" ]; then
    log "FATAL: by-id paths collapsed to same link: $BYID1"
    exit 2
fi

log "  NQN1=$NQN1  ctrl=$CTRL1  ns=$NS1  by-id=$BYID1"
log "  NQN2=$NQN2  ctrl=$CTRL2  ns=$NS2  by-id=$BYID2"

# --- 3. Fail-closed identity checks (Codex HIGH fix: recursive lsblk) --------
log "=== Fail-closed checks (recursive mount/holder, size, model) ==="
FAIL=0
for ns in $NS1 $NS2; do
    log "  --- checking $ns ---"
    # lsblk shows the device tree including partitions and their mount/holders
    lsblk -ln -o NAME,MAJ:MIN,TYPE,MOUNTPOINT,SIZE $ns 2>&1 | tee -a $LOG

    # Any mounted descendant?
    mounted=$(lsblk -ln -o MOUNTPOINT $ns 2>/dev/null | awk 'NF>0' | head -1)
    if [ -n "$mounted" ]; then
        log "  ABORT: $ns has a mounted descendant: $mounted"
        FAIL=1
    fi

    # Any holder anywhere in the tree (md, dm, crypt, etc.)?
    dev=$(basename $ns)
    for sub in /sys/block/$dev /sys/block/$dev/*; do
        [ -d "$sub/holders" ] || continue
        holders=$(ls $sub/holders 2>/dev/null)
        if [ -n "$holders" ]; then
            log "  ABORT: $sub has holders: $holders"
            FAIL=1
        fi
    done

    # Size check (1.8-2.0 TB expected)
    size_bytes=$(blockdev --getsize64 $ns 2>/dev/null || echo 0)
    size_tb=$(awk "BEGIN {printf \"%.2f\", $size_bytes/1e12}")
    if awk "BEGIN {exit !($size_tb < 1.8 || $size_tb > 2.0)}"; then
        log "  ABORT: $ns size $size_tb TB not in expected 1.8-2.0 TB range"
        FAIL=1
    fi
    log "  $ns size $size_tb TB OK"

    # Model check — must NOT be a physical Samsung drive
    model=$(nvme id-ctrl $ns 2>/dev/null | awk '/^mn/ {for (i=2;i<=NF;i++) printf "%s ",$i; print ""}' | xargs)
    log "  $ns model: $model"
    if echo "$model" | grep -qi samsung; then
        log "  ABORT: $ns model looks like a local physical Samsung drive."
        FAIL=1
    fi
    # Swap check on every descendant (Codex round-3 HIGH fix)
    for desc in $(lsblk -nrpo NAME $ns 2>/dev/null); do
        if grep -qE "^${desc}[[:space:]]" /proc/swaps; then
            log "  ABORT: $ns descendant $desc is in /proc/swaps"
            FAIL=1
        fi
    done

    # Emptiness / signature check (Codex round-8 HIGH fix).
    # wipefs -n reports any recognized signature (fs, partition, raid, etc.)
    # without modifying anything. mdadm --examine reports any md superblock
    # (even inactive). Reject the run unless STAGE1_FORCE_DESTRUCTIVE=1 is
    # set AND both by-id devices are named in STAGE1_APPROVED_DEVICES.
    wipefs_out=$(wipefs -n $ns 2>&1 | grep -v "^DEVICE\|^$" || true)
    if [ -n "$wipefs_out" ]; then
        log "  WARN: $ns has recognized signatures:"
        echo "$wipefs_out" | sed 's/^/    /' | tee -a $LOG
        NEED_OVERRIDE=1
    fi
    mdexam_out=$(mdadm --examine $ns 2>/dev/null | grep -E "Magic|Version|Array UUID" || true)
    if [ -n "$mdexam_out" ]; then
        log "  WARN: $ns has an MD superblock:"
        echo "$mdexam_out" | sed 's/^/    /' | tee -a $LOG
        NEED_OVERRIDE=1
    fi
done

# Enforce override if any non-empty signature was found
if [ "${NEED_OVERRIDE:-0}" -eq 1 ]; then
    if [ "${STAGE1_FORCE_DESTRUCTIVE:-0}" != "1" ]; then
        log "ABORT: one or more remote namespaces contain existing signatures."
        log "       Set STAGE1_FORCE_DESTRUCTIVE=1 to override."
        FAIL=1
    else
        # Require STAGE1_APPROVED_DEVICES to name BOTH by-id basenames as whitespace-delimited exact tokens
        approved=${STAGE1_APPROVED_DEVICES:-}
        # Read tokens into an array
        read -ra approved_tokens <<< "$approved"
        want1=$(basename $BYID1)
        want2=$(basename $BYID2)
        have1=0
        have2=0
        for tok in "${approved_tokens[@]}"; do
            [ "$tok" = "$want1" ] && have1=1
            [ "$tok" = "$want2" ] && have2=1
        done
        if [ $have1 -ne 1 ] || [ $have2 -ne 1 ]; then
            log "ABORT: STAGE1_FORCE_DESTRUCTIVE=1 but STAGE1_APPROVED_DEVICES does not exactly name both by-id basenames:"
            log "       want: $want1"
            log "       want: $want2"
            log "       got:  ${approved_tokens[*]}"
            log "       set: export STAGE1_APPROVED_DEVICES=\"$want1 $want2\""
            FAIL=1
        else
            log "OVERRIDE: STAGE1_FORCE_DESTRUCTIVE=1 with exact STAGE1_APPROVED_DEVICES; proceeding."
        fi
    fi
fi
if [ $FAIL -ne 0 ]; then
    log "===== ABORTED due to fail-closed check failures. No destructive ops issued. ====="
    exit 3
fi
log "All identity checks passed."

# Arm cleanup trap now that we're about to touch destructive state
CLEANUP_ARMED=1

# --- 4. MANIFEST (before) + NIC/SMART/queue counters -------------------------
log "=== MANIFEST (before) ==="
{
    echo "timestamp: $(date -Iseconds)"
    echo "hostname_initiator: $(hostname)"
    echo "hostname_target: mkp2 (200.0.0.37)"
    echo "kernel: $(uname -r)"
    echo "cpu: $(grep 'model name' /proc/cpuinfo | head -1 | cut -d: -f2 | xargs)"
    echo "dram: $(free -h | awk '/^Mem:/ {print $2}')"
    echo "fio_version: $(fio --version)"
    echo "mdadm_version: $(mdadm --version 2>&1)"
    echo "mkfs_xfs_version: $(mkfs.xfs -V 2>&1)"
    echo "kernel_perf_paranoid: $(cat /proc/sys/kernel/perf_event_paranoid)"
    echo "nqn1: $NQN1 -> $NS1 (by-id: $BYID1)"
    echo "nqn2: $NQN2 -> $NS2 (by-id: $BYID2)"
    echo "chunk_kb: $CHUNK_KB"
    echo "xfs_size_gib: $XFS_SIZE_GIB"
    echo "nvme_connect_note: --nr-io-queues=16 per controller (irdma ENOMEM at default 128)"
    echo "SCOPE: D1 FS-ceiling PREFLIGHT (md0/XFS raw baseline vs XFS ceiling). NOT plan Stage 1 T1/T2b evidence. Does NOT validate LocalDiskBackend."
} | tee $BASE/manifest/manifest.txt

# Reproducibility snapshot: fail closed on any copy/hash failure (Codex round-7 fix)
cp "$0" $BASE/manifest/stage1_script.sh || { log "FATAL: cannot snapshot script"; exit 6; }
cp $NVMEOF_UTIL $BASE/manifest/nvmeof_util.py || { log "FATAL: cannot snapshot nvmeof_util.py"; exit 6; }
SCRIPT_SHA=$(sha256sum $BASE/manifest/stage1_script.sh | awk '{print $1}') || { log "FATAL: script sha failed"; exit 6; }
UTIL_SHA=$(sha256sum $BASE/manifest/nvmeof_util.py | awk '{print $1}') || { log "FATAL: util sha failed"; exit 6; }
echo "script_sha256: $SCRIPT_SHA" >> $BASE/manifest/manifest.txt
echo "nvmeof_util_sha256: $UTIL_SHA" >> $BASE/manifest/manifest.txt
log "Snapshot: script sha=$SCRIPT_SHA  util sha=$UTIL_SHA"

ethtool -S ens2f0 2>/dev/null > $BASE/before/ethtool_ens2f0.txt || true
cat /proc/net/dev > $BASE/before/proc_net_dev.txt || true
nvme smart-log $NS1 > $BASE/before/smart_$(basename $NS1).txt 2>&1 || true
nvme smart-log $NS2 > $BASE/before/smart_$(basename $NS2).txt 2>&1 || true
for ns in $NS1 $NS2; do
    dev=$(basename $ns)
    {
        echo "== /sys/block/$dev/queue =="
        for f in nr_requests scheduler max_sectors_kb max_hw_sectors_kb read_ahead_kb rotational; do
            echo "$f: $(cat /sys/block/$dev/queue/$f 2>/dev/null)"
        done
    } >> $BASE/before/queue_limits.txt
done

# --- 5. MATCHED RAW BASELINE (Codex HIGH fix: --size matches XFS workset) ----
log "=== 5. Matched raw baseline FIO (BEFORE md0), --size=${XFS_SIZE_GIB}G ==="
BSLIST="256k"
QDLIST="16 64 256"
RUNTIME=60
RAMP=5
REPS=3

pat_flag() { case $1 in seq_read) echo read;; rand_read) echo randread;; esac; }

for pat in seq_read rand_read; do
    for bs in $BSLIST; do
        for qd in $QDLIST; do
            for rep in $(seq 1 $REPS); do
                rw=$(pat_flag $pat)
                out=$BASE/fio_raw/${pat}_${bs}_qd${qd}_rep${rep}.json
                HALF=$((XFS_SIZE_GIB / 2))
                PER_QD=$((qd / 2))
                log "  raw ${pat} ${bs} QD=${qd} (${PER_QD}/dev × 2 devs) rep=${rep} size=${HALF}G/ns"
                # Two jobs, one per ns. --size=${HALF}G/ns gives ~1 TiB total workset
                # (matched workset size and approximate LBA range vs. the 1 TiB XFS
                # file; not proven physical-extent equivalence — xfs_bmap runs later
                # and would differ if the file is allocated in multiple extents).
                # Each job gets iodepth=qd/2 so aggregate QD across the pair equals
                # the XFS job's iodepth.
                fio --group_reporting=1 --output-format=json --output=$out \
                    --rw=$rw --bs=$bs --iodepth=$PER_QD --ioengine=libaio \
                    --direct=1 --numjobs=$NUMJOBS_PRIMARY \
                    --time_based --runtime=$RUNTIME --ramp_time=$RAMP \
                    --randseed=$((42 + rep)) \
                    --name=raw_${pat}_dev1 --filename=$NS1 --size=${HALF}G \
                    --name=raw_${pat}_dev2 --filename=$NS2 --size=${HALF}G \
                    2>>$BASE/logs/fio_raw.err
            done
        done
    done
done

# numjobs sanity cell
log "  raw sanity: rand_read 256k QD 64 numjobs=$NUMJOBS_SANITY"
HALF=$((XFS_SIZE_GIB / 2))
SANITY_PER=$((NUMJOBS_SANITY / 2))
SANITY_QD=32
# Aggregate: SANITY_PER numjobs × iodepth=SANITY_QD × 2 devs = 2 × 32 × 2 = 128
# XFS sanity (below): numjobs=$NUMJOBS_SANITY × iodepth=32 through md0 = 4 × 32 = 128. Matched.
log "  raw sanity: rand_read 256k (${SANITY_PER}jobs/dev × iodepth=$SANITY_QD × 2 devs = 128 outstanding)"
fio --rw=randread --bs=256k --iodepth=$SANITY_QD --ioengine=libaio \
    --direct=1 --numjobs=$SANITY_PER \
    --time_based --runtime=$RUNTIME --ramp_time=$RAMP \
    --group_reporting=1 --output-format=json \
    --output=$BASE/fio_raw/raw_sanity_numjobs${NUMJOBS_SANITY}.json \
    --randseed=999 \
    --name=raw_sanity_dev1 --filename=$NS1 --size=${HALF}G \
    --name=raw_sanity_dev2 --filename=$NS2 --size=${HALF}G \
    2>>$BASE/logs/fio_raw.err

# --- 6. mdadm --create using by-id paths -------------------------------------
log "=== 6. mdadm --create md0 (by-id, chunk=${CHUNK_KB}k) ==="
# If override was engaged, wipe any signatures first (Codex round-9 BLOCKER fix).
# mdadm --zero-superblock removes MD metadata only, not GPT/fs/other signatures.
# mdadm --create refuses to touch partitioned devices without --force; we prefer
# to make devices truly empty rather than force.
if [ "${NEED_OVERRIDE:-0}" -eq 1 ]; then
    log "  wipefs -a $BYID1 (approved override)"
    wipefs -a $BYID1 2>&1 | tee -a $BASE/logs/mdadm.log
    log "  wipefs -a $BYID2 (approved override)"
    wipefs -a $BYID2 2>&1 | tee -a $BASE/logs/mdadm.log
    # Verify truly empty now
    for d in $BYID1 $BYID2; do
        remain=$(wipefs -n $d 2>&1 | grep -v "^DEVICE\|^$" || true)
        if [ -n "$remain" ]; then
            log "FATAL: $d still has signatures after wipefs -a:"
            echo "$remain" | sed 's/^/    /' | tee -a $LOG
            exit 7
        fi
    done
    log "  post-wipefs verification: both devices report empty."
fi
mdadm --zero-superblock --force $BYID1 2>&1 | tee -a $BASE/logs/mdadm.log || true
mdadm --zero-superblock --force $BYID2 2>&1 | tee -a $BASE/logs/mdadm.log || true
sleep 1
MD_CREATE_ATTEMPTED=1
mdadm --create --verbose $MD --level=0 --raid-devices=2 --chunk=${CHUNK_KB} \
    $BYID1 $BYID2 --run 2>&1 | tee -a $BASE/logs/mdadm.log
MD_CREATED=1
sleep 3
cat /proc/mdstat | tee -a $BASE/logs/mdadm.log
mdadm --detail $MD | tee $BASE/manifest/mdadm_detail.txt

# --- 7. mkfs.xfs + mount -----------------------------------------------------
log "=== 7. mkfs.xfs on md0 ==="
mkfs.xfs -f -m crc=1,reflink=0 -d agcount=32,su=${CHUNK_KB}k,sw=2 $MD 2>&1 | tee $BASE/logs/mkfs.log
xfs_info $MD > $BASE/manifest/xfs_info.txt 2>&1 || true
mkdir -p $MNT
mount -o noatime,nodiratime,logbufs=8 $MD $MNT
mount | grep $MD | tee -a $BASE/manifest/xfs_info.txt
df -h $MNT | tee -a $BASE/manifest/xfs_info.txt

# --- 8. Populate 1 TiB test file ---------------------------------------------
log "=== 8. Populate ${XFS_SIZE_GIB} GiB testfile ==="
fio --name=populate --filename=$MNT/testfile --rw=write --bs=1M --iodepth=32 \
    --ioengine=libaio --direct=1 --numjobs=1 --group_reporting=1 \
    --size=${XFS_SIZE_GIB}G --output-format=terse \
    --output=$BASE/populate_out/populate.out 2>&1 | tee $BASE/logs/populate.log
sync
echo 3 > /proc/sys/vm/drop_caches
xfs_bmap -v $MNT/testfile > $BASE/manifest/xfs_bmap_testfile.txt 2>&1 || true
filefrag -v $MNT/testfile | head -50 > $BASE/manifest/filefrag_testfile.txt 2>&1 || true

# --- 9. XFS+md0 FIO read matrix ---------------------------------------------
log "=== 9. FIO read matrix on XFS+md0 ==="
for pat in seq_read rand_read; do
    for bs in $BSLIST; do
        for qd in $QDLIST; do
            for rep in $(seq 1 $REPS); do
                rw=$(pat_flag $pat)
                out=$BASE/fio_xfs/${pat}_${bs}_qd${qd}_rep${rep}.json
                log "  xfs ${pat} ${bs} QD=${qd} rep=${rep} numjobs=1"
                fio --name=xfs_${pat} --filename=$MNT/testfile \
                    --rw=$rw --bs=$bs --iodepth=$qd --ioengine=libaio \
                    --direct=1 --numjobs=$NUMJOBS_PRIMARY \
                    --time_based --runtime=$RUNTIME --ramp_time=$RAMP \
                    --group_reporting=1 --output-format=json --output=$out \
                    --randseed=$((42 + rep)) 2>>$BASE/logs/fio_xfs.err
            done
        done
    done
done
# XFS sanity: numjobs=4 iodepth=32 => 128 outstanding
# Raw sanity: 2 devs × 2 jobs/dev × iodepth=32 => 128 outstanding. Matched.
log "  xfs sanity: rand_read 256k iodepth=32 numjobs=$NUMJOBS_SANITY (agg concurrency 128)"
fio --name=xfs_sanity --filename=$MNT/testfile \
    --rw=randread --bs=256k --iodepth=32 --ioengine=libaio \
    --direct=1 --numjobs=$NUMJOBS_SANITY \
    --time_based --runtime=$RUNTIME --ramp_time=$RAMP \
    --group_reporting=1 --output-format=json \
    --output=$BASE/fio_xfs/xfs_sanity_numjobs${NUMJOBS_SANITY}.json \
    --randseed=999 2>>$BASE/logs/fio_xfs.err

# --- 10. Post-run counters ---------------------------------------------------
log "=== 10. Post-run counters ==="
ethtool -S ens2f0 2>/dev/null > $BASE/after/ethtool_ens2f0.txt || true
cat /proc/net/dev > $BASE/after/proc_net_dev.txt || true
nvme smart-log $NS1 > $BASE/after/smart_$(basename $NS1).txt 2>&1 || true
nvme smart-log $NS2 > $BASE/after/smart_$(basename $NS2).txt 2>&1 || true

# --- 11. Summary --------------------------------------------------------------
log "=== 11. Summary ==="
python3 - <<PY | tee $BASE/summary.txt
import json, glob, os, statistics
from collections import defaultdict

BASE = "$BASE"

def load(dirname):
    rows = []
    for path in sorted(glob.glob(f"{BASE}/{dirname}/*.json")):
        try:
            d = json.load(open(path))
            j = d["jobs"][0]
            s = j["read"]
            name = os.path.basename(path).replace(".json","")
            rows.append((name, s["bw_bytes"]/1e9,
                         s["clat_ns"]["percentile"]["99.000000"]/1000,
                         s["iops"]))
        except Exception as e:
            print(f"ERR {path}: {e}")
    return rows

def group_and_report(label, rows):
    print(f"\n=== {label} ===")
    print(f"{'cell':<45} {'bw_med GB/s':>12} {'p99_med us':>12} {'reps':>5}")
    groups = defaultdict(list)
    for name, bw, p99, iops in rows:
        cell = name.rsplit("_rep", 1)[0]
        groups[cell].append((bw, p99))
    for cell in sorted(groups):
        bws = [b for b,_ in groups[cell]]
        p99s = [p for _,p in groups[cell]]
        print(f"{cell:<45} {statistics.median(bws):>12.2f} {statistics.median(p99s):>12.0f} {len(bws):>5}")

group_and_report("Matched raw baseline (over wire, no FS)", load("fio_raw"))
group_and_report("XFS + md0 ceiling", load("fio_xfs"))
PY

# --- 12. Normal-path cleanup (safe; EXIT trap covers failure paths) ----------
log "=== 12. Cleanup ==="
umount_ok=1
mdstop_ok=1
if findmnt -rn -o SOURCE --mountpoint "$MNT" 2>/dev/null | grep -qFx "$MD"; then
    umount $MNT 2>&1 | tee -a $LOG || umount_ok=0
fi
sync
if [ $MD_CREATED -eq 1 ] && [ -e $MD ]; then
    mdadm --stop $MD 2>&1 | tee -a $BASE/logs/mdadm.log || mdstop_ok=0
fi
# Verify the array is actually gone (Codex round-8 LOW fix)
if [ $mdstop_ok -eq 1 ] && [ -e $MD ]; then
    mdstop_ok=0
    log "  warn: mdadm --stop returned OK but $MD still exists"
fi
if [ $umount_ok -eq 1 ] && [ $mdstop_ok -eq 1 ]; then
    CLEANUP_ARMED=0
    log "md0 stopped (verified: $MD does not exist). Superblocks NOT zeroed."
    log "To fully wipe (destructive): mdadm --zero-superblock --force $BYID1 $BYID2"
else
    log "WARN: cleanup incomplete. Trap remains armed. umount_ok=$umount_ok mdstop_ok=$mdstop_ok"
    exit 5
fi

log "===== D1 FS-ceiling preflight (v10) done at $(date -Iseconds) ====="
