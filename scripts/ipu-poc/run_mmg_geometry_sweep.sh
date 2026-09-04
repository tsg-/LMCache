#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Sweep FIO workload, per-device numjobs, and iodepth across two initiators.
# FIO_DEVICES must name the raw devices deliberately. Mixed and write
# workloads also require an exact live serial allowlist on both initiators.

set -uo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
INVENTORY=${INVENTORY:-"$SCRIPT_DIR/inventories/mmg-two-initiator.env"}
read -r -a WORKLOADS <<<"${FIO_WORKLOADS:-read}"
NJ_LIST=(1 2 4 8 16)
QD_LIST=(1 2 4 8 16 32)
read -r -a FIO_DEVICE_LIST <<<"${FIO_DEVICES:-}"
JOB_SIZE_GIB=${JOB_SIZE_GIB:-128}
FIO_RUNTIME=${FIO_RUNTIME:-60}
FIO_RAMP=${FIO_RAMP:-10}
FIO_CPUS_ALLOWED=${FIO_CPUS_ALLOWED:-1,3,5,7,9,11,13,15,17,19,21,23,25,27,29,31,33,35,37,39,41,43,45,47,49,51,53,55,57,59,61,63,65,67,69,71,73,75,77,79,81,83,85,87,89,91,93,95,97,99,101,103,105,107,109,111,113,115,117,119,121,123,125,127}
FIO_ALLOW_SERIALS=${FIO_ALLOW_SERIALS:-}

usage() {
    cat <<'EOF'
Usage:
  FIO_DEVICES="/dev/fio-device-a /dev/fio-device-b" ./run_mmg_geometry_sweep.sh [output-dir]

Optional environment:
  INVENTORY=scripts/ipu-poc/inventories/mmg-two-initiator.env
                                      Host-only initiator/target inventory.
  FIO_WORKLOADS="read"               Space-separated: read, mixed, write.
  FIO_ALLOW_SERIALS="serial1,serial2" Required for mixed or write.
  ALLOW_DESTRUCTIVE_WRITE=1          Required for mixed or write.
  JOB_SIZE_GIB=128                   Size of each per-job range.
  FIO_RUNTIME=60 FIO_RAMP=10         FIO duration settings in seconds.
  FIO_CPUS_ALLOWED="1,3,..."         CPUs assigned to FIO jobs.

The script runs FIO on every initiator in the inventory. It refuses mixed/write
workloads unless every requested device has an exact allowed serial and is
unmounted with no block-device holders on every initiator.
EOF
}

log() {
    printf '[%s] %s\n' "$(date -u +%FT%TZ)" "$*" >&2
}

die() {
    log "ERROR: $*"
    exit 1
}

validate_hostname() {
    [[ $1 =~ ^[[:alnum:]][[:alnum:].-]*$ ]]
}

