#!/usr/bin/env python3
"""One host's part of a synchronized fleet cell: LMCache 5:1 or fio 5:1.

Writes /root/dig5to1/fleet/<name>/<host>.json. Only touches files under
/mnt/lmcache/<host>/fleet/ and runs fstrim on /mnt/lmcache.
"""
import argparse, json, os, resource, shutil, socket, subprocess, threading, time
from pathlib import Path

FIO = "/root/goal5-overnight/bin/fio"
FIO_ENGINE = "/root/goal5-overnight/bin/fio-libaio.so"
PROFILE_REL = "scripts/ipu-poc/models/mixtral_8x22b_fp8.yaml"
SEED_SUBMITS = 256


def host_name():
    return {"dell-r760-poc-003": "mmgi0", "dell-r760-poc-001": "mmgi1", "d-p00588-b14-p9": "mmgi2",
            "d-p00590-b14-p11": "mmgi3", "dell-r760-poc-004": "mmgi4",
            "dell-r760-poc-005": "mmgi5"}[socket.gethostname().split(".")[0].lower()]


def trim_loop(stop, every, log):
    while not stop.wait(every):
        t0 = time.time()
        r = subprocess.run(["fstrim", "/mnt/lmcache"], capture_output=True, text=True)
        log.append({"t": round(t0, 1), "sec": round(time.time() - t0, 2), "rc": r.returncode})


def wait_until(t):
    while time.time() < t:
        time.sleep(min(1.0, t - time.time()))


