#!/usr/bin/env bash
# nvmeof_initiator_attach.sh -- NVMe-oF/RDMA initiator lifecycle for the
# initiator-owned + remote NVMe-oF L2 alternative (LMCache-msm.1).
#
# Attaches a remote subsystem exported by nvmet-rdma, resolves the stable
# /dev/disk/by-id path, and provides a clean disconnect. Never connects
# against a 192.168.100.x management-plane IP.
#
# Usage:
#   nvmeof_initiator_attach.sh connect \
#     --target-ip 192.168.200.4 \
#     --target-port 4420 \
#     --nqn nqn.2026-07.io.lmcache.alt:bmg1 \
#     [--host-nqn nqn.2026-07.io.lmcache.alt:bmg0] \
#     [--ctrl-loss-tmo 30] \
#     [--reconnect-delay 2] \
#     [--wait-secs 15] \
#     [--dry-run]
#
#   nvmeof_initiator_attach.sh disconnect \
#     --nqn nqn.2026-07.io.lmcache.alt:bmg1 \
#     [--dry-run]
#
#   nvmeof_initiator_attach.sh path --nqn nqn.2026-07.io.lmcache.alt:bmg1 \
#     [--namespace 1]
#
# On success, `connect` prints the resolved /dev/disk/by-id/nvme-... path
# for the requested namespace so callers can pipe it directly into an
# LMCache adapter or benchmark runner.
#
# Requirements (on the initiator host, e.g. bmg0):
#   - nvme CLI (>= 1.13) and libnvme
#   - kernel modules nvme_fabrics, nvme_rdma
#   - root (via sudo)
#
# Exit codes:
#   0 success
#   1 usage / bad arguments
#   2 environment problem (missing nvme CLI, missing kmod, etc.)
#   3 refused: target IP is on the management plane
#   4 runtime error from nvme connect / disconnect / path resolution

set -euo pipefail

MGMT_PLANE_PREFIX="192.168.100."
FABRIC_PLANE_PREFIX="192.168.200."
DEFAULT_CTRL_LOSS_TMO="30"
DEFAULT_RECONNECT_DELAY="2"
DEFAULT_WAIT_SECS="15"
DEFAULT_TARGET_PORT="4420"

DRY_RUN=0

log() { printf '[nvmeof-init] %s\n' "$*" >&2; }
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

ensure_root_or_sudo() {
    if [[ $EUID -ne 0 ]] && ! command -v sudo > /dev/null; then
        die "must run as root or have sudo available" 2
    fi
}

ensure_nvme_prereqs() {
    (( DRY_RUN )) && return 0
    command -v nvme > /dev/null || die "nvme CLI not found on PATH" 2
    if ! lsmod | grep -q '^nvme_fabrics'; then
        sudo modprobe nvme_fabrics || die "modprobe nvme_fabrics failed" 2
    fi
    if ! lsmod | grep -q '^nvme_rdma'; then
        sudo modprobe nvme_rdma || die "modprobe nvme_rdma failed" 2
    fi
}

require_fabric_plane_ip() {
    local ip="$1"
    if [[ "$ip" == ${MGMT_PLANE_PREFIX}* ]]; then
        die "refusing to connect to management-plane target IP $ip" 3
    fi
    if [[ "$ip" != ${FABRIC_PLANE_PREFIX}* ]]; then
        log "WARNING: target IP $ip is not on the expected 192.168.200 fabric"
    fi
}

