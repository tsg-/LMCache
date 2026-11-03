#!/bin/bash
# Usage: PYPATH=... fleet_np.sh <name> <nproc> <dur> <adapter-json> <in_flight> <retention>
# One synchronized LMCache cell with <nproc> bench processes per initiator; samples
# each bench main thread's CPU for 20 s at mid-run and prints per-host sums.
set -u
NAME=$1; NP=$2; DUR=$3; ADAPTER=$4; IF=$5; RET=$6
PYPATH=${PYPATH:-/root/dig5to1/lmc_rebased}
HOSTS="mmgi0 mmgi1 mmgi2 mmgi3 mmgi4 mmgi5"
START=$(( $(date +%s) + 150 ))
SSH="ssh -o BatchMode=yes -o ControlPath=none -o ServerAliveInterval=30"
for h in $HOSTS; do
  case $h in mmgi4|mmgi5) R=/root/LMCache;; *) R=/root/anthropic/LMCache;; esac
  for p in $(seq 0 $((NP - 1))); do
    $SSH $h "python3 -I /root/dig5to1/fleet_cell.py lmc --name ${NAME}_p$p --start-at $START --dur $DUR \
      --adapter '$ADAPTER' --in-flight $IF --retention $RET --trim-every 100000 \
      --py $R/.venv-bench-l2/bin/python --repo $R --pythonpath $PYPATH" \
      > /tmp/dig5to1/fleet-${NAME}_p$p-$h.log 2>&1 &
  done
  $SSH $h "sleep \$(( $START - \$(date +%s) + $DUR / 2 )); T=\$(getconf CLK_TCK); \
    for P in \$(pgrep -f '^/root/.*/bin/python -m lmcache.cli.main bench l2 .*fleet-${NAME}_p.*duration-sec'); do \
      a=\$(awk '{print \$14+\$15}' /proc/\$P/task/\$P/stat); sleep 20; b=\$(awk '{print \$14+\$15}' /proc/\$P/task/\$P/stat); \
      echo \"main_thread pid=\$P util=\$(( (b-a)*100/(20*T) ))%\"; done" > /tmp/dig5to1/mt-$NAME-$h.log 2>&1 &
done
wait
python3 - "$NAME" "$NP" <<'PY'
import json, subprocess, sys
name, np_ = sys.argv[1], int(sys.argv[2])
tot = {"Load": 0.0, "Store": 0.0}
for h in ["mmgi0", "mmgi1", "mmgi2", "mmgi3", "mmgi4", "mmgi5"]:
    line, cores = [h], 0.0
    for p in range(np_):
        out = subprocess.run(["ssh", "-o", "BatchMode=yes", h, f"cat /root/dig5to1/fleet/{name}_p{p}/{h}.json"],
                             capture_output=True, text=True).stdout
        r = json.loads(out)
        if "error" in r:
            line.append(f"p{p} ERROR {r['error'][:120]}")
        for op in tot:
            tot[op] += r.get(op, {}).get("gbps", 0)
        line.append(f"p{p} L={r.get('Load', {}).get('gbps', 0):.1f} S={r.get('Store', {}).get('gbps', 0):.1f}")
        cores += r.get("cpu", {}).get("cores", 0)
    mt = open(f"/tmp/dig5to1/mt-{name}-{h}.log").read().split()
    line.append(f"cores={cores:.2f} " + " ".join(x for x in mt if x.startswith("util=")))
    print(" ".join(line))
print(f"FLEET {name}: read {tot['Load']:.1f} + write {tot['Store']:.1f} = {sum(tot.values()):.1f} Gb/s")
PY
