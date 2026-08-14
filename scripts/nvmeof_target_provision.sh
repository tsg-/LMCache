#!/usr/bin/env bash
# nvmeof_target_provision.sh -- NVMe-oF/RDMA target provisioning for the
# initiator-owned + remote NVMe-oF L2 alternative (LMCache-msm.1).
#
# Sets up (or tears down) a nvmet-rdma subsystem exporting a local NVMe
# namespace over the 192.168.200 RDMA fabric. Idempotent: safe to re-run.
#
# NEVER listens on the 192.168.100 management plane. Hard-fails if
# --listen-ip is 192.168.100.x.
#
# Usage:
#   nvmeof_target_provision.sh setup \
#     --nqn nqn.2026-07.io.lmcache.alt:bmg1 \
#     --namespace-device /dev/nvme6n1 \
#     --namespace-id 1 \
#     --listen-ip 192.168.200.4 \
#     --listen-port 4420 \
#     [--host-nqn nqn.2026-07.io.lmcache.alt:bmg0]  # repeatable
#     [--dry-run]
#
#   nvmeof_target_provision.sh teardown \
#     --nqn nqn.2026-07.io.lmcache.alt:bmg1 \
#     --listen-ip 192.168.200.4 \
#     --listen-port 4420 \
#     [--dry-run]
#
#   nvmeof_target_provision.sh status --nqn <nqn>
#
# Requirements (on the target host, e.g. bmg1):
#   - kernel modules nvmet, nvmet-rdma
#   - configfs mounted at /sys/kernel/config
#   - root (via sudo)
#
# Exit codes:
#   0 success (or already in desired state)
#   1 usage / bad arguments
#   2 environment problem (modules missing, configfs not mounted, etc.)
#   3 refused: listen IP is on the management plane
#   4 runtime error from nvmet configfs operations

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
CONFIGFS="/sys/kernel/config"
NVMET="${CONFIGFS}/nvmet"
DEFAULT_LISTEN_PORT="4420"
MGMT_PLANE_PREFIX="192.168.100."
FABRIC_PLANE_PREFIX="192.168.200."

DRY_RUN=0

# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
log() { printf '[nvmeof-target] %s\n' "$*" >&2; }
die() {
    # die "message" [exit_code]
    local msg="$1"
    local code="${2:-4}"
    log "ERROR: $msg"
    exit "$code"
}

run() {
    if (( DRY_RUN )); then
        printf 'DRY-RUN: %s\n' "$*"
    else
        eval "$@"
    fi
}

write_configfs() {
    # write_configfs <value> <path>
    local value="$1" path="$2"
    if (( DRY_RUN )); then
        printf 'DRY-RUN: echo %q | sudo tee %q > /dev/null\n' "$value" "$path"
        return 0
    fi
    printf '%s' "$value" | sudo tee "$path" > /dev/null
}

ensure_root_or_sudo() {
    if [[ $EUID -ne 0 ]] && ! command -v sudo > /dev/null; then
        die "must run as root or have sudo available" 2
    fi
}

ensure_kernel_prereqs() {
    (( DRY_RUN )) && return 0
    mountpoint -q "$CONFIGFS" || die "configfs not mounted at $CONFIGFS" 2
    if [[ ! -d "$NVMET" ]]; then
        sudo modprobe nvmet || die "modprobe nvmet failed" 2
    fi
    if [[ ! -d "$NVMET/subsystems" ]]; then
        die "$NVMET/subsystems missing even after modprobe nvmet" 2
    fi
    sudo modprobe nvmet-rdma || die "modprobe nvmet-rdma failed" 2
}

require_fabric_plane_ip() {
    # Fail-closed: only the 192.168.200 fabric plane is allowed by default.
    # Management-plane IPs (192.168.100.x) get a specific error message so
    # the SSH-plane guardrail is visible in logs.
    local ip="$1"
    local allow_non_fabric="$2"
    if [[ "$ip" == ${MGMT_PLANE_PREFIX}* ]]; then
        die "refusing to listen on management plane IP $ip (192.168.100.x)" 3
    fi
    if [[ "$ip" != ${FABRIC_PLANE_PREFIX}* ]]; then
        if (( allow_non_fabric )); then
            log "WARNING: --allow-non-fabric-ip set; using off-fabric IP $ip"
        else
            die "refusing to listen on non-fabric IP $ip (expected ${FABRIC_PLANE_PREFIX}x; pass --allow-non-fabric-ip to override)" 3
        fi
    fi
}

