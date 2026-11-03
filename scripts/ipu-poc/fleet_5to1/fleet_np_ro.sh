#!/bin/bash
# Usage: [LOGDIR=...] PYPATH=... PROFILE=... fleet_np_ro.sh <name> <nproc> <dur> <adapter-json> <in_flight>
# Read-only fleet cell with <nproc> bench l2 processes per initiator, each on its
# own seeded corpus. p0 serves the per-host 6x2 metrics port; the rest serve none.
set -u
NAME=$1; NP=$2; DUR=$3; ADAPTER=$4; IF=$5
PYPATH=${PYPATH:?}; PROFILE=${PROFILE:?}
HOSTS="mmgi0 mmgi1 mmgi2 mmgi3 mmgi4 mmgi5"
# Seeding scales with NP; give every process time to seed before the shared start.
START=$(( $(date +%s) + ${START_DELAY:-120} ))
SSH="ssh -o BatchMode=yes -o ControlPath=none -o ServerAliveInterval=30"
L=${LOGDIR:-/tmp/dig5to1}
for h in $HOSTS; do
  case $h in mmgi4|mmgi5) R=/root/LMCache;; *) R=/root/anthropic/LMCache;; esac
  for p in $(seq 0 $((NP - 1))); do
    MP=-1; [ "$p" -gt 0 ] && MP=0
    $SSH $h "python3 -I /root/dig5to1/fleet_cell.py lmc --name ${NAME}_p$p --start-at $START --dur $DUR \
      --adapter '$ADAPTER' --in-flight $IF --retention 20000 --trim-every 100000 --read-only \
      --profile $PROFILE --metrics-port $MP \
      --py $R/.venv-bench-l2/bin/python --repo $R --pythonpath $PYPATH" \
      > $L/fleet-${NAME}_p$p-$h.log 2>&1 &
  done
  $SSH $h "sleep \$(( $START - \$(date +%s) + $DUR / 2 )); T=\$(getconf CLK_TCK); \
    for P in \$(pgrep -f '^/root/.*/bin/python -m lmcache.cli.main bench l2 .*fleet-${NAME}_p.*duration-sec'); do \
      a=\$(awk '{print \$14+\$15}' /proc/\$P/task/\$P/stat); sleep 20; b=\$(awk '{print \$14+\$15}' /proc/\$P/task/\$P/stat); \
      echo \"util=\$(( (b-a)*100/(20*T) ))%\"; done; top -bn1 | sed -n 3p" > $L/mt-$NAME-$h.log 2>&1 &
done
wait
python3 - "$NAME" "$NP" "$L" <<'PY'
import json, subprocess, sys
name, np_ = sys.argv[1], int(sys.argv[2])
total, starts, ends = 0.0, [], []
for h in ["mmgi0", "mmgi1", "mmgi2", "mmgi3", "mmgi4", "mmgi5"]:
    line, cores, hsum = [h], 0.0, 0.0
    for p in range(np_):
        out = subprocess.run(["ssh", "-o", "BatchMode=yes", h, f"cat /root/dig5to1/fleet/{name}_p{p}/{h}.json"],
                             capture_output=True, text=True).stdout
        try:
            r = json.loads(out)
        except ValueError:
            line.append(f"p{p} NO-RESULT")
            continue
        if "error" in r:
            line.append(f"p{p} ERROR {r['error'][:120]}")
        ld = r.get("Load", {})
        hsum += ld.get("gbps", 0)
        if "start" in ld:
            starts.append(ld["start"]); ends.append(ld["end"])
        line.append(f"p{p}={ld.get('gbps', 0):.1f}")
        cores += r.get("cpu", {}).get("cores", 0)
    total += hsum
    mt = open(f"{sys.argv[3]}/mt-{name}-{h}.log").read().split("\n")
    utils = " ".join(x for x in " ".join(mt).split() if x.startswith("util="))
    cpu = next((x for x in mt if "Cpu" in x), "").strip()
    print(f"{h} sum={hsum:.1f} cores={cores:.2f} mainthread[{utils}] {cpu}")
    print("   " + " ".join(line[1:]))
skew = round(max(starts) - min(starts), 2) if starts else None
span = round(min(ends) - max(starts), 1) if starts else None
print(f"FLEET {name}: np={np_} read {total:.1f} Gb/s, start_skew={skew}s overlap={span}s")
PY
