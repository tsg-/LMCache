#!/bin/bash
# Prints total used GB across the 48 Solidigm namespaces and the max per-namespace %.
python3 -I - <<"PY"
import json, subprocess
ctrls = [f"nvme{i}" for i in list(range(2, 10)) + list(range(13, 21))]
used = mx = 0.0
for c in ctrls:
    for i in (1, 2, 3):
        d = json.loads(subprocess.run(["nvme", "id-ns", f"/dev/{c}n{i}", "-o", "json"], capture_output=True, text=True, check=True).stdout)
        used += d["nuse"] * 512 / 1e9
        mx = max(mx, 100 * d["nuse"] / d["nsze"])
print(f"used_GB={used:.1f} max_ns_pct={mx:.2f}")
PY
