"""Single-host 5:1 store-path dig: run cells, sample kernel stacks, reclaim own data."""

import collections
import json
import os
import shutil
import subprocess
import sys
import threading
import time

REPO = "/root/anthropic/LMCache"
PY = REPO + "/.venv-bench-l2/bin/python"
PROFILE = REPO + "/scripts/ipu-poc/models/mixtral_8x22b_fp8.yaml"
ROOT = "/mnt/lmcache/mmgi0/dig5to1"
OUT = "/root/dig5to1/out"
RAW_IMG = ROOT + "/raw.img"
SEED_SUBMITS = 256


def xfs_stat():
    rows = {}
    with open("/proc/fs/xfs/stat") as f:
        for line in f:
            p = line.split()
            rows[p[0]] = [int(x) for x in p[1:]]
    return rows


def xfs_delta(a, b):
    return {k: [y - x for x, y in zip(a[k], b[k])] for k in b if k in a}


def descendants(pid):
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        out.append(p)
        try:
            for t in os.listdir(f"/proc/{p}/task"):
                try:
                    with open(f"/proc/{p}/task/{t}/children") as f:
                        todo += [int(c) for c in f.read().split()]
                except OSError:
                    pass
        except OSError:
            pass
    return out


def proc_cpu(pids):
    tot = 0
    for p in pids:
        try:
            with open(f"/proc/{p}/stat") as f:
                s = f.read().rsplit(")", 1)[1].split()
            tot += int(s[11]) + int(s[12])
        except OSError:
            pass
    return tot / os.sysconf("SC_CLK_TCK")


class Sampler(threading.Thread):
    def __init__(self, roots, start_after, length, hz=20):
        super().__init__(daemon=True)
        self.roots, self.start_after, self.length, self.hz = roots, start_after, length, hz
        self.stacks = collections.Counter()
        self.states = collections.Counter()
        self.samples = 0
        self.cpu = None

    def run(self):
        time.sleep(self.start_after)
        pids = [d for r in self.roots for d in descendants(r) if d != os.getpid()]
        c0, t0 = proc_cpu(pids), time.time()
        end = t0 + self.length
        while time.time() < end:
            for p in pids:
                try:
                    tids = os.listdir(f"/proc/{p}/task")
                except OSError:
                    continue
                for t in tids:
                    try:
                        with open(f"/proc/{p}/task/{t}/stat") as f:
                            st = f.read().rsplit(")", 1)[1].split()[0]
                        with open(f"/proc/{p}/task/{t}/stack") as f:
                            fr = [ln.split("] ", 1)[-1].split("+")[0] for ln in f.read().splitlines()]
                    except OSError:
                        continue
                    self.states[st] += 1
                    if st == "R":
                        self.stacks["R:<running>"] += 1
                    elif fr:
                        self.stacks[st + ":" + ";".join(fr[:6])] += 1
            self.samples += 1
            time.sleep(1.0 / self.hz)
        self.cpu = (proc_cpu(pids) - c0) / (time.time() - t0)


def fs_adapter(base, workers):
    return {"type": "fs_native", "base_path": base, "num_workers": workers, "use_odirect": True}


def var_adapter(base, workers, shards, commit=None):
    return {"type": "native_plugin", "module_path": "lmcache_fs", "class_name": "LMCacheFSClient",
            "adapter_params": {"base_path": base, "num_workers": workers, "use_odirect": True,
                               "num_shard_dirs": shards}}


def fsu_adapter(base, workers, depth, shards):
    return {"type": "native_plugin", "module_path": "lmcache_fs_uring",
            "class_name": "LMCacheFSUringClient",
            "adapter_params": {"base_path": base, "num_workers": workers, "use_odirect": True,
                               "io_depth": depth, "num_shard_dirs": shards}}


