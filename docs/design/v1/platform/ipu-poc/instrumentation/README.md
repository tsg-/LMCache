# mkp-instrumentation — observability kit for NVMe-oF / RDMA test pairs

Everything needed to reproduce the mkp1/mkp2 monitoring stack on another pair of
test hosts. Two halves:

- **`host/`** — runs *on each test host*: node_exporter + three textfile
  collectors, each driven by its own systemd timer.
- **root of this dir** — runs *on the laptop/control host*: SSH tunnels plus
  Prometheus and Grafana in Docker.

Two metrics flows, both loopback-bound and tunnelled:

- **host counters** — collector script → `.prom` file → node_exporter on
  `127.0.0.1:9100` → SSH tunnel → Prometheus (5s scrape) → Grafana.
- **`bench l2` counters** — the CLI's own `--serve-metrics` endpoint on
  `127.0.0.1:9101+` → SSH tunnel → same Prometheus, job `lmcache_bench`. Short
  lived: it exists only while a benchmark process runs.

node_exporter binds to loopback only; the SSH tunnel is the sole exposure. No
firewall changes are needed on the test hosts.

## Replicate on a new pair

### 1. Per test host

```bash
scp -r host <newhost>:/tmp/obs-kit
ssh <newhost> 'RDMA_FABRIC_IFACE=<fabric-iface> bash /tmp/obs-kit/install.sh'
```

`install.sh` installs deps (`nvme-cli`, `ethtool`, `jq`), fetches node_exporter
1.8.2 if absent, creates the `node_exporter` system user and
`/var/lib/node_exporter/textfile`, installs the three collectors and seven units,
enables the timers, and verifies freshness plus series counts. It is idempotent.

`RDMA_FABRIC_IFACE` is **required** — it pins `rdma-nic.service` to the fabric
NIC via a drop-in, leaving the collector script byte-identical fleet-wide (`md5sum`
is then a useful drift check). Point it at the **RDMA fabric** interface, never
the SSH management interface. On mkp1/mkp2 that is `ens2f0` (200.0.0.35 /
200.0.0.37); management is `ens101f0` (10.166.97.x). The script refuses to run
without it and prints the candidate interfaces.

Verify:

```bash
ssh <newhost> 'systemctl list-timers --all | grep -E "rdma|nvme"'
ssh <newhost> 'curl -s localhost:9100/metrics | grep -c ^rdma_hw_counter{'
```

### 2. Control host

Edit `prometheus.yml` so the `targets` and the two `relabel_configs` regexes
match your local tunnel ports and host names, then:

```bash
HOSTS="<newhost1>:19100 <newhost2>:19101" ./up.sh
```

`up.sh` opens the SSH tunnels (idempotent), starts Docker Desktop if needed,
brings up the compose stack, and prints target health. Grafana provisions the
datasource and dashboard automatically.

## What's in the box

| Path | Runs on | Purpose |
|---|---|---|
| `host/install.sh` | test host | Idempotent installer + verifier |
| `host/bin/rdma_hwcounters_textfile.sh` | test host | irdma `hw_counters` — the only exact RDMA instrument here |
| `host/bin/rdma_nic_textfile.sh` | test host | `ethtool -S` on the fabric NIC — control plane only |
| `host/bin/nvme_stats_textfile.sh` | test host | NVMe SMART per namespace |
| `host/systemd/*.service`, `*.timer` | test host | node_exporter + one timer per collector |
| `prometheus.yml` | control | 5s scrape of the node tunnels (relabelled to `host=`) plus the `lmcache_bench` initiator ports (relabelled to `initiator=`) |
| `docker-compose.yml` | control | Prometheus 2.55.1 + Grafana 11.3.0, loopback-bound |
| `provisioning/` | control | Grafana datasource (uid `PROM`) + dashboard provider |
| `dashboards/lmcache-mkp.json` | control | 16 panels, uid `ipu-poc-mkp-stub` — provisioned copy |
| `up.sh` | control | Tunnels + stack + health check |

### The `lmcache_bench` job

`bench l2 --serve-metrics <port> --metrics-bind-address 127.0.0.1` publishes
live submit/success/bytes counters for the lifetime of one CLI process. The
multi-initiator drivers assign `METRICS_BASE_PORT + id` (default base 9101), so
forward one local port per initiator:

```bash
ssh -N -L 19102:127.0.0.1:9101 mkp1    # initiator 0
ssh -N -L 19103:127.0.0.1:9102 mkp1    # initiator 1, and so on
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

### Cheap intermediate (no Prometheus)

For single-run correlation without standing up the stack:

```bash
# on each host, timestamped CSV to a per-run dir
dstat --output run.dstat.csv --cpu --mem --net -N ens2f0 --disk -D nvme0n1,nvme1n1 --nvme 1 &
nvme iostat > run.nvme.csv &
```

Aligned timestamps plus a single-node view is ~80% of the value. Upgrade to the
full stack when you want live mid-run visibility or a second viewer.

Collector intervals: `rdma-hwcounters` 2s, `rdma-nic` 5s, `nvme-stats` 15s.
SMART reads issue an admin command per namespace, hence the slower cadence; NIC
and RDMA counters are cheap sysfs/ioctl reads wanted at fabric resolution.

## Load-bearing constraints

Each of these was learned by getting it wrong first. Preserve them when adapting.

**Timers, not cron.** The original deployment drove `nvme_stats` and `rdma_nic`
from crontab. A later `sed -i "/rdma_hwcounters_textfile/d"` cleanup took the
whole crontab with it, leaving both scripts executable with nothing invoking
them. node_exporter kept serving the stale `.prom` files as if current — which
renders in Grafana as a flat line, **indistinguishable from a quiet fabric**.
Drift before repair: mkp1 ~3h, mkp2 ~12h, 254.3 GiB of unaccounted
`port_rx_bytes`. `install.sh` warns if a cron entry still races the timers.

**Treat exporter freshness as a dashboard prerequisite.** Check `.prom` mtimes
before trusting any side-by-side comparison.

**node_exporter's built-in `infiniband` collector does not work on irdma.** It
reads `ports/<p>/counters/`; irdma exposes only `hw_counters/`. The collector
hard-fails with `node_scrape_collector_success{collector="infiniband"} 0` and
emits nothing. `rdma_hwcounters_textfile.sh` exists to replace it.

**`ethtool -S` is blind to RDMA payload on irdma.** Measured 2026-08-03: a 34 GB
RDMA read moved `port_rx_bytes` by ~3.8 KB. The `rdma_nic_stat` byte panels show
control traffic only — they look alive while being blind to the workload under
test. Do not build a throughput gate on NIC bytes.

**RDMA counter direction is asymmetric** (NVMe-oF over irdma/RoCEv2, measured):

| Operation | Wire mechanism | Counter | Bytes per op |
|---|---|---|---|
| NVMe-oF READ | target RDMA-**writes** into initiator memory | `InRdmaWrites` | `ceil(bytes / 52428)` |
| NVMe-oF WRITE | target RDMA-**reads** initiator memory | `InRdmaReads` | 4096, block-size invariant |

`OutRdmaWrites` stays 0 on the initiator. Payloads ≤4 KiB ride in-capsule
(`OutRdmaSends` only, zero RDMA r/w ops). Verified from both ends: mkp1
`InRdmaWrites` == mkp2 `OutRdmaWrites` exactly. The dashboard's byte-rate panels
are labelled `[TREND ONLY]` because they multiply op counts by these constants.

**irdma refreshes `hw_counters` asynchronously (~1s).** A delta sampled
immediately after a workload ends can read 0 — again indistinguishable from no
traffic. Settle ≥1s; the ad-hoc probes use 2s.

**Sampling aliasing is real.** irdma ~1s + textfile timer + 5s scrape + 15s rate
window understates peaks: a 94.4 Gbps bench read rendered as a 75.6 Gbps peak.
Rate panels need a **≥60s measurement window** to be meaningful.

**Grafana datasource uid must be `PROM`.** The dashboard JSON pins it. Changing
it in `provisioning/datasources/prometheus.yml` imports the dashboard with every
panel broken.

**The dashboard provider path must not nest inside the provisioning mount.**
`/etc/grafana/provisioning/dashboards/json` fails at container init — you cannot
bind-mount under an already read-only bind mount. The JSON lives at
`/var/lib/grafana/dashboards` instead.

**`dashboards/lmcache-mkp.json` is the only dashboard.** It is what compose
bind-mounts and what the provider loads; edit that one. An earlier redundant
hand-import copy at the kit root was dropped.

**The Grafana admin password is not committed.** `docker-compose.yml` declares
`GRAFANA_PASSWORD` required with no default, so bringing the stack up without it
fails fast rather than silently accepting a known credential. `up.sh` generates
a local secret into `.env` (mode 600, gitignored) on first run; export
`GRAFANA_PASSWORD` yourself to override.

## Known gaps

- **NVMe panels exclude `md0`.** They filter `device=~"nvme.+"`, so the RAID0
  aggregate is invisible. Widen to `device=~"nvme.+|md.+"` to see it.
- **No NVMe IOPS or latency panels** — only throughput, queue depth, and SMART
  temperature.
- **The dashboard has no LMCache panels yet.** `bench l2` now *does* serve
  metrics (see the `lmcache_bench` job below), and `prometheus.yml` scrapes it,
  but `dashboards/lmcache-mkp.json` still ends in an empty placeholder row —
  the series arrive with nowhere to render.
- **Prometheus does not replace in-process measurement for per-run figures.** A
  1–15 s scrape is too coarse for host CPU per GB in particular; that stays
  bracketed in-process around the measured window. The scrape is for live
  mid-run visibility and an independent cross-check of the run's own JSON, not
  for the headline numbers, which the `results/` docs take from the JSON.
- SELinux is `Enforcing` on mkp1/mkp2 and needed no policy work, since
  node_exporter reads only `/var/lib/node_exporter/textfile`. A different
  textfile directory may need a label.