# ---------------------------------------------------------------------------
# Namespace-device safety check (finding #1). Refuse fail-closed unless the
# caller explicitly passes --force-unsafe-device. Rejects:
#   - non-existent path
#   - non-block-device (regular file, char device, symlink to same)
#   - a partition (avoid clobbering a parent whole-namespace with siblings)
#   - anything currently mounted (self or a child block device)
#   - anything showing an existing filesystem / LVM / MD signature
# ---------------------------------------------------------------------------
check_namespace_device_safe() {
    local dev="$1"
    local force="$2"

    if [[ ! -e "$dev" ]]; then
        die "namespace device $dev does not exist" 5
    fi
    if [[ ! -b "$dev" ]]; then
        die "namespace device $dev is not a block device" 5
    fi

    # Reject partitions (a partition's device-mapper name has a trailing digit
    # after the namespace, e.g. nvme4n1p2). Exporting a partition while the
    # parent namespace serves other data is exactly the FSConnector-clobber
    # footgun we are defending against.
    local base
    base="$(basename "$dev")"
    if [[ "$base" =~ p[0-9]+$ ]] || [[ "$base" =~ [0-9]+p[0-9]+$ ]]; then
        die "$dev is a partition; export the whole namespace or pass --force-unsafe-device" 5
    fi

    # Mount check: /proc/mounts covers both the device and any child block
    # devices (e.g. a partition of $dev).
    if grep -qE "^${dev}[[:space:]]|^${dev}p[0-9]+[[:space:]]" /proc/mounts 2>/dev/null; then
        (( force )) || die "$dev (or a partition of it) is mounted; refusing to export" 5
        log "WARNING: --force-unsafe-device set; $dev appears mounted"
    fi

    # Existing signatures. wipefs -n is non-destructive; it prints any
    # detected magic. `blkid` is a fallback if wipefs is unavailable.
    local sig_found=""
    if command -v wipefs > /dev/null 2>&1; then
        sig_found="$(sudo wipefs -n "$dev" 2>/dev/null | grep -v '^$' || true)"
    elif command -v blkid > /dev/null 2>&1; then
        sig_found="$(sudo blkid "$dev" 2>/dev/null || true)"
    fi
    if [[ -n "$sig_found" ]]; then
        if (( force )); then
            log "WARNING: --force-unsafe-device set; overwriting existing signatures on $dev"
        else
            die "$dev has existing filesystem/LVM/MD signature(s); refusing to export (pass --force-unsafe-device to override)" 5
        fi
    fi
}