def raw_adapter(store_workers, load_workers, magic):
    return {
        "meta_magic": magic,
        "type": "raw_block",
        "device_path": RAW_IMG,
        "slot_bytes": ((262144 + 8191) // 4096) * 4096,
        "header_bytes": 4096,
        "meta_total_bytes": 268435456,
        "use_odirect": True,
        "use_uring_cmd": False,
        "io_engine": "io_uring",
        "iouring_queue_depth": 256,
        "num_load_workers": load_workers,
        "num_store_workers": store_workers,
        "num_lookup_workers": 1,
        "load_checkpoint_on_init": True,
        "capacity_bytes": 2560 << 30,
    }


def bench(adapter, prefix, in_flight, extra):
    return [
        "numactl", "--cpunodebind=1", "--membind=1", PY, "-m", "lmcache.cli.main",
        "bench", "l2", "--l2-adapter", json.dumps(adapter, sort_keys=True),
        "--kvcache-shape-profile", PROFILE, "--key-prefix", prefix,
        "--in-flight", str(in_flight), "--rounds", str(SEED_SUBMITS // in_flight),
        "--warmup-rounds", "0", "--l1-align-bytes", "4096", "--format", "json",
    ] + extra


VAR_ENV = dict(os.environ, PYTHONPATH="/root/dig5to1/mod")
BATCH_ENV = dict(os.environ, PYTHONPATH="/root/dig5to1/lmc_copy:/root/dig5to1/mod")


def run_procs(cmds, logbase, env=None):
    procs = []
    for i, c in enumerate(cmds):
        c = c + ["--output", f"{logbase}.p{i}.json"]
        out = open(f"{logbase}.p{i}.out", "w")
        err = open(f"{logbase}.p{i}.err", "w")
        procs.append((subprocess.Popen(c, stdout=out, stderr=err, env=env), out, err))
    return procs


def wait_procs(procs, logbase):
    res = []
    for i, (p, o, e) in enumerate(procs):
        rc = p.wait()
        o.close()
        e.close()
        try:
            with open(f"{logbase}.p{i}.json") as f:
                res.append(json.load(f))
        except (OSError, ValueError):
            res.append({"rc": rc, "error": open(f"{logbase}.p{i}.err").read()[-2000:]})
    return res


def ops(r):
    m = r.get("metrics", {})
    return {k: v for k, v in m.items() if k.startswith("op_")}


def summarize(results):
    rows, starts, ends = [], [], []
    tot = collections.Counter()
    for r in results:
        if "metrics" not in r:
            rows.append({"error": r.get("error", "")[-600:]})
            continue
        row = {}
        for o in ops(r).values():
            row[o["operation"]] = {
                "gbps": o["throughput_success_gbps_decimal"],
                "kops": round(o["ops_per_sec_aggregate"] / 1000, 2),
                "p50": o.get("submit_latency_p50_ms"),
                "p99": o.get("submit_latency_p99_ms"),
            }
            tot[o["operation"]] += o["throughput_success_gbps_decimal"]
            if "phase_start_time_seconds" in o:
                starts.append(o["phase_start_time_seconds"])
                ends.append(o["phase_end_time_seconds"])
        rows.append(row)
    skew = (max(starts) - min(starts)) if starts else None
    return {"per_proc": rows, "sum_gbps": dict(tot), "total_gbps": round(sum(tot.values()), 1),
            "start_skew_sec": skew}


def top_stacks(s, n=14):
    total = sum(s.stacks.values()) or 1
    return [(round(100 * c / total, 1), k) for k, c in s.stacks.most_common(n)]


def cell(spec):
    name = spec["name"]
    ts = time.strftime("%H%M%S")
    log = f"{OUT}/{name}"
    base = f"{ROOT}/{name}"
    procs = spec.get("procs", 1)
    shared = spec.get("shared_dir", False)
    mode = spec.get("mode", "mixed")
    inf = spec.get("if", 8)
    dur = spec.get("dur", 60)
    seeds, mixes = [], []
    for i in range(procs):
        d = base if shared else f"{base}/p{i}"
        os.makedirs(d, exist_ok=True)
        ad = spec["adapter"](d)
        prefix = f"dig-{name}-{ts}-p{i}"
        seeds.append(bench(ad, prefix, inf, ["--only", "store"]))
        if mode == "mixed":
            extra = ["--duration-sec", str(dur), "--read-write-ratio", "5:1",
                     "--write-key-prefix", prefix + "-w",
                     "--no-skip-verify" if spec.get("verify") else "--skip-verify"]
        elif mode == "store":
            extra = ["--only", "store", "--duration-sec", str(dur), "--warmup-sec", "0"]
            prefix += "-s"
        else:
            extra = ["--only", "load", "--duration-sec", str(dur), "--warmup-sec", "0"]
        mixes.append(bench(ad, prefix, inf, extra))
    env = BATCH_ENV if spec.get("batched") else VAR_ENV if spec.get("variant") else None
    seed_res = summarize(wait_procs(run_procs(seeds, log + ".seed", env), log + ".seed"))
    x0 = xfs_stat()
    t0 = time.time()
    running = run_procs(mixes, log, env)
    sampler = Sampler([p.pid for p, _, _ in running], spec.get("sample_after", 20), 25)
    sampler.start()
    res = summarize(wait_procs(running, log))
    wall = time.time() - t0
    sampler.join(timeout=5)
    xd = xfs_delta(x0, xfs_stat())
    out = {"name": name, "spec": {k: v for k, v in spec.items() if k != "adapter"},
           "adapter_example": spec["adapter"](base), "seed": seed_res, "result": res,
           "wall_sec": round(wall, 1), "cpu_cores": round(sampler.cpu or 0, 2),
           "thread_states": dict(sampler.states), "top_stacks": top_stacks(sampler),
           "xfs": {k: xd.get(k) for k in ("dir", "trans", "ig", "log", "push_ail", "xstrat",
                                           "rw", "attr", "icluster", "vnodes", "buf",
                                           "abtb2", "bmbt2", "ibt2", "fibt2")}}
    with open(f"{log}.summary.json", "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps({"name": name, "total": res["total_gbps"], "sum": res["sum_gbps"],
                      "skew": res["start_skew_sec"], "cpu": out["cpu_cores"],
                      "per_proc": res["per_proc"]}), flush=True)
    if not spec.get("keep"):
        shutil.rmtree(base, ignore_errors=True)
        subprocess.run(["fstrim", "/mnt/lmcache"], check=True)
    return out


