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
    local ip="$1"
    if [[ "$ip" == ${MGMT_PLANE_PREFIX}* ]]; then
        die "refusing to listen on management plane IP $ip (192.168.100.x)" 3
    fi
    if [[ "$ip" != ${FABRIC_PLANE_PREFIX}* ]]; then
        log "WARNING: listen IP $ip is not on the expected 192.168.200 fabric"
    fi
}

# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------
subcmd_setup() {
    local nqn="" ns_device="" ns_id="1" listen_ip="" listen_port="$DEFAULT_LISTEN_PORT"
    local -a host_nqns=()

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --nqn)               nqn="$2"; shift 2 ;;
            --namespace-device)  ns_device="$2"; shift 2 ;;
            --namespace-id)      ns_id="$2"; shift 2 ;;
            --listen-ip)         listen_ip="$2"; shift 2 ;;
            --listen-port)       listen_port="$2"; shift 2 ;;
            --host-nqn)          host_nqns+=("$2"); shift 2 ;;
            --dry-run)           DRY_RUN=1; shift ;;
            *) die "unknown option: $1" 1 ;;
        esac
    done

    [[ -n "$nqn" ]]        || die "--nqn required" 1
    [[ -n "$ns_device" ]]  || die "--namespace-device required" 1
    [[ -n "$listen_ip" ]]  || die "--listen-ip required" 1
    [[ "$ns_id" =~ ^[0-9]+$ ]] || die "--namespace-id must be numeric" 1

    require_fabric_plane_ip "$listen_ip"
    ensure_root_or_sudo
    ensure_kernel_prereqs

    local subsys="$NVMET/subsystems/$nqn"
    local ns_path="$subsys/namespaces/$ns_id"
    local port_path="$NVMET/ports/1"

    log "provisioning subsystem $nqn on $listen_ip:$listen_port (device=$ns_device ns=$ns_id)"

    run "sudo mkdir -p $(printf %q "$subsys")"
    if [[ ${#host_nqns[@]} -eq 0 ]]; then
        write_configfs "1" "$subsys/attr_allow_any_host"
        log "attr_allow_any_host=1 (no --host-nqn given; open access -- OK for lab)"
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
    local port_path="$NVMET/ports/1"
    local port_link="$port_path/subsystems/$nqn"

    log "tearing down subsystem $nqn"

    if [[ -L "$port_link" ]] || (( DRY_RUN )); then
        run "sudo rm -f $(printf %q "$port_link")"
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
