#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
#
# Build or tear down the mmgt NVMe-oF target export over Falcon RDMA: eight
# Solidigm namespaces across two RDMA ports, one port per initiator.
#
# The export lives entirely in configfs and does not survive a reboot. It was
# hand-built, and the mkp2 equivalent (setup_nvmeof_target.sh) was lost twice to
# reboots at a cost of a bring-up session each time. Run this instead of
# rebuilding by hand.
#
# Three invariants this script enforces:
#
# 1. NAMESPACES ARE BOUND BY SERIAL, NOT BY DEVICE NAME. The subsystem NQNs are
#    named after the kernel device names they had when first built
#    (nqn...:nvme1n1), but those names are not stable across reboots. Binding by
#    name would silently export a different physical drive and quietly invalidate
#    every result doc that refers to these NQNs. The serials below were captured
#    2026-08-29 from the live config.
#
# 2. PORT ASSIGNMENT IS NOT ARBITRARY. Port 1 (200.0.6.2) serves mmgi0 and port 2
#    (200.0.5.2) serves mmgi1, because each initiator routes to exactly one of
#    those addresses. Fabric map: docs/design/v1/platform/ipu-poc/
#    instrumentation/README.md, "Falcon host bring-up". Each initiator's RAID0
#    spans the four namespaces on its own port; crossing the split hands an
#    initiator a member set its md superblock does not describe.
#
# 3. NO LOCAL md ARRAY MAY HOLD THESE DRIVES. The RAID0s were created on the
#    initiators, so their superblocks live on mmgt's physical media. mmgt has no
#    mdadm.conf, so a boot-time scan here will assemble them locally as /dev/md12x.
#    If nvmet then exports the same drives they have two independent owners --
#    mmgt's md layer and the initiator's imported md0 -- which corrupts the
#    corpus. `up` refuses to run in that state; see `guard` to prevent it.
#
# Usage (on mmgt, as root):
#   ./setup_mmgt_nvmeof_target.sh up       # load modules, build subsystems + ports
#   ./setup_mmgt_nvmeof_target.sh down     # unexport, delete subsystems (modules stay)
#   ./setup_mmgt_nvmeof_target.sh status
#   ./setup_mmgt_nvmeof_target.sh guard    # write mdadm.conf so a reboot cannot
#                                          # auto-assemble the exported drives
#
# Bring the initiators up separately, from each initiator:
#   quiesce_mmg_initiator.sh up
set -uo pipefail

NVMET=/sys/kernel/config/nvmet
NQN_PREFIX=${NQN_PREFIX:-nqn.2026-08.lab.mmgt}
TRSVCID=${TRSVCID:-4420}

# Port id -> RDMA source address. Port 1 faces mmgi0, port 2 faces mmgi1.
PORT1_TRADDR=${PORT1_TRADDR:-200.0.6.2}
PORT2_TRADDR=${PORT2_TRADDR:-200.0.5.2}

# "<nqn-suffix> <drive-serial> <port-id> <namespace-uuid>", captured 2026-08-29.
# The namespace uuid is preserved so the exported namespace identity, and any
# /dev/disk/by-id path on an initiator that references it, survives a teardown.
EXPORTS="
nvme1n1  PHCP419600371P9AGN 1 69273828-bf13-4a29-9e1c-af3b12b47210
nvme3n1  PHCP4195005J1P9AGN 1 9c7fc7e5-4330-4299-aff6-66b8beba26c2
nvme5n1  PHCP419600FQ1P9AGN 1 7dbc204a-5596-4361-9a04-b2b30d926ed5
nvme7n1  PHCP4195003K1P9AGN 1 c614ea7a-6902-44d1-b6af-c54727d6bf21
nvme9n1  PHCP420300541P9AGN 2 3dca41d9-4b50-4882-9ba2-2c6f2bde8533
nvme11n1 PHCP419600B11P9AGN 2 d9c071f8-03d5-4808-bd6e-cc49f8a3a639
nvme13n1 PHCP419600AY1P9AGN 2 d38c5c2f-da72-4ada-a98d-aee163a787c7
nvme15n1 PHCP4196005Q1P9AGN 2 0d1d7539-cb7e-4312-8a5d-d25c5dd04db9
"

