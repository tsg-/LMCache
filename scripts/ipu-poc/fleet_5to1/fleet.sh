#!/bin/bash
# Usage: [PYPATH=...] fleet.sh <mode> <name> [dur] [adapter-json] [in_flight] [retention] [trim_every]
# Launches one synchronized cell on all six initiators, waits, prints the sum.
set -u
MODE=$1; NAME=$2; DUR=${3:-600}; ADAPTER=${4:-}; IF=${5:-16}; RET=${6:-20000}; TRIM=${7:-30}
PYPATH=${PYPATH:-/root/dig5to1/lmc_copy:/root/dig5to1/mod}
HOSTS="mmgi0 mmgi1 mmgi2 mmgi3 mmgi4 mmgi5"
START=$(( $(date +%s) + 120 ))
SSH="ssh -o BatchMode=yes -o ControlPath=none -o ServerAliveInterval=30"
nuse() { $SSH mmgt /home/ssd-span-duration-runner/nuse.sh; }
echo "nuse_before $(nuse)"
for h in $HOSTS; do
  case $h in mmgi4|mmgi5) R=/root/LMCache;; *) R=/root/anthropic/LMCache;; esac
  $SSH $h "python3 -I /root/dig5to1/fleet_cell.py $MODE --name $NAME --start-at $START --dur $DUR \
    --adapter '$ADAPTER' --in-flight $IF --retention $RET --trim-every $TRIM --py $R/.venv-bench-l2/bin/python --repo $R --pythonpath $PYPATH" \
    > /tmp/dig5to1/fleet-$NAME-$h.log 2>&1 &
done
wait
echo "nuse_after $(nuse)"
for h in $HOSTS; do $SSH $h "cat /root/dig5to1/fleet/$NAME/$h.json"; done > /tmp/dig5to1/fleet-$NAME.jsonl 2>/dev/null
python3 - "$NAME" <<'PY'
import json, sys, re
name = sys.argv[1]
raw = open(f"/tmp/dig5to1/fleet-{name}.jsonl").read()
dec, rows, pos = json.JSONDecoder(), [], 0
while pos < len(raw):
    if raw[pos].isspace():
        pos += 1
        continue
    obj, pos = dec.raw_decode(raw, pos)
    rows.append(obj)
tot = {"Load": 0.0, "Store": 0.0}
starts = []
for r in rows:
    line = [r["host"]] + [f'{op}={r[op]["gbps"]:.1f}' for op in ("Load", "Store") if op in r]
    if "error" in r: line.append("ERROR " + r["error"][:160])
    if "cpu" in r: line.append(f'cores={r["cpu"]["cores"]}')
    if "trims" in r: line.append(f'trims={len(r["trims"])} max={max([t["sec"] for t in r["trims"]] or [0])}s')
    print(" ".join(line))
    for op in tot:
        if op in r: tot[op] += r[op]["gbps"]
    if "Load" in r and "start" in r["Load"]: starts.append(r["Load"]["start"])
skew = round(max(starts) - min(starts), 2) if starts else None
print(f"FLEET {name}: read {tot['Load']:.1f} + write {tot['Store']:.1f} = {sum(tot.values()):.1f} Gb/s, hosts={len(rows)}, start_skew={skew}s")
PY
