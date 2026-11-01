#!/usr/bin/env python3
"""mmgt local 5:1 span x duration sweep on the 16 Solidigm SSDs (fabric must be down).

Cells run in order; a blkdiscard of every namespace separates cell 1 from 2.
Destroys all data on the 48 Solidigm namespaces. Requires --i-understand-data-loss.
"""
import argparse, collections, concurrent.futures, datetime, json, os, subprocess, sys, time
from pathlib import Path

FIO = "/usr/bin/fio"
SSDS = [f"nvme{i}" for i in range(2, 10)] + [f"nvme{i}" for i in range(13, 21)]
BOOT = "nvme11"
MODEL = "SB5PH27X019T"
TEMP_MAX_C = 70
FILLS = [10, 25, 50, 75, 100]
RUNTIME = 600
# Each cell: discard everything, sequentially fill X% of every namespace, then 5:1 over that X%.
CELLS = [(f"fill{x:03d}", f"{x}%", RUNTIME, True) for x in FILLS]


def namespaces(ctrl):
    return [f"/dev/{ctrl}n{i}" for i in (1, 2, 3)]


def log(msg, fh):
    line = f"{datetime.datetime.now():%F %T} {msg}"
    print(line, flush=True)
    fh.write(line + "\n")
    fh.flush()


def guard():
    bad = []
    for mod in ("nvmet", "nvmet_rdma"):
        if Path(f"/sys/module/{mod}").exists():
            bad.append(f"module {mod} loaded (fabric not torn down)")
    mounts = Path("/proc/mounts").read_text()
    mdstat = Path("/proc/mdstat").read_text()
    for ctrl in SSDS:
        if ctrl == BOOT:
            bad.append("boot disk in SSD list")
        if not Path(f"/sys/class/nvme/{ctrl}").exists():
            bad.append(f"{ctrl} missing")
            continue
        if MODEL not in Path(f"/sys/class/nvme/{ctrl}/model").read_text():
            bad.append(f"{ctrl} unexpected model")
        for ns in namespaces(ctrl):
            b = os.path.basename(ns)
            if not Path(f"/sys/block/{b}").exists():
                bad.append(f"{b} missing")
                continue
            if any(Path(f"/sys/block/{b}/holders").iterdir()):
                bad.append(f"{b} has holders")
            if f"{ns} " in mounts or f" {b}" in mdstat or f"{b}[" in mdstat:
                bad.append(f"{b} mounted or in md")
    other = subprocess.run(["pgrep", "-a", "fio"], capture_output=True, text=True).stdout.strip()
    if other:
        bad.append("fio already running: " + other[:200])
    return bad


def smart():
    out = {}
    for c in SSDS:
        d = json.loads(subprocess.run(["nvme", "smart-log", "-o", "json", f"/dev/{c}"],
                                      capture_output=True, text=True, check=True).stdout)
        t = d["temperature"]
        out[c] = {"temp_c": t - 273 if t > 200 else t, "media_errors": d["media_errors"],
                  "critical_warning": d["critical_warning"]}
    return out


def fio_cmd(ctrl, span, runtime, celldir):
    p = f"{celldir}/{ctrl}"
    return [FIO, f"--name={ctrl}", "--filename=" + ":".join(namespaces(ctrl)),
            "--ioengine=libaio", "--direct=1", "--iodepth=32", "--numjobs=1",
            "--bs=256k", "--rw=randrw", "--rwmixread=83", f"--size={span}",
            "--time_based=1", f"--runtime={runtime}", "--norandommap=1", "--randrepeat=0",
            "--lat_percentiles=1", "--percentile_list=50:99:99.9",
            f"--write_bw_log={p}", f"--write_lat_log={p}", "--log_avg_msec=10000",
            "--output-format=json", f"--output={p}.json"]


def timeline(celldir):
    """Sum per-SSD 10 s averages: {t_s: [read GB/s, write GB/s]}."""
    tl = collections.defaultdict(lambda: [0.0, 0.0])
    for c in SSDS:
        f = Path(f"{celldir}/{c}_bw.1.log")
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            t, v, ddir = [x.strip() for x in line.split(",")[:3]]
            tl[round(int(t) / 1000 / 10) * 10][int(ddir)] += int(v) * 1024 / 1e9
    return {k: [round(r, 2), round(w, 2)] for k, (r, w) in sorted(tl.items())}


