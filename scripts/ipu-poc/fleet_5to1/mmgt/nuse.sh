#!/bin/bash
# Prints total used GB across the 48 Solidigm namespaces and the max per-namespace %.
# Selects namespaces by model, because mmgt controller numbers change across reboots.
python3 -I - <<"PY"
import json, subprocess
MODEL = "SB5PH27X019T"
out = json.loads(subprocess.run(["nvme", "list", "-o", "json"], capture_output=True, text=True, check=True).stdout)
devs = sorted(d["DevicePath"] for d in out["Devices"] if MODEL in d.get("ModelNumber", ""))
if len(devs) != 48:
    raise SystemExit(f"expected 48 {MODEL} namespaces, found {len(devs)}")
used = mx = 0.0
for dev in devs:
    d = json.loads(subprocess.run(["nvme", "id-ns", dev, "-o", "json"], capture_output=True, text=True, check=True).stdout)
    used += d["nuse"] * 512 / 1e9
    mx = max(mx, 100 * d["nuse"] / d["nsze"])
print(f"used_GB={used:.1f} max_ns_pct={mx:.2f}")
PY