# Resolve a drive serial to its current /dev/nvmeXnY. Empty output means the
# drive is absent, which every caller treats as fatal rather than skippable.
dev_for_serial() {
    local want="$1" d sn
    for d in /dev/nvme[0-9]*n1; do
        [ -b "$d" ] || continue
        sn=$(nvme id-ctrl "$d" 2>/dev/null | awk -F: '/^sn /{gsub(/ /,"",$2); print $2}')
        if [ "$sn" = "$want" ]; then
            echo "$d"
            return 0
        fi
    done
    return 1
}

traddr_for_port() {
    case "$1" in
        1) echo "$PORT1_TRADDR" ;;
        2) echo "$PORT2_TRADDR" ;;
        *) echo "ABORT: unknown port id $1" >&2; return 1 ;;
    esac
}

# Any md array built from the exported drives means mmgt has claimed media the
# initiators own. Report the arrays so the operator can stop them deliberately.
local_md_on_exports() {
    local hits="" md members serial dev
    for md in /dev/md[0-9]*; do
        [ -b "$md" ] || continue
        members=$(ls "/sys/block/$(basename "$md")/slaves" 2>/dev/null | tr '\n' ' ')
        for serial in $(echo "$EXPORTS" | awk 'NF{print $2}'); do
            dev=$(dev_for_serial "$serial") || continue
            case " $members " in
                *" $(basename "$dev") "*) hits="$hits $md" ;;
            esac
        done
    done
    echo "$hits" | tr ' ' '\n' | grep -v '^$' | sort -u | tr '\n' ' '
}