def summarize(celldir):
    r = w = 0.0
    p99 = []
    for c in SSDS:
        j = json.load(open(f"{celldir}/{c}.json"))["jobs"][0]
        r += j["read"]["bw_bytes"] / 1e9
        w += j["write"]["bw_bytes"] / 1e9
        p99.append(j["read"]["clat_ns"]["percentile"].get("99.000000", 0) / 1e3)
    return {"read_GBps": round(r, 2), "write_GBps": round(w, 2), "total_GBps": round(r + w, 2),
            "total_Gbps": round((r + w) * 8, 1), "read_p99_us_max": round(max(p99)),
            "read_p99_us_median": round(sorted(p99)[len(p99) // 2])}


def fill(span, celldir, fh):
    t0 = time.time()
    procs = []
    for c in SSDS:
        argv = [FIO, "--ioengine=libaio", "--direct=1", "--bs=1M", "--iodepth=32", "--rw=write",
                f"--size={span}", "--output-format=json", f"--output={celldir}/fill_{c}.json"]
        for ns in namespaces(c):
            argv += [f"--name={os.path.basename(ns)}", f"--filename={ns}"]
        procs.append(subprocess.Popen(argv, stderr=open(f"{celldir}/fill_{c}.err", "w")))
    rcs = [p.wait() for p in procs]
    if any(rcs):
        log(f"fill {span} rc={rcs}", fh)
        raise SystemExit(4)
    written = sum(j["write"]["io_bytes"] for c in SSDS
                  for j in json.load(open(f"{celldir}/fill_{c}.json"))["jobs"])
    log(f"fill {span}: {written / 1e12:.2f} TB in {time.time() - t0:.0f}s", fh)


def tail_stats(tl):
    """Mean of the last 12 full 10 s buckets and their second-half vs first-half change."""
    rows = list(tl.values())[:-1][-12:]
    tot = [r + w for r, w in rows]
    if len(tot) < 12:
        return {}
    first, second = sum(tot[:6]) / 6, sum(tot[6:]) / 6
    return {"tail_read_GBps": round(sum(r for r, _ in rows) / 12, 2),
            "tail_write_GBps": round(sum(w for _, w in rows) / 12, 2),
            "tail_total_Gbps": round(sum(tot) / 12 * 8, 1),
            "tail_trend_pct": round(100 * (second - first) / first, 2)}


def discard(fh):
    t0 = time.time()
    devs = [ns for c in SSDS for ns in namespaces(c)]
    with concurrent.futures.ThreadPoolExecutor(len(devs)) as ex:
        rcs = list(ex.map(lambda d: subprocess.run(["blkdiscard", d], capture_output=True, text=True), devs))
    failed = [(d, r.stderr.strip()) for d, r in zip(devs, rcs) if r.returncode]
    log(f"blkdiscard {len(devs)} namespaces in {time.time() - t0:.0f}s, failed={failed}", fh)
    if failed:
        raise SystemExit(5)
    time.sleep(120)
    log("post-discard idle 120s done", fh)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["guard", "run"])
    ap.add_argument("--root", default="/home")
    ap.add_argument("--i-understand-data-loss", action="store_true")
    a = ap.parse_args()
    bad = guard()
    if a.mode == "guard" or bad:
        print(json.dumps({"guard_problems": bad}, indent=1))
        return 0 if not bad else 3
    if not a.i_understand_data_loss:
        print("refusing: --i-understand-data-loss required")
        return 2
    root = Path(a.root) / f"ssd-fill-curve-{datetime.datetime.now():%Y%m%d-%H%M}"
    root.mkdir()
    fh = open(root / "runner.log", "a")
    (root / "plan.json").write_text(json.dumps({"cells": CELLS, "ssds": SSDS, "bs": "256k",
                                                "rwmixread": 83, "iodepth": 32, "numjobs": 1,
                                                "fill": "seq 1M write of span after blkdiscard"}, indent=1))
    log(f"start {root}", fh)
    results = []
    for name, span, runtime, discard_first in CELLS:
        if discard_first:
            discard(fh)
        celldir = root / name
        celldir.mkdir()
        if discard_first:
            fill(span, celldir, fh)
        s0 = smart()
        if max(v["temp_c"] for v in s0.values()) > TEMP_MAX_C:
            log("temperature over limit, stopping", fh)
            return 6
        log(f"cell {name} span={span} runtime={runtime}s start", fh)
        procs = [subprocess.Popen(fio_cmd(c, span, runtime, celldir), stderr=open(f"{celldir}/{c}.err", "w"))
                 for c in SSDS]
        rcs = [p.wait() for p in procs]
        if any(rcs):
            log(f"cell {name} fio rc={rcs}", fh)
            return 4
        tl = timeline(celldir)
        res = {"cell": name, "span": span, "runtime": runtime, **summarize(celldir),
               **tail_stats(tl), "timeline_10s_read_write_GBps": tl, "smart_end": smart()}
        (celldir / "summary.json").write_text(json.dumps(res, indent=1))
        results.append(res)
        tl = list(res["timeline_10s_read_write_GBps"].values())
        log(f"cell {name}: {res['total_GBps']} GB/s ({res['total_Gbps']} Gb/s) r={res['read_GBps']} "
            f"w={res['write_GBps']} p99med={res['read_p99_us_median']}us "
            f"tail={res.get('tail_total_Gbps')} Gb/s (w={res.get('tail_write_GBps')}) "
            f"trend={res.get('tail_trend_pct')}% w_first={tl[0][1] if tl else None}", fh)
    (root / "summary.json").write_text(json.dumps(results, indent=1))
    log("SWEEP DONE", fh)
    return 0


if __name__ == "__main__":
    sys.exit(main())
