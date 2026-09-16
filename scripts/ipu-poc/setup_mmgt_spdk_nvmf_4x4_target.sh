#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
#
# Configure the MMGT NVMe-oF target with SPDK for the four-initiator IPU PoC.
#
# Before binding the 16 NVMe controllers to vfio-pci, run:
#   ./setup_mmgt_spdk_nvmf_4x4_target.sh prepare > /etc/spdk/mmgt-4x4-serial-pci.map
#
# Review that map and perform the required host provisioning separately:
# install SPDK, enable IOMMU, and bind only the listed PCI functions to
# vfio-pci. This script never changes boot configuration, binds PCI devices,
# formats media, or removes existing on-media metadata.
#
# Usage on mmgt as root:
#   SPDK_MAP=/etc/spdk/mmgt-4x4-serial-pci.map \
#     ./setup_mmgt_spdk_nvmf_4x4_target.sh preflight
#   SPDK_MAP=/etc/spdk/mmgt-4x4-serial-pci.map \
#     ./setup_mmgt_spdk_nvmf_4x4_target.sh up
#   ./setup_mmgt_spdk_nvmf_4x4_target.sh status
#   ./setup_mmgt_spdk_nvmf_4x4_target.sh down
set -euo pipefail

NQN_PREFIX=nqn.2026-09.io.lmcache.mmg
TRSVCID=4420
SPDK_TGT=${SPDK_TGT:-/usr/local/bin/nvmf_tgt}
SPDK_RPC=${SPDK_RPC:-/usr/local/bin/rpc.py}
SPDK_SOCKET=${SPDK_SOCKET:-/var/tmp/mmgt-spdk-nvmf.sock}
SPDK_PID_FILE=${SPDK_PID_FILE:-/run/mmgt-spdk-nvmf.pid}
SPDK_LOG=${SPDK_LOG:-/var/log/mmgt-spdk-nvmf.log}
SPDK_MAP=${SPDK_MAP:-/etc/spdk/mmgt-4x4-serial-pci.map}
SPDK_RUNTIME_MIN_KB=${SPDK_RUNTIME_MIN_KB:-102400}
SPDK_RUNTIME_MIN_INODES=${SPDK_RUNTIME_MIN_INODES:-64}
SPDK_HUGEPAGES_MIN=${SPDK_HUGEPAGES_MIN:-1024}

# port-id, subsystem suffix, target IPU address, allowed host NQN, four serials
TARGETS="
1 ipu1-mmgi0 200.0.5.2 nqn.2014-08.org.nvmexpress:uuid:4c4c4544-004a-3810-8056-b5c04f375933 PHCP419600371P9AGN PHCP4195005J1P9AGN PHCP419600FQ1P9AGN PHCP4195003K1P9AGN
2 ipu2-mmgi1 200.0.6.2 nqn.2014-08.org.nvmexpress:uuid:4c4c4544-004a-3810-8056-b9c04f375933 PHCP420300541P9AGN PHCP419600B11P9AGN PHCP419600AY1P9AGN PHCP4196005Q1P9AGN
3 ipu3-mmgi2 200.0.7.2 nqn.2014-08.org.nvmexpress:uuid:4c4c4544-0059-3310-8051-c6c04f325a33 PHCP4380000Y1P9AGN PHCP433400721P9AGN PHCP4334001N1P9AGN PHCP4334002V1P9AGN
4 ipu4-mmgi3 200.0.8.2 nqn.2014-08.org.nvmexpress:uuid:4c4c4544-004a-3810-8056-b8c04f375933 PHCP438000041P9AGN PHCP4321000P1P9AGN PHCP4334005X1P9AGN PHCP4321004H1P9AGN
"

die() {
    echo "ABORT: $*" >&2
    exit 1
}

expected_serial() {
    local _port _suffix _address _host serial
    while read -r _port _suffix _address _host serial; do
        [ -n "$_port" ] || continue
        for serial in $serial; do
            [ "$serial" = "$1" ] && return 0
        done
    done <<< "$TARGETS"
    return 1
}

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