status() {
    if [ ! -d "$NVMET" ]; then
        echo "nvmet: configfs not mounted (module not loaded)"
        return
    fi
    echo "subsystems: $(ls "$NVMET/subsystems" 2>/dev/null | wc -l) defined"
    local p
    for p in "$NVMET"/ports/*; do
        [ -d "$p" ] || continue
        echo "port $(basename "$p"): $(cat "$p/addr_traddr" 2>/dev/null):$(cat "$p/addr_trsvcid" 2>/dev/null) $(cat "$p/addr_trtype" 2>/dev/null), $(ls "$p/subsystems" 2>/dev/null | wc -l) subsystem(s)"
    done
    local md
    md=$(local_md_on_exports)
    if [ -n "$md" ]; then
        echo "WARNING: local md array(s) hold exported drives: $md"
    else
        echo "local md on exported drives: none (correct)"
    fi
}

up() {
    modprobe nvmet || { echo "ABORT: modprobe nvmet failed"; exit 1; }
    modprobe nvmet-rdma || { echo "ABORT: modprobe nvmet-rdma failed"; exit 1; }

    local md
    md=$(local_md_on_exports)
    if [ -n "$md" ]; then
        echo "ABORT: local md array(s) hold the drives about to be exported: $md"
        echo "  Stop them first (mdadm --stop $md), then re-run. Exporting drives"
        echo "  that mmgt's own md layer has claimed corrupts the initiator corpus."
        exit 1
    fi

    local suffix serial portid nsuuid dev nqn port traddr
    while read -r suffix serial portid nsuuid; do
        [ -n "$suffix" ] || continue
        dev=$(dev_for_serial "$serial") ||
            { echo "ABORT: no drive with serial $serial ($suffix)"; exit 1; }
        nqn="$NQN_PREFIX:$suffix"

        mkdir -p "$NVMET/subsystems/$nqn" ||
            { echo "ABORT: cannot create subsystem $nqn"; exit 1; }
        echo 1 > "$NVMET/subsystems/$nqn/attr_allow_any_host"

        mkdir -p "$NVMET/subsystems/$nqn/namespaces/1" ||
            { echo "ABORT: cannot create namespace for $nqn"; exit 1; }
        echo "$nsuuid" > "$NVMET/subsystems/$nqn/namespaces/1/device_uuid"
        echo "$dev" > "$NVMET/subsystems/$nqn/namespaces/1/device_path" ||
            { echo "ABORT: cannot bind $dev to $nqn"; exit 1; }
        echo 1 > "$NVMET/subsystems/$nqn/namespaces/1/enable" ||
            { echo "ABORT: cannot enable namespace for $nqn"; exit 1; }
        echo "  $nqn -> $dev (serial $serial)"
    done <<< "$(echo "$EXPORTS" | awk 'NF')"

    for portid in 1 2; do
        traddr=$(traddr_for_port "$portid") || exit 1
        port="$NVMET/ports/$portid"
        mkdir -p "$port" || { echo "ABORT: cannot create port $portid"; exit 1; }
        echo ipv4 > "$port/addr_adrfam"
        echo "$traddr" > "$port/addr_traddr"
        echo "$TRSVCID" > "$port/addr_trsvcid"
        # trtype last: writing it is what makes nvmet bind the listener, and it
        # fails if the address fields are not already in place.
        echo rdma > "$port/addr_trtype" ||
            { echo "ABORT: cannot set rdma on port $portid ($traddr) -- is the Falcon interface up?"; exit 1; }
        echo "  port $portid listening on $traddr:$TRSVCID"
    done

    while read -r suffix serial portid nsuuid; do
        [ -n "$suffix" ] || continue
        nqn="$NQN_PREFIX:$suffix"
        ln -sf "$NVMET/subsystems/$nqn" "$NVMET/ports/$portid/subsystems/$nqn" ||
            { echo "ABORT: cannot link $nqn to port $portid"; exit 1; }
    done <<< "$(echo "$EXPORTS" | awk 'NF')"

    echo "exported: 8 namespaces, port 1 -> $PORT1_TRADDR (mmgi0), port 2 -> $PORT2_TRADDR (mmgi1)"
    status
}

down() {
    if [ ! -d "$NVMET" ]; then
        echo "nvmet not present; nothing to unexport"
        return
    fi

    # Unlink from the ports first. This drops the listener and refuses new
    # connects; deleting a subsystem an initiator still holds fails with EBUSY.
    local p link nqn s n
    for p in "$NVMET"/ports/*; do
        [ -d "$p" ] || continue
        for link in "$p"/subsystems/*; do
            [ -e "$link" ] || continue
            rm -f "$link" ||
                { echo "ABORT: cannot unlink $(basename "$link") from port $(basename "$p") -- an initiator is probably still connected"; exit 1; }
        done
        rmdir "$p" || { echo "ABORT: cannot remove port $(basename "$p")"; exit 1; }
        echo "removed port $(basename "$p")"
    done

    for s in "$NVMET"/subsystems/*; do
        [ -d "$s" ] || continue
        nqn=$(basename "$s")
        for n in "$s"/namespaces/*; do
            [ -d "$n" ] || continue
            echo 0 > "$n/enable" 2>/dev/null
            rmdir "$n" || { echo "ABORT: cannot remove namespace $(basename "$n") of $nqn"; exit 1; }
        done
        rmdir "$s" || { echo "ABORT: cannot remove subsystem $nqn"; exit 1; }
        echo "removed subsystem $nqn"
    done

    echo "unexported; mmgt media is idle and the configfs tree is empty"
    status
}

# mmgt has no mdadm.conf, so its boot scan will assemble the initiators' RAID0
# superblocks locally. AUTO -all stops that without needing to name the arrays.
guard() {
    local conf=/etc/mdadm.conf
    [ -f /etc/mdadm/mdadm.conf ] && conf=/etc/mdadm/mdadm.conf
    if grep -qE '^\s*AUTO\s+-all' "$conf" 2>/dev/null; then
        echo "$conf already carries AUTO -all"
    else
        printf '\n# The RAID0s on the exported drives belong to the initiators, not to\n# this host. Never auto-assemble them here; see setup_mmgt_nvmeof_target.sh.\nAUTO -all\n' >> "$conf" ||
            { echo "ABORT: cannot write $conf"; exit 1; }
        echo "appended AUTO -all to $conf"
    fi
    command -v dracut >/dev/null 2>&1 && echo "NOTE: run 'dracut -f' if the initramfs also scans for arrays"
}

case ${1:-} in
    up) up ;;
    down) down ;;
    status) status ;;
    guard) guard ;;
    *) echo "usage: $0 up|down|status|guard"; exit 2 ;;
esac
