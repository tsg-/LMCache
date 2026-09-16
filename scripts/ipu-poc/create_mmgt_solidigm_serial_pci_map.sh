#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
#
# Emit a fresh serial-to-PCI map for the 16 approved Solidigm SSDs on MMGT.
#
# Run before binding any NVMe controller to vfio-pci:
#   ./create_mmgt_solidigm_serial_pci_map.sh \
#     > /etc/spdk/mmgt-4x4-serial-pci.map
#
# The output is intentionally limited to the known 16 POC drives. It does not
# include other Intel/Solidigm-looking devices and never modifies the host.
set -euo pipefail

SERIALS="
PHCP419600371P9AGN
PHCP4195005J1P9AGN
PHCP419600FQ1P9AGN
PHCP4195003K1P9AGN
PHCP420300541P9AGN
PHCP419600B11P9AGN
PHCP419600AY1P9AGN
PHCP4196005Q1P9AGN
PHCP4380000Y1P9AGN
PHCP433400721P9AGN
PHCP4334001N1P9AGN
PHCP4334002V1P9AGN
PHCP438000041P9AGN
PHCP4321000P1P9AGN
PHCP4334005X1P9AGN
PHCP4321004H1P9AGN
"

die() {
    echo "ABORT: $*" >&2
    exit 1
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
    local device=$1 controller bdf
    controller=${device##*/}
    controller=${controller%n1}
    bdf=$(basename "$(dirname "$(dirname "$(readlink -f "/sys/class/nvme/$controller/device")")")")
    [[ "$bdf" =~ ^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$ ]] ||
        die "could not resolve a PCI BDF for $device"
    printf '%s\n' "$bdf"
}

main() {
    local serial device bdf map= seen_bdfs=" "
    [ "$#" -eq 0 ] ||
        die "usage: $0"

    for serial in $SERIALS; do
        device=$(device_for_serial "$serial") ||
            die "approved Solidigm serial $serial is absent"
        bdf=$(pci_for_device "$device")
        case $seen_bdfs in
            *" $bdf "*) die "PCI BDF $bdf resolves to more than one SSD" ;;
        esac
        seen_bdfs="${seen_bdfs}${bdf} "
        map="${map}${serial} ${bdf}"$'\n'
    done

    printf '%s' "$map"
}

main "$@"
