#!/usr/bin/env bash
# Bring up Falcon RDMA on the x86 host side after a host reboot: load
# kernel modules in the required order, then configure the RDMA-capable
# interface(s). This is the "Host Setup" section only, from the Falcon/
# MMG/IPU-OS bring-up guide -- IMC and ACC are a separate persistent
# domain and are not touched by a host-only reboot, so nothing there
# needs to be redone here.
#
# Verified against each host's own ~/.bash_history (2026-08-24): idpf
# and irdma are loaded via insmod against Naveen's custom-built .ko
# files, NOT modprobe -- irdma specifically is the patched "-hvl" build
# (the one-line roce_info->pd_id fix that cuts Falcon connection setup
# from >30min to ~20s for 10k QPs per the 1.3.2 release notes). A plain
# `modprobe irdma` could silently load a different, unpatched module
# instead, so this script uses the exact paths seen in history.
#
# Order also matches history exactly: idpf load -> interface config ->
# rmmod ice -> ib_uverbs -> irdma load. Interface config happens on the
# idpf-provided netdev before irdma is loaded; ice removal happens
# after idpf but must happen before irdma (loading irdma while ice is
# present has been observed to crash the host).
#
# NOT covered: the NVMe-oF layer (nvmet target config on mmgt, nvme
# connect on the initiators). That's a separate step on top of this.
#
# Usage: ./falcon_host_setup.sh <mmgi0|mmgi1|mmgt|all>
set -euo pipefail

IDPF_KO="/root/naveen/ethernet-linux-idpf/idpf/src/idpf.ko"
IRDMA_KO="/root/naveen/release-ci-falcon-1.3.2/falcon_patches/irdma-0.0.129.57-hvl/src/irdma/irdma.ko"

usage() {
    echo "Usage: $0 <mmgi0|mmgi1|mmgt|all>" >&2
    exit 1
}

[ $# -eq 1 ] || usage
TARGET="$1"

# $2 is one interface spec: "iface|addr|route1,route2,..." (routes as
# "net:via", comma-separated, may be empty)
bring_up_host() {
    local host="$1"
    shift
    # ssh joins argv into one string for the remote shell to re-parse, which
    # loses local quoting -- the "|" in interface specs gets reinterpreted
    # as a shell pipe. %q re-escapes each arg so it survives that rejoin.
    local q_idpf q_irdma quoted=()
    printf -v q_idpf '%q' "$IDPF_KO"
    printf -v q_irdma '%q' "$IRDMA_KO"
    for a in "$@"; do
        printf -v qa '%q' "$a"
        quoted+=("$qa")
    done
    ssh "$host" bash -s -- "$q_idpf" "$q_irdma" "${quoted[@]}" <<'EOF'
set -euo pipefail
IDPF_KO="$1"; IRDMA_KO="$2"; shift 2

echo "== $(hostname): pre-clean =="
rmmod irdma 2>/dev/null || true
rmmod idpf 2>/dev/null || true

echo "== $(hostname): idpf =="
insmod "$IDPF_KO"
sleep 6

echo "== $(hostname): interfaces (idpf netdev, before irdma) =="
IFACES=()
for spec in "$@"; do
    iface="${spec%%|*}"
    rest="${spec#*|}"
    addr="${rest%%|*}"
    routes="${rest#*|}"
    IFACES+=("$iface")

    ip link set dev "$iface" up
    ip addr replace "$addr" dev "$iface"
    ip link set dev "$iface" mtu 9100

    if [ -n "$routes" ] && [ "$routes" != "$rest" ]; then
        IFS=',' read -ra ROUTE_LIST <<< "$routes"
        for r in "${ROUTE_LIST[@]}"; do
            [ -n "$r" ] || continue
            net="${r%%:*}"
            via="${r##*:}"
            ip route replace "$net" via "$via" dev "$iface"
        done
    fi
done

echo "== $(hostname): ice + ib_uverbs + irdma =="
if lsmod | grep -q '^ice '; then
    echo "  ice is loaded -- unloading before irdma (known crash-on-load otherwise)"
    rmmod ice
fi
modprobe ib_uverbs
insmod "$IRDMA_KO"
sleep 2

echo "  loaded: $(lsmod | grep -E '^(idpf|irdma) ' | awk '{print $1}' | tr '\n' ' ')"
for iface in "${IFACES[@]}"; do
    ip -br a show dev "$iface"
done
EOF
}

setup_mmgi0() {
    bring_up_host mmgi0 "ens7f0|200.0.4.2/24|200.0.3.0/24:200.0.4.1,200.0.6.0/24:200.0.4.1"
}

setup_mmgi1() {
    bring_up_host mmgi1 "ens7f0|200.0.3.2/24|200.0.4.0/24:200.0.3.1,200.0.5.0/24:200.0.3.1"
}

setup_mmgt() {
    bring_up_host mmgt \
        "enp45s0f0|200.0.5.2/24|200.0.3.0/24:200.0.5.1" \
        "enp79s0f0|200.0.6.2/24|200.0.4.0/24:200.0.6.1"
}

case "$TARGET" in
    mmgi0) setup_mmgi0 ;;
    mmgi1) setup_mmgi1 ;;
    mmgt) setup_mmgt ;;
    all)
        setup_mmgt
        setup_mmgi0
        setup_mmgi1
        ;;
    *) usage ;;
esac

echo "== done: rdma link state =="
ssh "$( [ "$TARGET" = all ] && echo mmgt || echo "$TARGET" )" "rdma link show" 2>&1 || true
