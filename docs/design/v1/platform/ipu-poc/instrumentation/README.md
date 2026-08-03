# mkp1/mkp2 Instrumentation (Parked)

Parked artifact — **not deployed**. Intended for LMCache Stage 2+ runs where a
live cross-host view earns its keep. Skip for Stage 1 FIO preflights (self-
contained JSON is sufficient and a background scraper risks perturbing DDIO
measurements at the QD 256/256k knee).

## Topology

```
  control host (laptop / VM)                mkp1 (target)          mkp2 (initiator)
  ┌──────────────────────────┐              ┌──────────────┐        ┌──────────────┐
  │ prometheus + grafana     │ ─mgmt scrape→│ node_exporter│        │ node_exporter│
  │ (docker-compose)         │              │  :9100       │        │  :9100       │
  └──────────────────────────┘              └──────────────┘        └──────────────┘
                                             192.168.100.x mgmt plane (scrape)
                                             192.168.200.x RDMA fabric (traffic; do NOT touch)
```

- **node_exporter** runs natively on mkp1/mkp2 (no docker on data-plane hosts).
- **Prometheus + Grafana** run in a single compose stack on a third host.
- Scrape traverses the **192.168.100 mgmt plane only**. RDMA fabric NIC counters
  are read via textfile collector, but the scrape connection itself must NOT
  touch 192.168.200 (that would perturb the workload under test).

## Coverage

| Signal                             | Source                                 |
|------------------------------------|----------------------------------------|
| CPU, memory, per-NUMA, load        | node_exporter (default collectors)     |
| Per-block-device I/O               | node_exporter `diskstats`              |
| NIC counters (RDMA fabric)         | textfile collector wrapping `ethtool -S ens1f1np1` |
| NVMe SMART / per-ns queue          | textfile collector wrapping `nvme smart-log` |
| LMCache internal counters (Stage 2+) | textfile collector wrapping app stats JSON |

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

- Bind exporter to 192.168.200 addresses.
- Run docker on mkp1 or mkp2.
- Enable prometheus scraping during the DDIO-knee FIO preflights — background
  cache/PCIe traffic invalidates the measurement.
- Reconfigure MTU or bring links down on 192.168.100 while an exporter target
  is being added — same "do not touch mgmt plane" rule as everything else.