FIO = "/root/goal5-overnight/bin/fio"
FIO_DIR = "/mnt/lmcache/mmgi0/fio/goal5-fio-20261019-0634"


def fio_cell(name, rw_extra):
    cmd = ["numactl", "--cpunodebind=1", "--membind=1", FIO, "--name=dig", f"--directory={FIO_DIR}",
           "--filename_format=scratch.$jobnum", "--size=16G", "--numjobs=32", "--iodepth=32",
           "--ioengine=/root/goal5-overnight/bin/fio-libaio.so", "--direct=1", "--group_reporting=1",
           "--invalidate=1", "--output-format=json", "--bs=256k", "--time_based=1", "--runtime=60",
           "--ramp_time=30", "--allow_file_create=0"] + rw_extra
    r = json.loads(subprocess.run(cmd, check=True, capture_output=True, text=True).stdout)
    j = r["jobs"][0]
    g = {k: round(j[k]["bw_bytes"] * 8 / 1e9, 1) for k in ("read", "write")}
    lat = {k: round(j[k]["clat_ns"]["mean"] / 1e6, 2) for k in ("read", "write") if j[k]["io_bytes"]}
    out = {"name": name, "gbps": g, "total": round(sum(g.values()), 1), "clat_ms": lat}
    with open(f"{OUT}/{name}.summary.json", "w") as f:
        json.dump(out, f, indent=1)
    print(json.dumps(out), flush=True)


def ensure_raw_img(gib):
    """Fully write the slot file with fio data so every block is mapped, not unwritten."""
    want = gib << 30
    if os.path.exists(RAW_IMG) and os.path.getsize(RAW_IMG) == want:
        return
    os.makedirs(ROOT, exist_ok=True)
    subprocess.run(["numactl", "--cpunodebind=1", "--membind=1", FIO, "--name=rawimg",
                    f"--filename={RAW_IMG}", f"--size={want}", "--rw=write", "--bs=1M",
                    "--iodepth=32", "--direct=1", "--ioengine=/root/goal5-overnight/bin/fio-libaio.so",
                    "--output-format=terse"], check=True, capture_output=True)