# ---------------------------------------------------------------------------
# Allocate a configfs port index that we own. If --listen-port-index is
# passed we use it, refusing to steal an existing port that points at a
# different NQN. Otherwise we scan ports 1..15 and pick the first unused
# slot, or an existing slot whose sole subsystem symlink already targets
# our NQN.
# ---------------------------------------------------------------------------
resolve_port_index() {
    local nqn="$1"
    local requested="$2"

    if [[ -n "$requested" ]]; then
        local port_path="$NVMET/ports/$requested"
        if [[ -d "$port_path" ]] && ! (( DRY_RUN )); then
            # Existing port -- confirm it is either empty or already ours.
            local links
            links=$(find "$port_path/subsystems" -mindepth 1 -maxdepth 1 -type l 2>/dev/null | wc -l)
            local ours
            ours=$([[ -L "$port_path/subsystems/$nqn" ]] && echo 1 || echo 0)
            if (( links > 0 )) && (( ours == 0 )); then
                die "port index $requested is in use by another subsystem; pick a free index" 6
            fi
        fi
        printf '%s\n' "$requested"
        return 0
    fi

    if (( DRY_RUN )); then
        printf '1\n'
        return 0
    fi

    local idx port_path
    for idx in $(seq 1 15); do
        port_path="$NVMET/ports/$idx"
        if [[ ! -d "$port_path" ]]; then
            printf '%s\n' "$idx"
            return 0
        fi
        # Already ours: reuse.
        if [[ -L "$port_path/subsystems/$nqn" ]]; then
            printf '%s\n' "$idx"
            return 0
        fi
    done
    die "no free nvmet port index available in 1..15" 6
}

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
subcmd_setup() {
    local nqn="" ns_device="" ns_id="1" listen_ip="" listen_port="$DEFAULT_LISTEN_PORT"
    local -a host_nqns=()
    local allow_any_host=0 allow_non_fabric=0 force_device=0 port_index=""

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --nqn)                    nqn="$2"; shift 2 ;;
            --namespace-device)       ns_device="$2"; shift 2 ;;
            --namespace-id)           ns_id="$2"; shift 2 ;;
            --listen-ip)              listen_ip="$2"; shift 2 ;;
            --listen-port)            listen_port="$2"; shift 2 ;;
            --listen-port-index)      port_index="$2"; shift 2 ;;
            --host-nqn)               host_nqns+=("$2"); shift 2 ;;
            --allow-any-host)         allow_any_host=1; shift ;;
            --allow-non-fabric-ip)    allow_non_fabric=1; shift ;;
            --force-unsafe-device)    force_device=1; shift ;;
            --dry-run)                DRY_RUN=1; shift ;;
            *) die "unknown option: $1" 1 ;;
        esac
    done

    [[ -n "$nqn" ]]        || die "--nqn required" 1
    [[ -n "$ns_device" ]]  || die "--namespace-device required" 1
    [[ -n "$listen_ip" ]]  || die "--listen-ip required" 1
    [[ "$ns_id" =~ ^[0-9]+$ ]] || die "--namespace-id must be numeric" 1
    if [[ -n "$port_index" && ! "$port_index" =~ ^[0-9]+$ ]]; then
        die "--listen-port-index must be numeric" 1
    fi

    # Access-control: require an explicit host NQN OR an explicit --allow-any-host
    # opt-in. The old default of attr_allow_any_host=1 whenever --host-nqn was
    # missing was a lab-safety footgun for this track.
    if [[ ${#host_nqns[@]} -eq 0 ]] && (( ! allow_any_host )); then
        die "at least one --host-nqn is required (or pass --allow-any-host explicitly for open lab access)" 1
    fi

    require_fabric_plane_ip "$listen_ip" "$allow_non_fabric"
    ensure_root_or_sudo
    ensure_kernel_prereqs

    # Namespace-device safety check. Blocks common footguns like exporting a
    # partition, exporting something mounted, or overwriting an existing FS/LVM.
    if (( ! DRY_RUN )); then
        check_namespace_device_safe "$ns_device" "$force_device"
    fi

    local resolved_port_index
    resolved_port_index="$(resolve_port_index "$nqn" "$port_index")"

    local subsys="$NVMET/subsystems/$nqn"
    local ns_path="$subsys/namespaces/$ns_id"
    local port_path="$NVMET/ports/$resolved_port_index"

    log "provisioning subsystem $nqn on $listen_ip:$listen_port (device=$ns_device ns=$ns_id port_index=$resolved_port_index)"

    run "sudo mkdir -p $(printf %q "$subsys")"
    if [[ ${#host_nqns[@]} -eq 0 ]]; then
        # Only reachable when --allow-any-host was explicitly passed.
        write_configfs "1" "$subsys/attr_allow_any_host"
        log "attr_allow_any_host=1 (--allow-any-host explicitly enabled)"
    else
        write_configfs "0" "$subsys/attr_allow_any_host"
        for host_nqn in "${host_nqns[@]}"; do
            local host_path="$NVMET/hosts/$host_nqn"
            local link="$subsys/allowed_hosts/$host_nqn"
            run "sudo mkdir -p $(printf %q "$host_path")"
            if (( DRY_RUN )); then
                printf 'DRY-RUN: sudo ln -sfT %q %q\n' "$host_path" "$link"
            else
                sudo test -L "$link" || sudo ln -s "$host_path" "$link"
            fi
        done
    fi

    run "sudo mkdir -p $(printf %q "$ns_path")"
    write_configfs "$ns_device" "$ns_path/device_path"
    write_configfs "1"          "$ns_path/enable"

    run "sudo mkdir -p $(printf %q "$port_path")"
    write_configfs "ipv4"        "$port_path/addr_adrfam"
    write_configfs "rdma"        "$port_path/addr_trtype"
    write_configfs "$listen_ip"  "$port_path/addr_traddr"
    write_configfs "$listen_port" "$port_path/addr_trsvcid"

    local port_link="$port_path/subsystems/$nqn"
    if (( DRY_RUN )); then
        printf 'DRY-RUN: sudo ln -sfT %q %q\n' "$subsys" "$port_link"
    else
        sudo test -L "$port_link" || sudo ln -s "$subsys" "$port_link"
    fi

    log "subsystem $nqn ready on ${listen_ip}:${listen_port} (namespace $ns_id -> $ns_device)"
}

# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------
subcmd_teardown() {
    local nqn="" listen_ip="" listen_port="$DEFAULT_LISTEN_PORT"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --nqn)          nqn="$2"; shift 2 ;;
            --listen-ip)    listen_ip="$2"; shift 2 ;;
            --listen-port)  listen_port="$2"; shift 2 ;;
            --dry-run)      DRY_RUN=1; shift ;;
            *) die "unknown option: $1" 1 ;;
        esac
    done

    [[ -n "$nqn" ]] || die "--nqn required" 1
    ensure_root_or_sudo

    local subsys="$NVMET/subsystems/$nqn"

    log "tearing down subsystem $nqn"

    # Scan every port for a symlink to our NQN and only unlink those. Never
    # touch ports whose linked subsystem is not ours.
    if [[ -d "$NVMET/ports" ]] || (( DRY_RUN )); then
        for port_dir in "$NVMET"/ports/*/; do
            [[ -d "$port_dir" ]] || continue
            local port_link="${port_dir}subsystems/$nqn"
            if [[ -L "$port_link" ]]; then
                run "sudo rm -f $(printf %q "$port_link")"
                # Remove the now-empty port directory only if we owned the
                # last symlink on it. rmdir is safe -- it refuses non-empty dirs.
                run "sudo rmdir $(printf %q "$port_dir") 2>/dev/null || true"
            fi
        done
    fi

    if [[ -d "$subsys" ]] || (( DRY_RUN )); then
        for ns_dir in "$subsys"/namespaces/*/; do
            [[ -d "$ns_dir" ]] || continue
            local ns_enable="${ns_dir}enable"
            [[ -f "$ns_enable" ]] && write_configfs "0" "$ns_enable"
            run "sudo rmdir $(printf %q "$ns_dir")"
        done
        for host_link in "$subsys"/allowed_hosts/*; do
            [[ -L "$host_link" ]] || continue
            run "sudo rm -f $(printf %q "$host_link")"
        done
        run "sudo rmdir $(printf %q "$subsys")"
    fi

    log "subsystem $nqn removed"
}

subcmd_status() {
    local nqn=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --nqn) nqn="$2"; shift 2 ;;
            *) die "unknown option: $1" 1 ;;
        esac
    done
    [[ -n "$nqn" ]] || die "--nqn required" 1

    local subsys="$NVMET/subsystems/$nqn"
    if [[ ! -d "$subsys" ]]; then
        echo "absent"
        return 0
    fi
    echo "present: $subsys"
    ls -1 "$subsys/namespaces" 2>/dev/null | while read -r ns; do
        local dev
        dev="$(cat "$subsys/namespaces/$ns/device_path" 2>/dev/null || true)"
        local en
        en="$(cat "$subsys/namespaces/$ns/enable" 2>/dev/null || echo 0)"
        echo "  namespace $ns: device=$dev enable=$en"
    done
    for port in "$NVMET"/ports/*/; do
        [[ -L "$port/subsystems/$nqn" ]] || continue
        local ip port_num
        ip="$(cat "$port/addr_traddr" 2>/dev/null || true)"
        port_num="$(cat "$port/addr_trsvcid" 2>/dev/null || true)"
        echo "  linked port: $ip:$port_num"
    done
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
[[ $# -ge 1 ]] || die "usage: $0 {setup|teardown|status} ..." 1
sub="$1"; shift
case "$sub" in
    setup)    subcmd_setup    "$@" ;;
    teardown) subcmd_teardown "$@" ;;
    status)   subcmd_status   "$@" ;;
    *) die "unknown subcommand: $sub (setup|teardown|status)" 1 ;;
esac
