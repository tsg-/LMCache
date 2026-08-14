#!/bin/bash
#
# D1 T2b read-side matrix, plan-conformant durations (LMCache-hfi, partial).
#
# SCOPE: READ-ONLY, direct-block, over the wire. NO md0, NO XFS, NO LMCache
# in the path. This is the read half of plan §5.2 / T2b at the plan's
# durations and repetition count (60 s latency / 300 s bandwidth / 5 reps).
#
# WHAT THIS DOES *NOT* CLOSE: T2b requires direction in {read, write} and T2a
# requires deterministic pattern writes with external SHA-256 verification.
# Both are excluded here by operator decision (md0 + XFS + Stage 2 .pt corpus
# must survive). D1 therefore REMAINS OPEN after this run.
#
# SAFETY MODEL:
#   - Not one fio invocation in this script issues a write. Every job is
#     --rw=read or --rw=randread, and a preflight guard greps this file for
#     write-capable rw values and aborts if any appear.
#   - md0 stays assembled and /mnt/lmcache-stage2 stays mounted throughout.
#     O_DIRECT reads of the underlying namespaces cannot disturb either.
#     Both are asserted present before and after the run.
#   - Namespaces are resolved by NQN and then verified against known serials;
#     unstable /dev/nvmeXn1 names are never trusted directly.
#
# EVIDENCE HYGIENE (the Stage 2 smoke lesson): every cell cross-checks fio's
# reported I/O count against RDMA hw_counters, so a cell that reports ops/s
# without moving data on the wire is caught rather than published.
#
# Counter selection was determined empirically on this rig (2026-08-03), not
# assumed:
#   - /proc/net/dev is USELESS here: RDMA bypasses the kernel netdev path.
#   - ethtool -S ens2f0 port-rx_bytes is ALSO useless: irdma does not account
#     RDMA-offloaded traffic in ethtool port counters (a 34 GB read moved
#     port-rx_bytes by ~3.8 KB, i.e. control traffic only).
#   - hw_counters/InRdmaWrites IS exact. The target satisfies an NVMe-oF read
#     by RDMA-writing into initiator memory, so a read workload increments
#     InRdmaWrites deterministically:
#       4 KiB -> 1 op/IO, 128 KiB -> 3 ops/IO, 256 KiB -> 5 ops/IO
#     (measured at exactly 1.0000/3.0000/5.0000 ops per IO across three runs)
# A cell whose observed op count deviates from expected by >10% is flagged
# SUSPECT in logs/counter_validation.txt. Deviation is expected to be small but
# nonzero because the counter is host-wide, not per-job.

set -euo pipefail

BASE=/root/mkp1-d1/t2-reads
LOG=$BASE/logs/run.log
NQN1=mkp2-nvme1
NQN2=mkp2-nvme2
SERIAL1=1c83718a24a71f34fb9e
SERIAL2=9970d8704cad2ba293a6
RDMA_DEV=rocep69s0f0
NIC=ens2f0
MD=/dev/md0
MNT=/mnt/lmcache-stage2
SIZE_PER_NS_G=512
REPS=5
RAMP=5
LAT_RUNTIME=60
BW_RUNTIME=300

mkdir -p "$BASE"/{logs,manifest,fio,counters}

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

# --- 0. Write-op guard -------------------------------------------------------
# Fail closed if this script contains any write-capable fio rw value.
if grep -Eq -- '--rw=(write|randwrite|rw|randrw|trim|randtrim|trimwrite)' "$0"; then
    echo "FATAL: write-capable --rw found in script. Refusing to run." >&2
    exit 2
fi

# --- 1. Resolve namespaces by NQN, verify against known serials -------------
resolve_ns() {
    local nqn=$1
    nvme list-subsys 2>/dev/null | awk -v nqn="$nqn" '
        $0 ~ "NQN="nqn"$" { found=1; next }
        found && /^ \+- nvme/ { split($2, a, " "); print a[1]; exit }
    '
}