def bench_cmd(py, repo, adapter, prefix, in_flight, extra, out):
    return ["numactl", "--cpunodebind=1", "--membind=1", py, "-m", "lmcache.cli.main", "bench", "l2",
            "--l2-adapter", json.dumps(adapter, sort_keys=True),
            "--kvcache-shape-profile", f"{repo}/{PROFILE_REL}", "--key-prefix", prefix,
            "--in-flight", str(in_flight), "--rounds", str(SEED_SUBMITS // in_flight),
            "--warmup-rounds", "0", "--l1-align-bytes", "4096", "--format", "json",
            "--output", out] + extra


def run_lmc(a, host, outdir):
    base = f"/mnt/lmcache/{host}/fleet/{a.name}"
    os.makedirs(base, exist_ok=True)
    adapter = json.loads(a.adapter.replace("@BASE@", base))
    env = dict(os.environ, PYTHONPATH="/root/dig5to1/lmc_copy:/root/dig5to1/mod",
               LMCACHE_DISABLE_BANNER="1")
    prefix = f"fleet-{a.name}-{host}"
    seed = subprocess.run(bench_cmd(a.py, a.repo, adapter, prefix, a.in_flight, ["--only", "store"],
                                    f"{outdir}/{host}.seed.json"),
                          env=env, capture_output=True, text=True)
    if seed.returncode:
        return {"error": "seed failed", "stderr": seed.stderr[-1500:]}
    wait_until(a.start_at)
    trims, stop = [], threading.Event()
    th = threading.Thread(target=trim_loop, args=(stop, a.trim_every, trims), daemon=True)
    th.start()
    extra = ["--duration-sec", str(a.dur), "--read-write-ratio", "5:1",
             "--write-key-prefix", prefix + "-w", "--write-retention-submits", str(a.retention),
             "--no-skip-verify"]
    t0 = time.time()
    r = subprocess.run(bench_cmd(a.py, a.repo, adapter, prefix, a.in_flight, extra, f"{outdir}/{host}.bench.json"),
                       env=env, capture_output=True, text=True)
    stop.set()
    th.join()
    out = {"rc": r.returncode, "launched_at": t0, "trims": trims,
           "tail": [l for l in r.stdout.splitlines() if "[" in l][-8:],
           "drain": [l for l in r.stdout.splitlines() if "draining" in l]}
    try:
        m = json.load(open(f"{outdir}/{host}.bench.json"))["metrics"]
        for op in (v for k, v in m.items() if k.startswith("op_")):
            out[op["operation"]] = {"gbps": op["throughput_success_gbps_decimal"],
                                    "kops": round(op["ops_per_sec_aggregate"] / 1e3, 2),
                                    "p50": op["submit_latency_p50_ms"], "p99": op["submit_latency_p99_ms"],
                                    "start": op["phase_start_time_seconds"], "end": op["phase_end_time_seconds"]}
    except (OSError, ValueError, KeyError) as e:
        out["error"] = f"no bench result: {e}; {r.stderr[-1500:]}"
    out["used_before_reclaim"] = shutil.disk_usage("/mnt/lmcache").used
    shutil.rmtree(base, ignore_errors=True)
    subprocess.run(["fstrim", "/mnt/lmcache"], check=False)
    return out


def fio_dir(host):
    return f"/mnt/lmcache/{host}/fleet/fio"


def run_fio_seed(a, host):
    d = fio_dir(host)
    os.makedirs(d, exist_ok=True)
    r = subprocess.run(["numactl", "--cpunodebind=1", "--membind=1", FIO, "--name=seed", f"--directory={d}",
                        "--filename_format=scratch.$jobnum", "--size=16G", "--numjobs=32", "--iodepth=32",
                        f"--ioengine={FIO_ENGINE}", "--direct=1", "--rw=write", "--bs=1M",
                        "--output-format=terse"], capture_output=True, text=True)
    return {"rc": r.returncode, "stderr": r.stderr[-500:]}


def run_fio(a, host):
    wait_until(a.start_at)
    t0 = time.time()
    r = subprocess.run(["numactl", "--cpunodebind=1", "--membind=1", FIO, "--name=fleet",
                        f"--directory={fio_dir(host)}", "--filename_format=scratch.$jobnum", "--size=16G",
                        "--numjobs=32", "--iodepth=32", f"--ioengine={FIO_ENGINE}", "--direct=1",
                        "--group_reporting=1", "--invalidate=1", "--output-format=json", "--bs=256k",
                        "--rw=randrw", "--rwmixread=83", "--time_based=1", f"--runtime={a.dur}",
                        "--ramp_time=30", "--allow_file_create=0", "--norandommap=1", "--randrepeat=0"],
                       capture_output=True, text=True)
    if r.returncode:
        return {"rc": r.returncode, "error": r.stderr[-800:]}
    j = json.loads(r.stdout)["jobs"][0]
    return {"rc": 0, "launched_at": t0,
            "Load": {"gbps": round(j["read"]["bw_bytes"] * 8 / 1e9, 2)},
            "Store": {"gbps": round(j["write"]["bw_bytes"] * 8 / 1e9, 2)}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["lmc", "fio", "fio_seed"])
    ap.add_argument("--name", required=True)
    ap.add_argument("--start-at", type=float, default=0)
    ap.add_argument("--dur", type=int, default=600)
    ap.add_argument("--adapter", default="")
    ap.add_argument("--in-flight", type=int, default=16)
    ap.add_argument("--retention", type=int, default=20000)
    ap.add_argument("--trim-every", type=int, default=30)
    ap.add_argument("--py", default="")
    ap.add_argument("--repo", default="")
    a = ap.parse_args()
    # fs_uring holds one fd per in-flight read; a 1024 soft limit caps in-flight
    # below 32 submits x 56 objects. Children inherit the raised soft limit.
    _, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    host = host_name()
    outdir = Path(f"/root/dig5to1/fleet/{a.name}")
    outdir.mkdir(parents=True, exist_ok=True)
    if a.mode == "lmc":
        res = run_lmc(a, host, outdir)
    elif a.mode == "fio":
        res = run_fio(a, host)
    else:
        res = run_fio_seed(a, host)
    res.update(host=host, mode=a.mode, name=a.name, args=vars(a))
    (outdir / f"{host}.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
