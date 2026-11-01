#!/usr/bin/env bash
set -euo pipefail

root=/root/lmc-ghsm-20261024-2205
cd "$root"
exec >>"$root/stage-b.log" 2>&1
printf 'STAGE_B_START %s\n' "$(date -Is)"
/usr/bin/python3 -u "$root/ghsm_stage_b.py" orchestrate
printf 'STAGE_B_COMPLETE %s\n' "$(date -Is)"