# ---------------------------------------------------------------------------
# Discovery of the controller and namespace after `nvme connect`.
# nvme connect creates /dev/nvmeX (controller) and /dev/nvmeXnY (namespace).
# We resolve to the stable /dev/disk/by-id/nvme-<model>_<serial> path so the
# caller does not depend on the enumeration number.
# ---------------------------------------------------------------------------
find_controller_for_nqn() {
    local nqn="$1"
    (( DRY_RUN )) && { echo "/dev/nvmeX"; return 0; }
    # `nvme list-subsys` shows subsystems and their controllers.
    local ctrl
    ctrl=$(nvme list-subsys -o json 2>/dev/null | python3 -c '
import json, sys
target = sys.argv[1]
data = json.load(sys.stdin)
subsystems = data if isinstance(data, list) else data.get("Subsystems", [])
for entry in subsystems:
    subs = entry.get("Subsystems") if isinstance(entry, dict) else None
    candidates = subs if subs is not None else [entry]
    for sub in candidates:
        if not isinstance(sub, dict):
            continue
        if sub.get("NQN") == target or sub.get("Subsystem NQN") == target:
            for ctrl in sub.get("Controllers", []):
                name = ctrl.get("Controller") if isinstance(ctrl, dict) else None
                if name:
                    print(name)
                    sys.exit(0)
sys.exit(1)
' "$nqn" || true)
    if [[ -z "$ctrl" ]]; then
        return 1
    fi
    printf '/dev/%s\n' "$ctrl"
}

wait_for_by_id_path() {
    local ctrl_dev="$1" namespace="$2" wait_secs="$3"
    (( DRY_RUN )) && { echo "/dev/disk/by-id/nvme-DRYRUN"; return 0; }

    local ctrl_name
    ctrl_name="$(basename "$ctrl_dev")"
    local ns_dev="/dev/${ctrl_name}n${namespace}"

    local deadline=$(( SECONDS + wait_secs ))
    while (( SECONDS < deadline )); do
        [[ -b "$ns_dev" ]] || { sleep 0.5; continue; }
        # Find the by-id symlink that resolves to this namespace.
        local link
        for link in /dev/disk/by-id/nvme-*; do
            [[ -e "$link" ]] || continue
            [[ "$link" == *-part* ]] && continue
            local target
            target="$(readlink -f "$link" 2>/dev/null || true)"
            if [[ "$target" == "$ns_dev" ]]; then
                printf '%s\n' "$link"
                return 0
            fi
        done
        sleep 0.5
    done
    die "timed out waiting for /dev/disk/by-id symlink for $ns_dev" 4
}

# ---------------------------------------------------------------------------
# Connect
# ---------------------------------------------------------------------------
subcmd_connect() {
    local target_ip="" target_port="$DEFAULT_TARGET_PORT" nqn=""
    local host_nqn="" ctrl_loss_tmo="$DEFAULT_CTRL_LOSS_TMO"
    local reconnect_delay="$DEFAULT_RECONNECT_DELAY"
    local wait_secs="$DEFAULT_WAIT_SECS" namespace="1"

    while [[ $# -gt 0 ]]; do
        case "$1" in
            --target-ip)       target_ip="$2"; shift 2 ;;
            --target-port)     target_port="$2"; shift 2 ;;
            --nqn)             nqn="$2"; shift 2 ;;
            --host-nqn)        host_nqn="$2"; shift 2 ;;
            --ctrl-loss-tmo)   ctrl_loss_tmo="$2"; shift 2 ;;
            --reconnect-delay) reconnect_delay="$2"; shift 2 ;;
            --wait-secs)       wait_secs="$2"; shift 2 ;;
            --namespace)       namespace="$2"; shift 2 ;;
            --dry-run)         DRY_RUN=1; shift ;;
            *) die "unknown option: $1" 1 ;;
        esac
    done

    [[ -n "$target_ip" ]] || die "--target-ip required" 1
    [[ -n "$nqn" ]]       || die "--nqn required" 1

    require_fabric_plane_ip "$target_ip"
    ensure_root_or_sudo
    ensure_nvme_prereqs

    log "connecting to $nqn at $target_ip:$target_port"

    local -a cmd=(sudo nvme connect
        --transport rdma
        --traddr "$target_ip"
        --trsvcid "$target_port"
        --nqn "$nqn"
        --ctrl-loss-tmo "$ctrl_loss_tmo"
        --reconnect-delay "$reconnect_delay")
    [[ -n "$host_nqn" ]] && cmd+=(--hostnqn "$host_nqn")

    if (( DRY_RUN )); then
        printf 'DRY-RUN: %s\n' "${cmd[*]}"
    else
        "${cmd[@]}" > /dev/null || die "nvme connect failed" 4
    fi

    local ctrl_dev
    if (( DRY_RUN )); then
        ctrl_dev="/dev/nvmeX"
    else
        ctrl_dev="$(find_controller_for_nqn "$nqn")" || die "controller for $nqn not found after connect" 4
    fi
    log "controller device: $ctrl_dev"

    local by_id
    by_id="$(wait_for_by_id_path "$ctrl_dev" "$namespace" "$wait_secs")"
    log "stable device path: $by_id"

    # Print the stable path on stdout so callers can capture it.
    printf '%s\n' "$by_id"
}

# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------
subcmd_disconnect() {
    local nqn=""
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --nqn)      nqn="$2"; shift 2 ;;
            --dry-run)  DRY_RUN=1; shift ;;
            *) die "unknown option: $1" 1 ;;
        esac
    done
    [[ -n "$nqn" ]] || die "--nqn required" 1
    ensure_root_or_sudo

    log "disconnecting $nqn"
    if (( DRY_RUN )); then
        printf 'DRY-RUN: sudo nvme disconnect --nqn=%q\n' "$nqn"
        return 0
    fi

    if ! sudo nvme disconnect --nqn="$nqn" > /dev/null; then
        # `nvme disconnect` returns non-zero if the subsystem is not attached.
        log "note: nvme disconnect reported non-zero; verifying"
    fi

    if find_controller_for_nqn "$nqn" > /dev/null 2>&1; then
        die "controller for $nqn still present after disconnect" 4
    fi
    log "disconnected cleanly"
}

# ---------------------------------------------------------------------------
# Path lookup (idempotent: also usable after a reattach)
# ---------------------------------------------------------------------------
subcmd_path() {
    local nqn="" namespace="1" wait_secs="$DEFAULT_WAIT_SECS"
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --nqn)         nqn="$2"; shift 2 ;;
            --namespace)   namespace="$2"; shift 2 ;;
            --wait-secs)   wait_secs="$2"; shift 2 ;;
            --dry-run)     DRY_RUN=1; shift ;;
            *) die "unknown option: $1" 1 ;;
        esac
    done
    [[ -n "$nqn" ]] || die "--nqn required" 1

    local ctrl_dev
    if (( DRY_RUN )); then
        ctrl_dev="/dev/nvmeX"
    else
        ctrl_dev="$(find_controller_for_nqn "$nqn")" || die "controller for $nqn not attached" 4
    fi
    wait_for_by_id_path "$ctrl_dev" "$namespace" "$wait_secs"
}

# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
[[ $# -ge 1 ]] || die "usage: $0 {connect|disconnect|path} ..." 1
sub="$1"; shift
case "$sub" in
    connect)    subcmd_connect    "$@" ;;
    disconnect) subcmd_disconnect "$@" ;;
    path)       subcmd_path       "$@" ;;
    *) die "unknown subcommand: $sub (connect|disconnect|path)" 1 ;;
esac
