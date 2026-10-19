#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
#
# Configure the MMGT NVMe-oF target for the four-initiator IPU PoC.
#
# Each row below creates one ACL-restricted subsystem with four serial-pinned
# namespaces, reachable only through the listed Falcon IPU address:
#
#   mmgi0 -> IPU1 -> 200.0.5.2 -> nvme13..16 (NUMA node 1)
#   mmgi1 -> IPU2 -> 200.0.6.2 -> nvme17..20 (NUMA node 1)
#   mmgi2 -> IPU3 -> 200.0.7.2 -> nvme2..5 (NUMA node 0)
#   mmgi3 -> IPU4 -> 200.0.8.2 -> nvme6..9 (NUMA node 0)
#
# Usage on mmgt as root:
#   ./setup_mmgt_nvmeof_4x4_target.sh up
#   ./setup_mmgt_nvmeof_4x4_target.sh status
#   ./setup_mmgt_nvmeof_4x4_target.sh down
#
# `up` refuses media that is mounted, has a holder, an on-media signature, or
# belongs to an md array. It never formats or mounts a namespace.
set -euo pipefail

NVMET=/sys/kernel/config/nvmet
NQN_PREFIX=nqn.2026-09.io.lmcache.mmg
TRSVCID=4420
SPDK_SOCKET=${SPDK_SOCKET:-/var/tmp/mmgt-spdk-nvmf.sock}
SPDK_PID_FILE=${SPDK_PID_FILE:-/run/mmgt-spdk-nvmf.pid}

# port-id, subsystem suffix, target IPU address, allowed host NQN, four serials
TARGETS="
1 ipu1-mmgi0 200.0.5.2 nqn.2014-08.org.nvmexpress:uuid:4c4c4544-004a-3810-8056-b5c04f375933 PHCP419600371P9AGN PHCP4195005J1P9AGN PHCP419600FQ1P9AGN PHCP4195003K1P9AGN
2 ipu2-mmgi1 200.0.6.2 nqn.2014-08.org.nvmexpress:uuid:4c4c4544-004a-3810-8056-b9c04f375933 PHCP420300541P9AGN PHCP419600B11P9AGN PHCP419600AY1P9AGN PHCP4196005Q1P9AGN
3 ipu3-mmgi2 200.0.7.2 nqn.2014-08.org.nvmexpress:uuid:4c4c4544-0059-3310-8051-c6c04f325a33 PHCP4380000Y1P9AGN PHCP433400721P9AGN PHCP4334001N1P9AGN PHCP4334002V1P9AGN
4 ipu4-mmgi3 200.0.8.2 nqn.2014-08.org.nvmexpress:uuid:4c4c4544-004a-3810-8056-b8c04f375933 PHCP438000041P9AGN PHCP4321000P1P9AGN PHCP4334005X1P9AGN PHCP4321004H1P9AGN
"

device_for_serial() {
    local want=$1 device serial
    for device in /dev/nvme*n1; do
        [ -b "$device" ] || continue
        serial=$(nvme id-ctrl "$device" 2>/dev/null |
            awk -F: '/^sn / {gsub(/ /, "", $2); print $2}')
        if [ "$serial" = "$want" ]; then
            printf '%s\n' "$device"
            return 0
        fi
    done
    return 1
}