load_inventory() {
    local host

    [[ -f $INVENTORY ]] || die "inventory not found: $INVENTORY"
    unset INITIATOR_HOSTS TARGET_HOST BASE_PATH PYTHON LMCACHE_VENV
    # shellcheck disable=SC1090
    source "$INVENTORY"

    declare -p INITIATOR_HOSTS >/dev/null 2>&1 ||
        die "inventory must define INITIATOR_HOSTS=(...)"
    [[ ${#INITIATOR_HOSTS[@]} -gt 0 ]] ||
        die "INITIATOR_HOSTS must not be empty"
    [[ -n ${TARGET_HOST:-} ]] || die "inventory must define TARGET_HOST"
    if [[ -n ${BASE_PATH+x} || -n ${PYTHON+x} || -n ${LMCACHE_VENV+x} ]]; then
        die "inventory must contain host names only"
    fi

    for host in "${INITIATOR_HOSTS[@]}" "$TARGET_HOST"; do
        validate_hostname "$host" || die "invalid hostname in inventory: $host"
    done
}

write_job() {
    local job_file=$1
    local workload=$2
    local numjobs=$3
    local iodepth=$4
    local device job offset_bytes
    local job_size_bytes=$((JOB_SIZE_GIB * 1024 * 1024 * 1024))

    {
        printf '[global]\n'
        printf 'ioengine=libaio\n'
        printf 'direct=1\n'
        printf 'thread=1\n'
        printf 'time_based=1\n'
        printf 'runtime=%s\n' "$FIO_RUNTIME"
        printf 'ramp_time=%s\n' "$FIO_RAMP"
        printf 'group_reporting=0\n'
        printf 'cpus_allowed=%s\n' "$FIO_CPUS_ALLOWED"
        printf 'cpus_allowed_policy=split\n'
        printf 'rw=%s\n' "$([[ $workload == mixed ]] && printf rw || printf '%s' "$workload")"
        printf 'bs=1M\n'
        printf 'iodepth=%s\n' "$iodepth"
        if [[ $workload == mixed ]]; then
            printf 'rwmixread=83\n'
        fi
        printf '\n'

        for device in "${FIO_DEVICE_LIST[@]}"; do
            for ((job = 0; job < numjobs; job++)); do
                offset_bytes=$((job * job_size_bytes))
                printf '[%s-j%s]\n' "$(basename "$device")" "$job"
                printf 'filename=%s\n' "$device"
                printf 'offset=%s\n' "$offset_bytes"
                printf 'size=%s\n\n' "$job_size_bytes"
            done
        done
    } >"$job_file"
}

mmgt_alive() {
    ssh -o ConnectTimeout=10 "$TARGET_HOST" 'true' >/dev/null 2>&1
}

check_dmesg_redflags() {
    ssh "$TARGET_HOST" \
        "dmesg --since '@$1' | grep -Ei 'irdma|IOMMU|DMAR|AER|reset|fatal' || true"
}

serial_is_allowed() {
    local serial=$1

    [[ -n $serial && ,$FIO_ALLOW_SERIALS, == *",$serial,"* ]]
}

validate_destructive_devices() {
    local host device block report serial mounts holders

    [[ ${ALLOW_DESTRUCTIVE_WRITE:-0} == 1 ]] ||
        die "set ALLOW_DESTRUCTIVE_WRITE=1 for mixed or write workloads"
    [[ -n $FIO_ALLOW_SERIALS ]] ||
        die "set FIO_ALLOW_SERIALS to exact comma-separated device serials"

    for host in "${INITIATOR_HOSTS[@]}"; do
        for device in "${FIO_DEVICE_LIST[@]}"; do
            block=${device##*/}
            report=$(ssh "$host" "
                serial=\$(cat /sys/class/block/$block/device/serial 2>/dev/null) ||
                    exit 1
                mounts=\$(lsblk -nrpo MOUNTPOINT /dev/$block 2>/dev/null |
                    sed '/^$/d' | paste -sd, -)
                holders=\$(find /sys/class/block/$block/holders -mindepth 1 \
                    -maxdepth 1 -printf '%f,' 2>/dev/null)
                printf '%s\t%s\t%s\n' \"\$serial\" \"\$mounts\" \"\$holders\"
            ") || die "$host: cannot read safety state for $device"

            IFS=$'\t' read -r serial mounts holders <<<"$report"
            serial_is_allowed "$serial" ||
                die "$host: $device serial '$serial' is not in FIO_ALLOW_SERIALS"
            [[ -z $mounts ]] || die "$host: $device is mounted at $mounts"
            [[ -z $holders ]] || die "$host: $device has holders: $holders"
        done
    done
}

validate_inputs() {
    local workload device
    local needs_write_guard=0

    load_inventory

    [[ ${#FIO_DEVICE_LIST[@]} -gt 0 ]] ||
        die "set FIO_DEVICES to explicit raw device paths"
    [[ $JOB_SIZE_GIB =~ ^[1-9][0-9]*$ ]] ||
        die "JOB_SIZE_GIB must be a positive integer"
    [[ $FIO_RUNTIME =~ ^[1-9][0-9]*$ ]] ||
        die "FIO_RUNTIME must be a positive integer"
    [[ $FIO_RAMP =~ ^[0-9]+$ ]] || die "FIO_RAMP must be a non-negative integer"

    for device in "${FIO_DEVICE_LIST[@]}"; do
        [[ $device =~ ^/dev/[[:alnum:]_.-]+$ ]] ||
            die "invalid device path: $device"
    done

    for workload in "${WORKLOADS[@]}"; do
        case $workload in
        read) ;;
        mixed | write) needs_write_guard=1 ;;
        *) die "unsupported workload: $workload" ;;
        esac
    done

    if ((needs_write_guard)); then
        validate_destructive_devices
    fi
}

main() {
    local out_dir=${1:-"results/mmg-geometry-sweep-$(date -u +%Y%m%dT%H%M%SZ)"}
    local jobs_dir="$out_dir/jobs"
    local start_epoch
    local workload numjobs iodepth cell job_file initiator rc0 rc1

    [[ $# -le 1 ]] || {
        usage
        exit 2
    }

    validate_inputs
    mkdir -p "$out_dir" "$jobs_dir"
    start_epoch=$(date +%s)

    for workload in "${WORKLOADS[@]}"; do
        for numjobs in "${NJ_LIST[@]}"; do
            for iodepth in "${QD_LIST[@]}"; do
                cell="${workload}-nj${numjobs}-qd${iodepth}"
                job_file="$jobs_dir/$cell.fio"
                write_job "$job_file" "$workload" "$numjobs" "$iodepth"
                log "starting $cell"

                for initiator in "${INITIATOR_HOSTS[@]}"; do
                    scp "$job_file" "$initiator:/root/$cell.fio"
                done

                ssh "${INITIATOR_HOSTS[0]}" "fio --output-format=json --output=/root/$cell.json \
                    /root/$cell.fio" &
                local pid0=$!
                local -a pids=("$pid0")
                for initiator in "${INITIATOR_HOSTS[@]:1}"; do
                    ssh "$initiator" "fio --output-format=json --output=/root/$cell.json \
                        /root/$cell.fio" &
                    pids+=("$!")
                done

                rc0=0
                rc1=0
                wait "$pid0"
                rc0=$?
                for pid in "${pids[@]:1}"; do
                    wait "$pid" || rc1=1
                done

                for initiator in "${INITIATOR_HOSTS[@]}"; do
                    scp "$initiator:/root/$cell.json" "$out_dir/$initiator-$cell.json"
                done

                if ! mmgt_alive; then
                    die "$TARGET_HOST is not reachable after $cell"
                fi
                check_dmesg_redflags "$start_epoch" |
                    tee "$out_dir/$TARGET_HOST-dmesg-$cell.txt"

                if ((rc0 != 0 || rc1 != 0)); then
                    die "FIO failed for $cell (first initiator=$rc0 others=$rc1)"
                fi
            done
        done
    done
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
fi
