#!/usr/bin/env bash
# Shell-only monitor for the Stage B chain: polls mmgi0, logs, notifies once.
set -u
root=/root/lmc-ghsm-20261024-2205
log="${1:-$HOME/ghsm-stage-b-monitor.log}"
interval="${2:-300}"
misses=0
while true; do
    out=$(ssh -o BatchMode=yes -o ConnectTimeout=15 root@mmgi0 \
        "systemctl is-active lmc-ghsm-stage-b-run; cat $root/stage-b/status.json 2>/dev/null; echo; tail -n 3 $root/stage-b.log" 2>&1)
    state=$(printf '%s\n' "$out" | head -n1)
    printf '[%s] %s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-600)" >>"$log"
    if [ "$state" = "active" ]; then
        misses=0
    elif [ "$state" = "inactive" ] || [ "$state" = "failed" ]; then
        verdict=FAILED
        printf '%s\n' "$out" | grep -q STAGE_B_COMPLETE && verdict=COMPLETE
        printf '[%s] STAGE_B_%s\n' "$(date '+%Y-%m-%dT%H:%M:%S')" "$verdict" >>"$log"
        osascript -e "display notification \"Stage B $verdict\" with title \"lmc-ghsm\"" 2>/dev/null
        exit 0
    else
        misses=$((misses + 1))
        if [ "$misses" -ge 6 ]; then
            printf '[%s] MONITOR_UNREACHABLE\n' "$(date '+%Y-%m-%dT%H:%M:%S')" >>"$log"
            osascript -e 'display notification "mmgi0 unreachable" with title "lmc-ghsm"' 2>/dev/null
            exit 1
        fi
    fi
    sleep "$interval"
done