FAIL=0
for spec in "$NQN1:$SERIAL1:NS1" "$NQN2:$SERIAL2:NS2"; do
    nqn=${spec%%:*}; rest=${spec#*:}; serial=${rest%%:*}; var=${rest#*:}
    ctrl=$(resolve_ns "$nqn" || true)
    if [ -z "$ctrl" ]; then
        log "FATAL: could not resolve NQN $nqn to a controller"; FAIL=1; continue
    fi
    dev="/dev/${ctrl}n1"
    if [ ! -b "$dev" ]; then
        log "FATAL: $dev is not a block device"; FAIL=1; continue
    fi
    # Verify the serial matches the namespace we intend to read.
    actual=$(nvme id-ctrl "$dev" 2>/dev/null | awk '/^sn *:/ {print $3}')
    if [ "$actual" != "$serial" ]; then
        log "FATAL: $dev serial '$actual' != expected '$serial' for $nqn"; FAIL=1; continue
    fi
    declare "$var=$dev"
    log "resolved $nqn -> $dev (serial $actual verified)"
done

if [ $FAIL -ne 0 ]; then
    log "===== ABORTED: namespace identity checks failed. No I/O issued. ====="
    exit 3
fi

# --- 2. Assert md0 + mount are intact (they must survive this run) ----------
assert_intact() {
    local phase=$1 rc=0
    if ! grep -q '^md0 : active raid0' /proc/mdstat; then
        log "$phase: md0 NOT active"; rc=1
    fi
    if ! findmnt -rn -o SOURCE --mountpoint "$MNT" 2>/dev/null | grep -qFx "$MD"; then
        log "$phase: $MD NOT mounted at $MNT"; rc=1
    fi
    return $rc
}

if ! assert_intact "PRE"; then
    log "===== ABORTED: md0/$MNT not in expected pre-state. No I/O issued. ====="
    exit 4
fi
log "PRE: md0 active, $MD mounted at $MNT"

# --- 3. Manifest -------------------------------------------------------------
{
    echo "timestamp: $(date -Iseconds)"
    echo "hostname_initiator: $(hostname)"
    echo "hostname_target: mkp2 (200.0.0.37)"
    echo "kernel: $(uname -r)"
    echo "cpu: $(grep 'model name' /proc/cpuinfo | head -1 | cut -d: -f2 | xargs)"
    echo "dram: $(free -h | awk '/^Mem:/ {print $2}')"
    echo "fio_version: $(fio --version)"
    echo "nqn1: $NQN1 -> $NS1 (serial $SERIAL1)"
    echo "nqn2: $NQN2 -> $NS2 (serial $SERIAL2)"
    echo "rdma_dev: $RDMA_DEV"
    echo "nic: $NIC"
    echo "size_per_ns: ${SIZE_PER_NS_G}G"
    echo "reps: $REPS"
    echo "lat_runtime_s: $LAT_RUNTIME"
    echo "bw_runtime_s: $BW_RUNTIME"
    echo "ramp_s: $RAMP"
    echo "qd_semantics: QD value is PER-DEVICE iodepth; aggregate outstanding = 2x QD (one job per namespace)"
    echo "nvme_connect_note: --nr-io-queues=16 per controller (irdma ENOMEM at default 128)"
    echo "SCOPE: D1 T2b READ-SIDE ONLY at plan durations/reps. Writes excluded by operator decision."
    echo "NOT CLOSED: T2a integrity pass and T2b write direction. D1 remains OPEN."
} | tee "$BASE/manifest/manifest.txt"

cp "$0" "$BASE/manifest/run_d1_t2_reads.sh" || { log "FATAL: cannot snapshot script"; exit 6; }
SCRIPT_SHA=$(sha256sum "$BASE/manifest/run_d1_t2_reads.sh" | awk '{print $1}') \
    || { log "FATAL: script sha failed"; exit 6; }
echo "script_sha256: $SCRIPT_SHA" >> "$BASE/manifest/manifest.txt"
log "script sha=$SCRIPT_SHA"

ethtool -S "$NIC" > "$BASE/counters/ethtool_pre.txt" 2>/dev/null || true
nvme smart-log "$NS1" > "$BASE/counters/smart_pre_ns1.txt" 2>&1 || true
nvme smart-log "$NS2" > "$BASE/counters/smart_pre_ns2.txt" 2>&1 || true

# --- 4. Counter helpers ------------------------------------------------------
HWC=/sys/class/infiniband/$RDMA_DEV/ports/1/hw_counters

rdma_writes() { cat "$HWC/InRdmaWrites" 2>/dev/null || echo 0; }

# RDMA write-ops the target issues per initiator read, per block size.
# Empirically exact on this rig; see header note.
ops_per_io() {
    case $1 in
        4k)   echo 1 ;;
        128k) echo 3 ;;
        256k) echo 5 ;;
        *)    echo 0 ;;
    esac
}