pci_for_device() {
    local device=$1 controller
    controller=${device##*/}
    controller=${controller%n1}
    basename "$(dirname "$(dirname "$(readlink -f "/sys/class/nvme/$controller/device")")")"
}

assert_target_addresses() {
    local _port _suffix address _host _serials
    while read -r _port _suffix address _host _serials; do
        [ -n "$_port" ] || continue
        ip -o -4 addr show | awk '{print $4}' | grep -qx "$address/24" ||
            die "target address $address/24 is not configured"
    done <<< "$TARGETS"
}

assert_no_kernel_target() {
    local configfs=/sys/kernel/config/nvmet
    if systemctl is-active --quiet nvmet.service; then
        die "kernel nvmet.service is active; stop it before SPDK"
    fi
    if systemctl is-enabled --quiet nvmet.service; then
        die "kernel nvmet.service is enabled; disable it before SPDK"
    fi
    [ ! -d "$configfs/subsystems" ] ||
        [ -z "$(ls -A "$configfs/subsystems" 2>/dev/null)" ] ||
        die "kernel nvmet subsystems exist; run the kernel target down first"
    [ ! -d "$configfs/ports" ] ||
        [ -z "$(ls -A "$configfs/ports" 2>/dev/null)" ] ||
        die "kernel nvmet ports exist; run the kernel target down first"
}

assert_map() {
    local line serial bdf count
    [ -f "$SPDK_MAP" ] ||
        die "serial-to-PCI map $SPDK_MAP is absent; run prepare before binding"

    count=0
    while read -r line; do
        [ -z "$line" ] && continue
        case $line in \#*) continue ;; esac
        set -- $line
        [ "$#" -eq 2 ] || die "invalid map row: $line"
        serial=$1
        bdf=$2
        expected_serial "$serial" || die "unexpected serial in map: $serial"
        [[ "$bdf" =~ ^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$ ]] ||
            die "invalid PCI BDF for $serial: $bdf"
        count=$((count + 1))
    done < "$SPDK_MAP"
    [ "$count" -eq 16 ] || die "map must contain exactly 16 serial-to-PCI rows"

    awk '
        /^[[:space:]]*($|#)/ { next }
        { serial[$1]++; bdf[$2]++ }
        END {
            for (item in serial) if (serial[item] != 1) exit 1
            for (item in bdf) if (bdf[item] != 1) exit 1
        }
    ' "$SPDK_MAP" || die "map contains a duplicate serial or PCI BDF"

    local _port _suffix _address _host serial mapped
    while read -r _port _suffix _address _host serial; do
        [ -n "$_port" ] || continue
        for serial in $serial; do
            mapped=$(awk -v serial="$serial" '$1 == serial {print $2}' "$SPDK_MAP")
            [ -n "$mapped" ] || die "map lacks expected serial $serial"
        done
    done <<< "$TARGETS"
    echo "validated serial-to-PCI map: 16 rows, 16 unique serials, 16 unique PCI BDFs"
}

bdf_for_serial() {
    awk -v serial="$1" '$1 == serial {print $2}' "$SPDK_MAP"
}

leaf_blocks() {
    local block=$1 child has_child=0
    for child in "/sys/class/block/$block"/slaves/*; do
        [ -e "$child" ] || continue
        has_child=1
        leaf_blocks "${child##*/}"
    done
    [ "$has_child" -eq 1 ] || printf '%s\n' "$block"
}

pci_for_block() {
    local block=$1 path bdf
    path=$(readlink -f "/sys/class/block/$block/device") ||
        die "could not resolve sysfs device for $block"
    while [ "$path" != / ]; do
        bdf=${path##*/}
        [[ "$bdf" =~ ^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$ ]] &&
            { printf '%s\n' "$bdf"; return; }
        path=$(dirname "$path")
    done
    die "could not resolve a PCI BDF for block device $block"
}

assert_os_storage_excluded() {
    local mountpoint source source_block leaf protected_bdf serial mapped_bdf
    local protected_bdfs=" "
    local _port _suffix _address _host serials
    for mountpoint in / /home /var; do
        source=$(findmnt -nro SOURCE --target "$mountpoint") ||
            die "could not resolve the backing device for $mountpoint"
        source=$(readlink -f "$source")
        [ -b "$source" ] ||
            die "$mountpoint is backed by non-block source $source"
        source_block=${source##*/}
        while read -r leaf; do
            protected_bdf=$(pci_for_block "$leaf")
            protected_bdfs="${protected_bdfs}${protected_bdf} "
        done < <(leaf_blocks "$source_block")
    done

    while read -r _port _suffix _address _host serials; do
        [ -n "$_port" ] || continue
        for serial in $serials; do
            mapped_bdf=$(bdf_for_serial "$serial" | tr '[:upper:]' '[:lower:]')
            case $protected_bdfs in
                *" $mapped_bdf "*)
                    die "map assigns $serial to $mapped_bdf, backing /, /home, or /var"
                    ;;
            esac
        done
    done <<< "$TARGETS"
    echo "validated serial-to-PCI map excludes storage backing /, /home, and /var"
}

assert_runtime_directory() {
    local path=$1 purpose=$2 directory avail_kb avail_inodes
    directory=$(dirname "$path")
    [ -d "$directory" ] || die "$purpose directory is absent: $directory"
    [ -w "$directory" ] || die "$purpose directory is not writable: $directory"
    avail_kb=$(df -Pk "$directory" | awk 'NR == 2 {print $4}')
    avail_inodes=$(df -Pi "$directory" | awk 'NR == 2 {print $4}')
    [[ "$avail_kb" =~ ^[0-9]+$ ]] ||
        die "could not determine free space for $purpose directory $directory"
    [[ "$avail_inodes" =~ ^[0-9]+$ ]] ||
        die "could not determine free inodes for $purpose directory $directory"
    [ "$avail_kb" -ge "$SPDK_RUNTIME_MIN_KB" ] ||
        die "$purpose directory $directory has ${avail_kb} KiB free; need ${SPDK_RUNTIME_MIN_KB} KiB"
    [ "$avail_inodes" -ge "$SPDK_RUNTIME_MIN_INODES" ] ||
        die "$purpose directory $directory has ${avail_inodes} free inodes; need ${SPDK_RUNTIME_MIN_INODES}"
}

assert_runtime_storage() {
    assert_runtime_directory "$SPDK_LOG" "SPDK log"
    assert_runtime_directory "$SPDK_SOCKET" "SPDK socket"
    assert_runtime_directory "$SPDK_PID_FILE" "SPDK pid file"
    echo "validated runtime storage and inode headroom"
}

assert_hugepages() {
    local total free
    mountpoint -q /dev/hugepages ||
        die "/dev/hugepages is not a hugetlbfs mount"
    [ "$(findmnt -nro FSTYPE --target /dev/hugepages)" = hugetlbfs ] ||
        die "/dev/hugepages is not backed by hugetlbfs"
    total=$(awk '/^HugePages_Total:/ {print $2}' /proc/meminfo)
    free=$(awk '/^HugePages_Free:/ {print $2}' /proc/meminfo)
    [[ "$total" =~ ^[0-9]+$ && "$free" =~ ^[0-9]+$ ]] ||
        die "could not determine hugepage availability"
    [ "$total" -ge "$SPDK_HUGEPAGES_MIN" ] ||
        die "only $total hugepages configured; need $SPDK_HUGEPAGES_MIN"
    [ "$free" -ge "$SPDK_HUGEPAGES_MIN" ] ||
        die "only $free free hugepages; need $SPDK_HUGEPAGES_MIN"
    echo "validated $free free hugepages (minimum $SPDK_HUGEPAGES_MIN)"
}

assert_rdma_endpoints() {
    local _port _suffix address _host _serials netdev
    command -v rdma >/dev/null ||
        die "rdma utility is absent; install rdma-core"
    while read -r _port _suffix address _host _serials; do
        [ -n "$_port" ] || continue
        netdev=$(ip -o -4 addr show |
            awk -v address="$address/24" '$4 == address {print $2; exit}')
        [ -n "$netdev" ] ||
            die "could not resolve a netdev for target address $address"
        [ "$(cat "/sys/class/net/$netdev/operstate")" = up ] ||
            die "target address $address is on $netdev, which is not up"
        rdma link show |
            awk -v netdev="$netdev" '
                $0 ~ ("netdev " netdev "($| )") && $0 ~ /state ACTIVE/ {
                    active = 1
                }
                END { exit !active }
            ' ||
            die "target address $address on $netdev lacks an active RDMA link"
        echo "validated active RDMA endpoint $address on $netdev"
    done <<< "$TARGETS"
}

assert_spdk_dependencies() {
    [ -x "$SPDK_TGT" ] || die "SPDK target binary is not executable: $SPDK_TGT"
    [ -x "$SPDK_RPC" ] || die "SPDK RPC helper is not executable: $SPDK_RPC"
    "$SPDK_TGT" --help >/dev/null 2>&1 ||
        die "SPDK target binary cannot run: $SPDK_TGT"
    "$SPDK_RPC" --help >/dev/null 2>&1 ||
        die "SPDK RPC helper cannot run: $SPDK_RPC"
    command -v findmnt >/dev/null || die "findmnt utility is absent"
    command -v mountpoint >/dev/null || die "mountpoint utility is absent"
    echo "validated SPDK target, RPC helper, and host utilities"
}

assert_iommu_and_vfio() {
    local serial bdf driver group group_device group_driver
    local _port _suffix _address _host serials
    grep -Eq '(^|[[:space:]])intel_iommu=on([,[:space:]]|$)' /proc/cmdline ||
        die "intel_iommu=on is absent from /proc/cmdline"
    [ -d /sys/kernel/iommu_groups ] &&
        [ -n "$(find /sys/kernel/iommu_groups -mindepth 1 -maxdepth 1 -type d -print -quit)" ] ||
        die "IOMMU groups are unavailable; enable IOMMU and reboot before SPDK"

    while read -r _port _suffix _address _host serials; do
        [ -n "$_port" ] || continue
        for serial in $serials; do
            bdf=$(bdf_for_serial "$serial")
            [ -e "/sys/bus/pci/devices/$bdf" ] ||
                die "mapped PCI device $bdf for $serial is absent"
            driver=$(basename "$(readlink -f "/sys/bus/pci/devices/$bdf/driver")")
            [ "$driver" = vfio-pci ] ||
                die "$bdf for $serial is bound to $driver, not vfio-pci"
            group=$(readlink -f "/sys/bus/pci/devices/$bdf/iommu_group")
            [ -d "$group/devices" ] ||
                die "$bdf for $serial has no usable IOMMU group"
            for group_device in "$group"/devices/*; do
                group_driver=$(basename "$(readlink -f "$group_device/driver")")
                [ "$group_driver" = vfio-pci ] ||
                    die "IOMMU group ${group##*/} includes ${group_device##*/}, bound to $group_driver"
            done
        done
    done <<< "$TARGETS"
}

prepare() {
    local _port _suffix _address _host serial device bdf
    echo "# Review this map before saving it to SPDK_MAP and binding vfio-pci."
    echo "# serial pci_bdf"
    while read -r _port _suffix _address _host serial; do
        [ -n "$_port" ] || continue
        for serial in $serial; do
            device=$(device_for_serial "$serial") ||
                die "Solidigm serial $serial is absent"
            bdf=$(pci_for_device "$device")
            printf '%s %s\n' "$serial" "$bdf"
        done
    done <<< "$TARGETS"
}

preflight() {
    [ ! -e "$SPDK_SOCKET" ] || die "SPDK socket already exists: $SPDK_SOCKET"
    [ ! -e "$SPDK_PID_FILE" ] || die "SPDK pid file already exists: $SPDK_PID_FILE"
    assert_map
    assert_os_storage_excluded
    assert_runtime_storage
    assert_hugepages
    assert_rdma_endpoints
    assert_spdk_dependencies
    assert_target_addresses
    assert_no_kernel_target
    assert_iommu_and_vfio
}

rpc() {
    "$SPDK_RPC" -s "$SPDK_SOCKET" "$@"
}

cleanup_failed_up() {
    local pid
    [ -f "$SPDK_PID_FILE" ] || return
    pid=$(cat "$SPDK_PID_FILE")
    kill "$pid" 2>/dev/null || true
    rm -f "$SPDK_PID_FILE"
}

up() {
    local port suffix address host serials serial bdf bdev ns nqn attempt
    preflight
    trap cleanup_failed_up ERR
    "$SPDK_TGT" -r "$SPDK_SOCKET" > "$SPDK_LOG" 2>&1 &
    echo "$!" > "$SPDK_PID_FILE"

    for attempt in $(seq 1 50); do
        [ -S "$SPDK_SOCKET" ] && break
        sleep 0.1
    done
    [ -S "$SPDK_SOCKET" ] || die "SPDK target did not create $SPDK_SOCKET"

    rpc nvmf_create_transport -t RDMA
    while read -r port suffix address host serials; do
        [ -n "$port" ] || continue
        nqn="$NQN_PREFIX:$suffix"
        rpc nvmf_create_subsystem "$nqn" -s "mmgt-$suffix"
        rpc nvmf_subsystem_add_listener "$nqn" -t RDMA -a "$address" -s "$TRSVCID"
        rpc nvmf_subsystem_add_host "$nqn" "$host"
        ns=1
        for serial in $serials; do
            bdf=$(bdf_for_serial "$serial")
            bdev="mmgt_${port}_${ns}"
            rpc bdev_nvme_attach_controller -b "$bdev" -t PCIe -a "$bdf"
            rpc nvmf_subsystem_add_ns "$nqn" "${bdev}n1"
            ns=$((ns + 1))
        done
    done <<< "$TARGETS"
    trap - ERR
    status
}

status() {
    [ -S "$SPDK_SOCKET" ] ||
        { echo "SPDK target socket is absent: $SPDK_SOCKET"; return; }
    rpc nvmf_get_subsystems
}

down() {
    local pid
    [ -f "$SPDK_PID_FILE" ] ||
        { echo "SPDK target pid file is absent: $SPDK_PID_FILE"; return; }
    pid=$(cat "$SPDK_PID_FILE")
    kill "$pid"
    wait "$pid" 2>/dev/null || true
    rm -f "$SPDK_PID_FILE"
}

case ${1:-} in
    prepare) prepare ;;
    preflight) preflight ;;
    up) up ;;
    down) down ;;
    status) status ;;
    *) echo "usage: $0 prepare|preflight|up|down|status"; exit 2 ;;
esac