CELLS = {
    "fsn_w32": dict(adapter=lambda d: fs_adapter(d, 32)),
    "fsn_w32_store": dict(adapter=lambda d: fs_adapter(d, 32), mode="store"),
    "fsn_w32_load": dict(adapter=lambda d: fs_adapter(d, 32), mode="load"),
    "fsn_w64": dict(adapter=lambda d: fs_adapter(d, 64)),
    "fsn_w64_if16": dict(adapter=lambda d: fs_adapter(d, 64), **{"if": 16}),
    "fsn_w128_if16": dict(adapter=lambda d: fs_adapter(d, 128), **{"if": 16}),
    "fsn_w32_p2": dict(adapter=lambda d: fs_adapter(d, 32), procs=2),
    "fsn_w32_p2_shared": dict(adapter=lambda d: fs_adapter(d, 32), procs=2, shared_dir=True),
    "fsn_w32_p4": dict(adapter=lambda d: fs_adapter(d, 32), procs=4),
    "var_flat_rename": dict(adapter=lambda d: var_adapter(d, 32, 0, "rename"), variant=True, verify=True),
    "var_flat_tmpfile": dict(adapter=lambda d: var_adapter(d, 32, 0, "tmpfile_link"), variant=True, verify=True),
    "var_s256_rename": dict(adapter=lambda d: var_adapter(d, 32, 256, "rename"), variant=True, verify=True),
    "var_s256_tmpfile": dict(adapter=lambda d: var_adapter(d, 32, 256, "tmpfile_link"), variant=True, verify=True),
    "var_s256_tmpfile_store": dict(adapter=lambda d: var_adapter(d, 32, 256, "tmpfile_link"), variant=True, mode="store"),
    "var_s256_tmpfile_w64": dict(adapter=lambda d: var_adapter(d, 64, 256, "tmpfile_link"), variant=True, verify=True, **{"if": 16}),
    "var_s256_rename_w64": dict(adapter=lambda d: var_adapter(d, 64, 256, "rename"), variant=True, verify=True, **{"if": 16}),
    "var_s256_rename_store": dict(adapter=lambda d: var_adapter(d, 32, 256, "rename"), variant=True, mode="store"),
    "var_s256_rename_load_w64": dict(adapter=lambda d: var_adapter(d, 64, 256, "rename"), variant=True, mode="load", **{"if": 16}),
    "var_s256_rename_w128": dict(adapter=lambda d: var_adapter(d, 128, 256, "rename"), variant=True, verify=True, **{"if": 32}),
    "rep_flat_w32": dict(adapter=lambda d: var_adapter(d, 32, 0), variant=True, verify=True),
    "rep_s256_w32": dict(adapter=lambda d: var_adapter(d, 32, 256), variant=True, verify=True),
    "rep_flat_w64": dict(adapter=lambda d: var_adapter(d, 64, 0), variant=True, verify=True, **{"if": 16}),
    "rep_s256_w64": dict(adapter=lambda d: var_adapter(d, 64, 256), variant=True, verify=True, **{"if": 16}),
    "t_fsn_s256_w64": dict(adapter=lambda d: var_adapter(d, 64, 256), variant=True, verify=True, dur=240, **{"if": 16}),
    "t_fsn_flat_w64": dict(adapter=lambda d: var_adapter(d, 64, 0), variant=True, verify=True, dur=240, **{"if": 16}),
    "t_raw_s4": dict(adapter=lambda d: raw_adapter(4, 32, "DIGRAW04"), raw=True, variant=True, verify=True, dur=240, **{"if": 16}),
    "t_raw_s16": dict(adapter=lambda d: raw_adapter(16, 32, "DIGRAW16"), raw=True, variant=True, verify=True, dur=240, **{"if": 16}),
    "t_raw_s32_l64": dict(adapter=lambda d: raw_adapter(32, 64, "DIGRAW32"), raw=True, variant=True, verify=True, dur=240, **{"if": 16}),
    "p_raw_s16": dict(adapter=lambda d: raw_adapter(16, 32, "DIGRAWP6"), raw=True, variant=True, dur=90, **{"if": 16}),
    "b_raw_s4": dict(adapter=lambda d: raw_adapter(4, 32, "DIGBAT04"), raw=True, batched=True, verify=True, dur=240, **{"if": 16}),
    "b_raw_s16": dict(adapter=lambda d: raw_adapter(16, 32, "DIGBAT16"), raw=True, batched=True, verify=True, dur=240, **{"if": 16}),
    "u_s256_w64_d64_if16": dict(adapter=lambda d: fsu_adapter(d, 64, 64, 256), variant=True, verify=True, dur=240, **{"if": 16}),
    "u_s256_w64_d64_if32": dict(adapter=lambda d: fsu_adapter(d, 64, 64, 256), variant=True, verify=True, dur=240, **{"if": 32}),
    "u_s256_w64_d64_if64": dict(adapter=lambda d: fsu_adapter(d, 64, 64, 256), variant=True, verify=True, dur=240, **{"if": 64}),
    "u_s256_w128_d64_if64": dict(adapter=lambda d: fsu_adapter(d, 128, 64, 256), variant=True, verify=True, dur=240, **{"if": 64}),
    "u_flat_w64_d64_if32": dict(adapter=lambda d: fsu_adapter(d, 64, 64, 0), variant=True, verify=True, dur=240, **{"if": 32}),
    "raw_s4": dict(adapter=lambda d: raw_adapter(4, 32, "DIGRAWX4"), raw=True),
    "raw_s16": dict(adapter=lambda d: raw_adapter(16, 32, "DIGRAWX6"), raw=True),
}


def main():
    os.makedirs(OUT, exist_ok=True)
    for arg in sys.argv[1:]:
        name, _, rep = arg.partition("#")
        if name == "fio_mixed":
            fio_cell(name, ["--rw=randrw", "--rwmixread=83"])
            continue
        if name == "fio_read":
            fio_cell(name, ["--rw=randread"])
            continue
        spec = dict(CELLS[name], name=arg.replace("#", "_r"))
        if spec.get("raw"):
            ensure_raw_img(2560)
        cell(spec)


if __name__ == "__main__":
    main()
