# mkp1/mkp2 Instrumentation

Deployed on the control host as of 2026-08-04 and used to cross-check the
`fs_native` sustained-load results. Still skip it for Stage 1 FIO preflights:
self-contained JSON is sufficient there, and a background scraper risks
perturbing DDIO measurements at the QD 256/256k knee.

## Topology (mkp lab)

```
  control host (laptop / VM)                mkp1 (10.166.97.21)     mkp2 (10.166.97.113)
  ┌──────────────────────────┐              ┌──────────────┐        ┌──────────────┐
  │ prometheus + grafana     │ ─mgmt scrape→│ node_exporter│        │ node_exporter│
  │ (docker-compose)         │              │  127.0.0.1:9100        │  127.0.0.1:9100
  └──────────────────────────┘              └──────────────┘        └──────────────┘
                                             10.166.97.0/22 mgmt (SSH + scrape)
                                             200.0.0.0/24    RDMA fabric on ens2f0
                                                             (traffic; do NOT touch)
```

- **node_exporter** binds to `127.0.0.1:9100` on each host — reachable only via
  the SSH tunnel from the control host. No listener on the mgmt or fabric IP.
- **Scrape** traverses the SSH tunnel over the 10.166.97 mgmt plane.
- **RDMA fabric NIC counters** are read via the textfile collector against
  `ens2f0`; the scrape path never touches 200.0.0.x.
- **IPU-side aliases** (100.0.0.1, 100.2.0.2, 10.10.0.1 on ens2f0d2/d3) — those
  are IPU control planes / duplicates. Do NOT scrape them.

## Coverage

| Signal                             | Source                                 |
|------------------------------------|----------------------------------------|
| CPU, memory, per-NUMA, load        | node_exporter (default collectors)     |
| Per-block-device I/O               | node_exporter `diskstats`              |
| NIC counters (RDMA fabric)         | textfile collector wrapping `ethtool -S ens1f1np1` |
| NVMe SMART / per-ns queue          | textfile collector wrapping `nvme smart-log` |
| LMCache internal counters (Stage 2+) | textfile collector wrapping app stats JSON |
| `bench l2` live counters            | `--serve-metrics` endpoint, job `lmcache_bench` |

### The `lmcache_bench` job

`bench l2 --serve-metrics 9101 --metrics-bind-address 127.0.0.1` publishes live
submit/success/bytes counters for the duration of the CLI process. Forward it
over the mgmt plane and Prometheus picks it up as job `lmcache_bench`:

```bash
ssh -N -L 19102:127.0.0.1:9101 mkp1
```

Two properties matter when reading the series:

- **The target is down between cells.** The endpoint lives only as long as one
  `bench l2` invocation, so `up == 0` between sweep cells is expected, not a
  scrape failure. A 120 s cell at the 5 s interval yields roughly 24 samples.
- **`phase` separates `warmup` from `measured`**, which is what lets a
  Prometheus-side rate be compared against the run's JSON independently. Rate
  over the measured phase only; including warmup biases it.

`--web.enable-lifecycle` is set on the Prometheus container so `curl -X POST
http://127.0.0.1:9090/-/reload` picks up config edits. Reload (or recreate)
**before** a sweep starts — a restart mid-sweep loses samples for the cell in
flight.

## Cheap intermediate (no Prometheus)

For single-run correlation without a full stack:

```bash
# on each host, timestamped CSV to a per-run dir
dstat --output run.dstat.csv --cpu --mem --net -N ens1f1np1 --disk -D nvme0n1,nvme1n1 --nvme 1 &
nvme iostat > run.nvme.csv &
```

Aligned timestamps + a single-node view = ~80% of the value. Upgrade to the
full stack only when you want live mid-run visibility or a second viewer.

## Files

- `node_exporter.service` — systemd unit for mkp1/mkp2
- `docker-compose.yml` — Prometheus + Grafana stack for control host
- `prometheus.yml` — scrape config with mgmt-plane targets
- `textfile/rdma_nic.sh` — cron-driven Mellanox counter exporter
- `textfile/nvme_stats.sh` — cron-driven NVMe SMART exporter
- `grafana-dashboard-lmcache.json` — bench-oriented dashboard (Stage 2+)

## Install order (when triggered)

1. Copy `node_exporter.service` + binary to mkp1, mkp2. Bind `127.0.0.1:9100`.
2. Verify no listener on 0.0.0.0 (`ss -tlnp | grep 9100`).
3. On control host: `docker compose up -d` from this dir.
4. SSH-tunnel port 9100 from each SUT to prometheus, or open the mgmt-plane
   port explicitly (never expose to test fabric).
5. Load Grafana dashboard JSON.

## Do NOT

- Bind exporter to `200.0.0.x` (RDMA fabric on ens2f0) — 127.0.0.1 only.
- Run docker on mkp1 or mkp2.
- Enable prometheus scraping during the DDIO-knee FIO preflights — background
  cache/PCIe traffic invalidates the measurement.
- Reconfigure MTU or bring links down on the 10.166.97 mgmt plane while an
  exporter target is being added — same "do not touch mgmt plane" rule as
  everything else.
- Read counters from `ens101f0` (mgmt) in the RDMA-fabric textfile job — it
  hides pause / OOB counters that only surface on the fabric NIC.
