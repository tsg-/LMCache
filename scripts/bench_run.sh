#!/usr/bin/env bash
# bench_run.sh — per-run FSConnector isolation helper
#
# Creates an isolated subdirectory under each given base path so that back-to-back
# benchmark runs do not share NVMe data.  Without isolation a second run sees
# warm L2 hits from prior data, making numbers incomparable.
#
# Usage:
#   bench_run.sh --label RUN_LABEL --paths /mnt/p2p_ext4[,/mnt/nvme5] [--cleanup]
#
# Outputs (one line per base path):
#   /mnt/p2p_ext4/lmcache_bench/run_20260709T0300
#   /mnt/nvme5/lmcache_bench/run_20260709T0300
#
# Pass these paths as FSConnector base_path in the lmcache server config.
# With --cleanup the process remains alive as a cleanup owner. Send it SIGINT
# or SIGTERM after the benchmark exits to remove the directories.

set -euo pipefail

LABEL=""
PATHS=""
CLEANUP=0

usage() {
    echo "Usage: $0 --label LABEL --paths PATH1[,PATH2,...] [--cleanup]" >&2
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --label)   LABEL="$2";  shift 2 ;;
        --paths)   PATHS="$2";  shift 2 ;;
        --cleanup) CLEANUP=1;   shift   ;;
        -h|--help) usage ;;
        *) echo "Unknown argument: $1" >&2; usage ;;
    esac
done

[[ -z "$LABEL" ]] && { echo "ERROR: --label is required" >&2; usage; }
[[ -z "$PATHS" ]] && { echo "ERROR: --paths is required" >&2; usage; }

IFS=',' read -ra BASE_PATHS <<< "$PATHS"

RUN_DIRS=()
for base in "${BASE_PATHS[@]}"; do
    dir="$base/lmcache_bench/$LABEL"
    mkdir -p "$dir"
    RUN_DIRS+=("$dir")
    echo "$dir"
done

if [[ $CLEANUP -eq 1 ]]; then
    CLEANED_UP=0

    cleanup() {
        if [[ $CLEANED_UP -eq 1 ]]; then
            return
        fi
        CLEANED_UP=1

        for d in "${RUN_DIRS[@]}"; do
            rm -rf "$d"
            echo "bench_run: cleaned up $d" >&2
        done
    }

    trap cleanup EXIT
    trap 'exit 0' INT TERM
    echo "bench_run: cleanup armed — send SIGINT (Ctrl-C) or SIGTERM to clean up" >&2

    # Keep the owner alive without a child that can outlive the shell and retain
    # a caller's output pipes after the cleanup trap exits.
    while true; do
        sleep 1
    done
fi