assert_safe_device() {
    local device=$1 block
    block=${device##*/}
    if findmnt -rn -S "$device" >/dev/null; then
        echo "ABORT: $device is mounted"
        exit 1
    fi
    if [ -n "$(ls -A "/sys/class/block/$block/holders" 2>/dev/null)" ]; then
        echo "ABORT: $device has a block holder"
        exit 1
    fi
    if wipefs -n "$device" | tail -n +2 | grep -q .; then
        echo "ABORT: $device has an on-media signature"
        exit 1
    fi
}

assert_no_local_md() {
    local arrays
    arrays=$(awk '/^md[0-9]+/ {print $1}' /proc/mdstat)
    [ -z "$arrays" ] ||
        { echo "ABORT: local md array(s) exist: $arrays"; exit 1; }
}

assert_target_addresses() {
    local _port _suffix address _host _serials
    while read -r _port _suffix address _host _serials; do
        [ -n "$_port" ] || continue
        ip -o -4 addr show | awk '{print $4}' | grep -qx "$address/24" ||
            { echo "ABORT: target address $address/24 is not configured"; exit 1; }
    done <<< "$TARGETS"
}

assert_no_spdk_target() {
    local pid
    command -v pgrep >/dev/null ||
        { echo "ABORT: pgrep utility is absent"; exit 1; }
    [ ! -S "$SPDK_SOCKET" ] ||
        { echo "ABORT: SPDK target socket exists: $SPDK_SOCKET"; exit 1; }
    if [ -e "$SPDK_PID_FILE" ]; then
        pid=$(cat "$SPDK_PID_FILE" 2>/dev/null || true)
        [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null &&
            { echo "ABORT: SPDK target pid $pid is active"; exit 1; }
        echo "ABORT: SPDK target pid file exists: $SPDK_PID_FILE"
        exit 1
    fi
    if pgrep -x nvmf_tgt >/dev/null || pgrep -x spdk_tgt >/dev/null; then
        echo "ABORT: SPDK NVMf target process is active"
        exit 1
    fi
}

preflight() {
    local _port _suffix _address _host serial device
    assert_no_local_md
    assert_target_addresses
    while read -r _port _suffix _address _host serial; do
        [ -n "$_port" ] || continue
        for serial in $serial; do
            device=$(device_for_serial "$serial") ||
                { echo "ABORT: Solidigm serial $serial is absent"; exit 1; }
            assert_safe_device "$device"
        done
    done <<< "$TARGETS"
}

assert_port_matches_or_empty() {
    local port=$1 address=$2 pdir=$NVMET/ports/$1 actual
    [ -d "$pdir" ] || return 0
    [ -z "$(ls -A "$pdir/subsystems" 2>/dev/null)" ] || {
        actual=$(cat "$pdir/addr_traddr" 2>/dev/null)
        [ "$actual" = "$address" ] ||
            { echo "ABORT: port $port is already linked at $actual, not $address"; exit 1; }
    }
}

up() {
    assert_no_spdk_target
    preflight
    modprobe nvmet
    modprobe nvmet-rdma
    [ -d "$NVMET" ] || { echo "ABORT: $NVMET is unavailable after modprobe"; exit 1; }

    local port suffix address host serials nqn device ns pdir
    while read -r port suffix address host serials; do
        [ -n "$port" ] || continue
        nqn="$NQN_PREFIX:$suffix"
        assert_port_matches_or_empty "$port" "$address"
        mkdir -p "$NVMET/hosts/$host" "$NVMET/subsystems/$nqn"
        echo 0 > "$NVMET/subsystems/$nqn/attr_allow_any_host"
        ln -sfn "$NVMET/hosts/$host" "$NVMET/subsystems/$nqn/allowed_hosts/$host"

        ns=1
        for serial in $serials; do
            device=$(device_for_serial "$serial")
            mkdir -p "$NVMET/subsystems/$nqn/namespaces/$ns"
            echo 0 > "$NVMET/subsystems/$nqn/namespaces/$ns/enable" 2>/dev/null || true
            printf '%s' "$device" > "$NVMET/subsystems/$nqn/namespaces/$ns/device_path"
            echo 1 > "$NVMET/subsystems/$nqn/namespaces/$ns/enable"
            printf '  %s ns%s -> %s (serial %s)\n' "$nqn" "$ns" "$device" "$serial"
            ns=$((ns + 1))
        done

        pdir=$NVMET/ports/$port
        mkdir -p "$pdir"
        printf '%s' ipv4 > "$pdir/addr_adrfam"
        printf '%s' "$address" > "$pdir/addr_traddr"
        printf '%s' "$TRSVCID" > "$pdir/addr_trsvcid"
        printf '%s' rdma > "$pdir/addr_trtype"
        ln -sfn "$NVMET/subsystems/$nqn" "$pdir/subsystems/$nqn"
    done <<< "$TARGETS"
    status
}

status() {
    [ -d "$NVMET" ] || { echo "nvmet configfs is absent"; return; }
    local port suffix address host _serials nqn ns
    while read -r port suffix address host _serials; do
        [ -n "$port" ] || continue
        nqn="$NQN_PREFIX:$suffix"
        printf 'port %s %s:%s -> %s (host %s): ' \
            "$port" "$address" "$TRSVCID" "$nqn" "$host"
        for ns in "$NVMET/subsystems/$nqn"/namespaces/*; do
            [ -d "$ns" ] || continue
            printf 'ns%s=%s ' "${ns##*/}" "$(cat "$ns/device_path")"
        done
        echo
    done <<< "$TARGETS"
}

down() {
    [ -d "$NVMET" ] || { echo "nvmet configfs is absent"; return; }
    local port suffix _address host _serials nqn ns pdir
    while read -r port suffix _address host _serials; do
        [ -n "$port" ] || continue
        nqn="$NQN_PREFIX:$suffix"
        pdir=$NVMET/ports/$port
        rm -f "$pdir/subsystems/$nqn"
        rmdir "$pdir" 2>/dev/null || true
        for ns in "$NVMET/subsystems/$nqn"/namespaces/*; do
            [ -d "$ns" ] || continue
            echo 0 > "$ns/enable" 2>/dev/null || true
            rmdir "$ns"
        done
        rm -f "$NVMET/subsystems/$nqn/allowed_hosts/$host"
        rmdir "$NVMET/subsystems/$nqn" 2>/dev/null || true
        rmdir "$NVMET/hosts/$host" 2>/dev/null || true
    done <<< "$TARGETS"
}

case ${1:-} in
    up) up ;;
    down) down ;;
    status) status ;;
    *) echo "usage: $0 up|down|status"; exit 2 ;;
esac