bs_bytes() {
    case $1 in
        4k)   echo 4096 ;;
        128k) echo 131072 ;;
        256k) echo 262144 ;;
        *)    echo 0 ;;
    esac
}

snap_counters() {
    local tag=$1
    { echo "rdma_writes: $(rdma_writes)"
      echo "nic_rx_bytes_procnetdev: $(awk -v n="$NIC:" '$1==n {print $2}' /proc/net/dev)"
      for f in "$HWC"/*; do
          echo "$(basename "$f"): $(cat "$f" 2>/dev/null || echo NA)"
      done
    } > "$BASE/counters/${tag}.txt"
}

# --- 5. Cell runner ----------------------------------------------------------
# One fio job per namespace, iodepth=QD each. Reads only.
run_cell() {
    local pat=$1 bs=$2 qd=$3 rep=$4 runtime=$5
    local rw out tag
    case $pat in
        seq)  rw=read ;;
        rand) rw=randread ;;
        *) log "FATAL: unknown pattern $pat"; return 9 ;;
    esac
    tag="${pat}_${bs}_qd${qd}_rep${rep}"
    out="$BASE/fio/${tag}.json"

    if [ -f "$out" ]; then
        log "  SKIP $tag (already present)"
        return 0
    fi

    sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true

    snap_counters "${tag}_pre"
    log "  cell $tag runtime=${runtime}s (iodepth=$qd/dev x 2 devs = $((qd*2)) outstanding)"
    if ! fio --group_reporting=1 --output-format=json --output="$out" \
        --rw="$rw" --bs="$bs" --iodepth="$qd" --ioengine=libaio \
        --direct=1 --numjobs=1 \
        --time_based --runtime="$runtime" --ramp_time="$RAMP" \
        --norandommap --gtod_reduce=0 --lat_percentiles=1 \
        --randseed=$((42 + rep)) \
        --name="ns1_${tag}" --filename="$NS1" --size="${SIZE_PER_NS_G}G" \
        --name="ns2_${tag}" --filename="$NS2" --size="${SIZE_PER_NS_G}G" \
        2>>"$BASE/logs/fio.err"; then
        log "  WARN: fio failed for $tag (continuing)"
        echo "$tag" >> "$BASE/logs/failed_cells.txt"
        snap_counters "${tag}_post"
        return 0
    fi
    snap_counters "${tag}_post"

    # Counter validation: fio-reported I/O count vs RDMA write-ops observed.
    local fio_bytes pre_w post_w obs_ops exp_ops verdict
    fio_bytes=$(python3 -c "
import json
d=json.load(open('$out'))
print(sum(j['read']['io_bytes'] for j in d['jobs']))
" 2>/dev/null || echo 0)
    pre_w=$(awk '/^rdma_writes:/ {print $2}' "$BASE/counters/${tag}_pre.txt")
    post_w=$(awk '/^rdma_writes:/ {print $2}' "$BASE/counters/${tag}_post.txt")
    obs_ops=$((post_w - pre_w))
    # fio's io_bytes covers only the measurement window; the counter snapshots
    # bracket ramp_time as well. Scale the expectation by (runtime+ramp)/runtime,
    # which assumes ramp-phase throughput approximates steady state -- hence the
    # +/-15% band rather than a tight one.
    exp_ops=$(python3 -c "
bsb=$(bs_bytes "$bs"); opio=$(ops_per_io "$bs"); fb=$fio_bytes
ramp_scale=($runtime + $RAMP) / $runtime
print(int(fb / bsb * opio * ramp_scale) if bsb else 0)
" 2>/dev/null || echo 0)

    if [ "$exp_ops" -le 0 ]; then
        verdict="NO_EXPECTATION"
    elif [ "$obs_ops" -le 0 ]; then
        verdict="SUSPECT_ZERO_WIRE_OPS"
    else
        verdict=$(python3 -c "
r = $obs_ops / $exp_ops
print('OK' if 0.85 <= r <= 1.15 else 'SUSPECT')
" 2>/dev/null || echo NA)
    fi

    echo "$tag fio_bytes=$fio_bytes expected_ops=$exp_ops observed_ops=$obs_ops verdict=$verdict" \
        >> "$BASE/logs/counter_validation.txt"
    log "    fio=${fio_bytes}B ops exp=${exp_ops} obs=${obs_ops} -> ${verdict}"
    if [ "$verdict" != "OK" ]; then
        echo "$tag $verdict" >> "$BASE/logs/suspect_cells.txt"
    fi
}

# --- 6. Prioritized blocks ---------------------------------------------------
# Ordered so decision-relevant cells land first. Block 1 is the set that does
# not exist today at any duration; blocks 2-3 extend existing 256 KiB coverage
# to plan durations and add 128 KiB.
QDLIST="1 4 16 32 64"

log "===== BLOCK 1: 4 KiB latency, ${LAT_RUNTIME}s x ${REPS} reps ====="
for pat in seq rand; do
    for qd in $QDLIST; do
        for rep in $(seq 1 $REPS); do
            run_cell "$pat" 4k "$qd" "$rep" "$LAT_RUNTIME"
        done
    done
done
log "===== BLOCK 1 complete ====="

log "===== BLOCK 2: 256 KiB bandwidth, ${BW_RUNTIME}s x ${REPS} reps ====="
for pat in seq rand; do
    for qd in $QDLIST; do
        for rep in $(seq 1 $REPS); do
            run_cell "$pat" 256k "$qd" "$rep" "$BW_RUNTIME"
        done
    done
done
log "===== BLOCK 2 complete ====="

log "===== BLOCK 3: 128 KiB bandwidth, ${BW_RUNTIME}s x ${REPS} reps ====="
for pat in seq rand; do
    for qd in $QDLIST; do
        for rep in $(seq 1 $REPS); do
            run_cell "$pat" 128k "$qd" "$rep" "$BW_RUNTIME"
        done
    done
done
log "===== BLOCK 3 complete ====="

# --- 7. Post-state + summary -------------------------------------------------
ethtool -S "$NIC" > "$BASE/counters/ethtool_post.txt" 2>/dev/null || true
nvme smart-log "$NS1" > "$BASE/counters/smart_post_ns1.txt" 2>&1 || true
nvme smart-log "$NS2" > "$BASE/counters/smart_post_ns2.txt" 2>&1 || true

if assert_intact "POST"; then
    log "POST: md0 active, $MD mounted at $MNT (intact)"
else
    log "POST: *** md0/mount state changed unexpectedly *** investigate before trusting results"
fi

log "=== cells completed: $(ls "$BASE/fio" | wc -l) ==="
if [ -f "$BASE/logs/failed_cells.txt" ]; then
    log "=== failed cells: $(wc -l < "$BASE/logs/failed_cells.txt") (see logs/failed_cells.txt) ==="
fi
log "===== RUN COMPLETE ====="
