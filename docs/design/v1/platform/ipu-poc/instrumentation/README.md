# mkp-instrumentation — observability kit for NVMe-oF / RDMA test pairs

Everything needed to reproduce the mkp1/mkp2 monitoring stack on another pair of
test hosts. Two halves:

- **`host/`** — runs *on each test host*: node_exporter + six textfile
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
ssh <newhost> '
  RDMA_FABRIC_IFACE=<fabric-iface> \
  ACC_TELEMETRY_ENDPOINT=<acc-ip>:50051 \
  ACC_TELEMETRY_PROTO_DIR=<feature-pack>/ipu_client/config \
  bash /tmp/obs-kit/install.sh'
```

`install.sh` installs deps (`nvme-cli`, `ethtool`, `jq`, Intel PCM), fetches node_exporter
1.8.2 if absent, creates the `node_exporter` system user and
`/var/lib/node_exporter/textfile`, installs the six collectors and thirteen
units, enables the timers, and verifies freshness plus series counts. It is
idempotent. It also requires Python's `grpc` module and the generated
`telemetry_pb2.py` and `telemetry_pb2_grpc.py` files in
`ACC_TELEMETRY_PROTO_DIR`.

**All three variables are required and any one missing aborts the script.**
`SKIP_ACC_TELEMETRY=1` stands in for the two `ACC_TELEMETRY_` ones on a host with
no accelerator to point them at, and installs five collectors and eleven units
instead. Skipping it gives up the authoritative payload-byte source — see
[Load-bearing constraints](#load-bearing-constraints) on why the NIC counters are
not a substitute.

Four collectors are **not** wired by `install.sh` because they are rig-specific:
`pcm_pcie_textfile.sh` and `mmgt_nic_textfile.sh` (with their units and the
`pcm-memory` drop-in) are the MMG-400 target's set — `mmgt` runs no `rdma-nic` or
`rdma-hwcounters` at all — and the ACC-over-SSH collector is separate again. See
[ACC telemetry on the MMG-400 rig](#acc-telemetry-on-the-mmg-400-rig).

`RDMA_FABRIC_IFACE` is **required** — it pins `rdma-nic.service` to the fabric
NIC via a drop-in, leaving the collector script byte-identical fleet-wide (`md5sum`
is then a useful drift check). Point it at the **RDMA fabric** interface, never
the SSH management interface. On mkp1/mkp2 that is `ens2f0` (200.0.0.35 /
200.0.0.37); management is `ens101f0` (10.166.97.x). The script refuses to run
without it and prints the candidate interfaces.

Verify:

```bash
ssh <newhost> 'systemctl list-timers --all | grep -E "rdma|nvme|pcm|numa"'
ssh <newhost> 'curl -s localhost:9100/metrics | grep -c ^rdma_hw_counter{'
```

### 2. Control host

Edit `prometheus.yml` so the `targets` and the `relabel_configs` regexes match
your local tunnel ports and host names. There are eleven relabel rules across the
three jobs — five in `node`, four in `lmcache_bench`, two in
`lmcache_bench_mmg` — and `lmcache_bench` also carries `host` and `workload` as
static labels that have to change with them. Then:

```bash
export HOSTS="<newhost1>:19100 <newhost2>:19101"
./up.sh
```

The built-in default is `mkp1:19100 mkp2:19101 mmgt:19106 mmgi0:19107
mmgi1:19108` — the older NVMe-oF pair plus the three MMG-400 hosts. **Set
`HOSTS` on every invocation** on any other rig. `ensure_tunnel` uses
`ExitOnForwardFailure` under `set -e`, so a host you do not have aborts `up.sh`
at that tunnel rather than leaving a down target behind — exporting `HOSTS` once,
as above, is what keeps the re-runs working.

`up.sh` opens the SSH tunnels (idempotent), starts Docker Desktop if needed,
brings up the compose stack, reloads Prometheus, and prints target health.
Grafana provisions the datasource and dashboard automatically.

## What's in the box

| Path | Runs on | Purpose |
|---|---|---|
| `host/install.sh` | test host | Idempotent installer + verifier |
| `host/bin/rdma_hwcounters_textfile.sh` | test host | irdma `hw_counters` — directional RDMA operation counters |
| `host/bin/rdma_nic_textfile.sh` | test host | `ethtool -S` fabric-NIC counters — traffic-presence diagnostic only |
| `host/bin/acc_telemetry_textfile.py` | test host | ACC gRPC RC byte counters — authoritative Falcon payload-byte source |
| `host/bin/nvme_stats_textfile.sh` | test host | NVMe SMART per namespace |
| `host/bin/pcm_memory_textfile.sh` | test host | Intel PCM DRAM read/write bandwidth per socket |
| `host/bin/numa_stats_textfile.sh` | test host | kernel node memory, allocation, and CPU-time counters |
| `host/bin/pcm_pcie_textfile.sh` | mmgt only | Intel PCM PCIe/DDIO bandwidth per socket — feeds the LLC hit% and DDIO absorption panels |
| `host/bin/mmgt_nic_textfile.sh` | mmgt only | `ethtool -S` on both fabric ports; replaces `rdma_nic` on the MMG-400 target |
| `host/systemd/*.service`, `*.timer` | test host | node_exporter + one timer per collector |
| `host/systemd/pcm-memory.service.d/pcm.conf` | mmgt only | Pins `PCM_MEMORY_BIN` to the dated PCM build |
| `prometheus.yml` | control | 5s scrape of the node tunnels (relabelled to `host=`) plus the `lmcache_bench` and `lmcache_bench_mmg` initiator ports (relabelled to `initiator=`) |
| `docker-compose.yml` | control | Prometheus 2.55.1 + Grafana 11.3.0, loopback-bound |
| `provisioning/` | control | Grafana datasource (uid `PROM`) + dashboard provider |
| `dashboards/lmcache-mkp.json` | control | 26 panels, uid `ipu-poc-mkp-stub` — provisioned copy; LMCache row first |
| `up.sh` | control | Tunnels + stack + health check |

### The `lmcache_bench` job

`bench l2 --serve-metrics <port> --metrics-bind-address 127.0.0.1` publishes
live submit/success/bytes counters for the lifetime of one CLI process. The
multi-initiator drivers assign `METRICS_BASE_PORT + id` (default base 9101), so
`prometheus.yml` configures four targets and `up.sh` can open the matching
tunnels:

```bash
BENCH_TUNNELS=1 ./up.sh                      # initiators 0..3 -> :19102-19105
BENCH_TUNNELS=1 BENCH_INITIATORS=2 ./up.sh   # just 0..1
```

Off by default — with no benchmark running these are four permanently-down
targets. Override `BENCH_HOST`, `BENCH_REMOTE_BASE`, `BENCH_LOCAL_BASE` to match
a different rig, keeping them aligned with `prometheus.yml`.

`run_model_geometry.sh` accepts `METRICS_PORT` and forwards it to `bench l2`.
The host-only geometry coordinator uses one endpoint per host; use a
multi-initiator driver only when a host deliberately runs more than one
benchmark process.

Two properties matter when reading the series:

- **The target is down between cells.** The endpoint lives only as long as one
  `bench l2` invocation, so `up == 0` between sweep cells is expected, not a
  scrape failure. A 120 s cell at the 5 s interval yields roughly 24 samples.
- **`phase` separates `warmup` from `measured`**, which is what lets a
  Prometheus-side rate be compared against the run's JSON independently. Rate
  over the measured phase only; including warmup biases it.

### The `lmcache_bench_mmg` job

Same endpoint and the same metric names on the MMG-400 rig, where the load runs
on two initiator hosts (`mmgi0`, `mmgi1`) against target `mmgt`. It gets its own
job rather than sharing `lmcache_bench`: the mkp dashboard's LMCache panels filter
on `job` alone, so a shared name would fold mmg series into the mkp aggregate.

The local port encodes both host and initiator id — `1911x` is `mmgi0`, `1912x`
is `mmgi1`, and the **last digit is the id**, which is what the relabel rules key
on. The remote side stays `METRICS_BASE_PORT + id`:

```bash
MMG_BENCH_TUNNELS=1 ./up.sh                          # mmgi0+mmgi1, ids 0..3
MMG_BENCH_TUNNELS=1 MMG_BENCH_INITIATORS=2 ./up.sh   # just ids 0..1 on each
```

`MMG_BENCH_HOSTS="mmgi0:19110 mmgi1:19120"` sets the host-to-local-base map; keep
it aligned with `prometheus.yml`.

The id is **per host, not global**, so `mmgi0 init 0` and `mmgi1 init 0` are two
separate benchmark processes. `run_geometry_inventory.sh sweep` runs one process
per host and exposes only id 0. Open only those two tunnels for that workflow:

```bash
HOSTS="mmgt:19106 mmgi0:19107 mmgi1:19108" \
  MMG_BENCH_TUNNELS=1 MMG_BENCH_INITIATORS=1 ./up.sh
```

Ports for ids 1–3 are reserved for an explicit multi-process run and are expected
to stay down during the geometry sweep. Install the bench with
`scripts/ipu-poc/install_bench_l2_handoff.sh` on each initiator first.

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

Collector intervals: `rdma-hwcounters` 2s, `rdma-nic` 5s, `acc-telemetry` 5s,
`pcm-memory` 5s, `numa-stats` 5s, `nvme-stats` 15s. PCM takes a one-second measurement inside
each five-second cycle. SMART reads issue an admin command per namespace, hence
the slower cadence; NIC, RDMA, and NUMA counters are cheap reads wanted at
fabric resolution.

### Memory and NUMA telemetry

`pcm-memory` exports socket-level DRAM controller bandwidth in PCM's reported
MB/s. It measures all host memory traffic, including unrelated processes and
kernel activity; it is not a substitute for application goodput or wire-byte
measurement. The collector emits `pcm_memory_collector_success=0` if PCM is
unavailable or its CSV output cannot be parsed, rather than presenting an old
successful sample as current.

The NUMA collector exports `MemTotal`, `MemFree`, and `MemUsed` from each
node's kernel `meminfo`, plus per-node `numa_hit`, `numa_miss`,
`numa_foreign`, `local_node`, and `other_node` counters. They are host-wide
kernel counters. A change during a measured cell is useful diagnostic evidence;
their absolute values do not belong to the benchmark process alone.

## Falcon host bring-up is the platform team's step, not ours

Loading `idpf`/`irdma` and configuring the fabric interfaces on `mmgt`, `mmgi0`
and `mmgi1` belongs to the platform team. The benchmark side assumes RDMA is
already up and starts at the NVMe-oF layer. This section records the as-built
facts so they are not lost; it is not a procedure to run.

There was a `falcon_host_setup.sh` here that reproduced the bring-up. It was
removed, because owning another team's step is a trap in a specific way: the
modules are insmod'd from build trees the platform team rebuilds on its own
schedule, so any path we pin goes stale silently. It did — the pinned path had
been superseded on all three hosts by a newer build of the same version, so
running the script would have downgraded the driver without any error. Ask the
platform team which build is current instead of pinning one here.

### As-built fabric map

MTU 9100 on every fabric interface. The 192.168.100-equivalent management path
is not involved; only these addresses are fabric.

| Host | Interface | Address | Routes |
|---|---|---|---|
| `mmgi0` | `ens7f0` | `200.0.4.2/24` | `200.0.3.0/24` and `200.0.6.0/24` via `200.0.4.1` |
| `mmgi1` | `ens7f0` | `200.0.3.2/24` | `200.0.4.0/24` and `200.0.5.0/24` via `200.0.3.1` |
| `mmgt` | `enp45s0f0` | `200.0.5.2/24` | `200.0.3.0/24` via `200.0.5.1` |
| `mmgt` | `enp79s0f0` | `200.0.6.2/24` | `200.0.4.0/24` via `200.0.6.1` |

Each initiator routes to exactly one target address, which is what fixes the
port split in `setup_mmgt_nvmeof_target.sh`: **nvmet port 1 (`200.0.6.2`) serves
`mmgi0`, port 2 (`200.0.5.2`) serves `mmgi1`.**

### Ordering constraints worth knowing

`idpf` loads first, the interfaces are configured on the `idpf` netdev, and
`irdma` loads last. `ice` must not be resident when `irdma` loads — that
combination has been observed to crash the host. IMC and ACC are a separate
persistent domain and are untouched by a host reboot.

### Assert bring-up, don't reproduce it

The failure mode here is silent: on 2026-08-28 `rdma link show` reported
`state ACTIVE physical_state LINK_UP` and `ibv_devinfo` reported
`PORT_ACTIVE (4)` for a full day while memory-region registration was dead and
every NVMe-oF controller had gone. **Never read fabric health off link state.**
Two checks that do work:

- `rdma resource show` — a healthy device has a nonzero `mr` count. `mr 0`
  means registration is failing even though the link looks up.
- A live NVMe-oF controller over `rdma` transport (`nvme list-subsys`) is
  positive proof, since it cannot exist without successful MR registration.
  This is what `run_geometry_inventory.sh preflight` gates on, and it is the
  right gate — it fails before a sweep rather than midway through one.

Module identity needs care too: three distinct `irdma` builds on this rig all
report `version 0.0.129-hvl`. Only `/sys/module/irdma/srcversion` identifies
what is loaded. `modinfo irdma` resolves by path and on `mmgi0` names a build
that is not the running one.

## ACC telemetry on the MMG-400 rig

The MMG-400 hosts (`mmgt`, `mmgi0`, `mmgi1`) use `tele_cli` through the IMC for
the deployed ACC panels. `mmgt` also has an optional gRPC shadow path. It keeps
the IMC connection in each IPU namespace and binds the forwarded ACC gRPC port
on host loopback. The dashboard stays on the deployed `tele_cli` counters until
a fixed-cell comparison approves a cutover.

| Path | Purpose |
|---|---|
| `scripts/ipu-poc/install_acc_stats.sh` | Installs the collector (and node_exporter if absent) on one host. Idempotent. |
| `scripts/ipu-poc/acc_ssh_stats.py` | Samples ACC core usage and `tele_cli -t global` into separate textfiles |
| `scripts/ipu-poc/acc-stats.service`, `.timer` | ACC core busy gauges, every 30 s |
| `scripts/ipu-poc/acc-transport.service`, `.timer` | Falcon transport counters, every 10 s |
| `scripts/ipu-poc/acc_grpc_tunnel.py` | Persistent target-only IMC → ACC gRPC tunnel and protobuf staging |
| `scripts/ipu-poc/acc-grpc-*` units | Optional 5 s target-only gRPC shadow collector |
| `scripts/ipu-poc/acc_capture_runbook.sh` | Serial-console capture plus periodic `tele_cli` injection, for crash forensics |
| `scripts/ipu-poc/sync_capture_bundle.sh` | Pulls capture dirs back to the laptop |

Install it per host, staging node_exporter from a host that already has the
matching build:

```bash
scp mmgt:/usr/local/bin/node_exporter /tmp/node_exporter
cd scripts/ipu-poc
IMC_PASSWORD=<imc-root-pw> NODE_EXPORTER_BIN=/tmp/node_exporter \
  ./install_acc_stats.sh mmgi0 ':acc1:200.0.4.3'
IMC_PASSWORD=<imc-root-pw> NODE_EXPORTER_BIN=/tmp/node_exporter \
  ./install_acc_stats.sh mmgi1 ':acc1:200.0.3.3'
IMC_PASSWORD=<imc-root-pw> ./install_acc_stats.sh mmgt      # two-card default
```

Then add the node targets and tunnels — `mmgt:19106`, `mmgi0:19107`,
`mmgi1:19108` are already in `up.sh`'s `HOSTS` default and `prometheus.yml`'s
`node` job, so `./up.sh` picks them up.

To install the target-only shadow path, use the same installer with
`ACC_GRPC_SHADOW=1`:

```bash
IMC_PASSWORD=<imc-root-pw> ACC_GRPC_SHADOW=1 \
  ./install_acc_stats.sh mmgt
```

It creates `/opt/acc-grpc-telemetry/venv` if needed, installs `grpcio` and
`protobuf`, and copies `telemetry_pb2.py` plus `telemetry_pb2_grpc.py` from the
running ACC's
`/opt/falcon/tools/controller/python_out`. The host needs package access on
that first install. `ACC_GRPC_PYTHON`, `ACC_GRPC_PROTO_DIR`, and
`ACC_GRPC_PROTO_SOURCE` override those paths when a controlled runtime or
feature-pack location is required. On an offline target, pre-stage a compatible
gRPC runtime and set `ACC_GRPC_PYTHON`; do not point the collector at a
benchmark virtualenv.

**The IMC hop is namespaced on the target but not on the initiators.** On `mmgt`
each card's IMC management vport lives in its own netns (`IPU1`, `IPU2`), so the
path is `ip netns exec <netns> ssh root@100.0.0.100`. On `mmgi0`/`mmgi1` the IMC
link is a plain host interface holding `100.0.0.1/24` and there is no netns at
all — an **empty netns field** in `ACC_TARGETS` selects the direct path. Getting
this wrong is the difference between working telemetry and a silent timeout.

The ACC fabric IP is the host fabric address with the last octet set to `.3`:
`mmgi0` is `200.0.4.2` → ACC `200.0.4.3`, `mmgi1` is `200.0.3.2` → ACC
`200.0.3.3`, and `mmgt`'s two cards are `200.0.6.3` and `200.0.5.3`. Those do not
answer ping from the host — the ACC is reached through the IMC, and the fabric IP
is only `tele_cli`'s `-s` argument. **A failed ping proves nothing here.**

**No credential is baked into the collector or the unit.** `IMC_PASSWORD` — and
`ACC_TARGETS` on the initiators — are read from `/etc/default/acc-stats`, written
mode 600 by the installer, which keeps the script and the unit byte-identical
fleet-wide. The collector exits non-zero with a usage message if `IMC_PASSWORD` is
unset rather than hanging on an unanswered prompt.

Core gauges and transport counters are separate to avoid duplicate Prometheus
series: `acc_stats.prom` contains `acc_cpu_busy_percent`; `acc_transport.prom`
contains `acc_tele_field`.

The optional gRPC shadow writes `acc_grpc_acc1.prom` and
`acc_grpc_acc2.prom` with `acc_telemetry_bytes_total{acc=...,counter=...}`.
It is intentionally not a dashboard input yet. Compare `increase()` for both
sources over the exact benchmark interval; only then replace the polling
series.

Two traps when reading the resulting series:

- **Transport counters update every 10 s but Prometheus scrapes at 5 s**, so
  `acc_tele_field` remains a staircase. Use `rate(...[60s]) * 8` for the live
  RDMA panels so their headline window matches NVMe and LMCache goodput.
  For a completed benchmark cell, use `increase()` over its exact measured
  interval rather than a dashboard trend window.
- **The expected poll-mode floor is ~2.0 busy cores per reporting IPU.** An
  initiator reading 2.0 is idle, not broken; `mmgt` reads ~4.0 because it has two
  cards.

Verify with `curl -s --noproxy '*' localhost:9100/metrics | grep -c
'^acc_cpu_busy_percent'`. **`--noproxy` is required, not cosmetic** — `mmgi0`
carries a curl proxy config that intercepts even `localhost` and answers
`/metrics` with a `403`, which looks exactly like a broken exporter.

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

**`ethtool -S` is diagnostic only on Falcon.** Its port-byte samples arrive
through a textfile and can alias with Prometheus scrapes: a short rate window
can enclose two collector updates or none. It is useful to establish that the
port is active and to inspect pause or error counters, but it is not the
payload-throughput authority.

Driver spelling is inconsistent: `port-rx_bytes` but `port-tx-bytes`. The
collector's `gsub` normalizes both to `port_rx_bytes` / `port_tx_bytes`.

**Falcon is NOT RoCE: the byte-conversion rules below do not carry over.**
Neither `rdma stat show link` nor `/sys/class/net/<iface>/statistics/rx_bytes`
work on Falcon/MEV — measured, mkp1 sent 1.25 GiB of RDMA WRITE and netdev
`tx_bytes` moved **140 bytes**. `hw_counters` *do* work, but as transaction
counters only.

**RDMA counter direction is asymmetric** (NVMe-oF over irdma, measured):

| Operation | Wire mechanism | Counter | Interpretation |
|---|---|---|---|
| NVMe-oF READ | target RDMA-**writes** into initiator memory | `InRdmaWrites` | transaction count; **no valid fixed byte constant on Falcon** |
| NVMe-oF WRITE | target RDMA-**reads** initiator memory | `InRdmaReads` | transaction count |

`OutRdmaWrites` stays 0 on the initiator. Payloads ≤4 KiB ride in-capsule
(`OutRdmaSends` only, zero RDMA r/w ops).

On Falcon, bytes per `InRdmaWrite` is stable *within* a block size (±0.3% across
reps) but varies **13× across** block sizes — measured over all 20 cells of the
2026-08-12 `remote_xfs` read sweep:

| block size | bytes per `InRdmaWrite` |
|---|---|
| 4k | 3,523 |
| 16k | 14,004 |
| 144k | 38,991 |
| 256k | 45,073 |
| 512k | 45,084 |

It saturates near 45 KB, so the 52,428 B RoCE segment cap does **not** hold
here. Treat `InRdmaWrites` as a traffic-shape gate and never compare its rate
across block sizes. The per-cell counter/app-byte ratio used by
`run_fio_capacity_sweep.sh` stays valid because it is computed at fixed block
size — the only regime where the counter is stable.

**ACC telemetry is the authoritative Falcon payload-throughput source.** `rtcmd`
exposes a gRPC telemetry service on the ACC (`10.0.0.35:50051` for mkp1,
`10.0.0.37:50051` for mkp2). The collector uses the feature-pack generated
`telemetry_pb2_grpc.TelemetryStub` with `HOST0` and `ULP_RDMA`. `ULP_NVME` does
not expose the live RC byte counters on this rig.

The controlled 60-second, O_DIRECT Llama-3.1 405B read on 2026-08-12 moved
715,399,888,896 application bytes. `mkp1 bytes_to_ulp` increased by
725,366,413,360 bytes (1.0139x), while `mkp2 bytes_from_ulp_rc` increased by
720,399,569,128 bytes (1.0070x). The primary dashboard panel displays those
two host-local views of the **same NVMe-oF read payload**; never sum them.
Both are within the runbook's +/-5% counter/app acceptance band. The other two
counters moved only 0.03-0.04%, consistent with control traffic.

This establishes the read direction only. Store and mixed-direction mappings
remain unvalidated, so the dashboard must not infer them from counter names.

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

- **Store and mixed ACC direction mappings remain unvalidated.** The primary
  panel is deliberately scoped to NVMe-oF reads; do not relabel the remaining
  counters from their names alone.
- **The host-only geometry sweep uses one process per host.** It publishes id 0
  on each initiator; the remaining per-host ports are intentionally idle unless
  a multi-process driver is selected.
- **The dashboard is hardcoded to `mkp1` and `mkp2`.** 23 of its 43 queries pin
  one of those two host labels, and the Block I/O row additionally pins the role
  split by device — `mkp1` to `md.+` (initiator RAID0), `mkp2` to `nvme.+`
  (target SSDs). On a differently-named rig roughly half the panels render empty,
  which reads as a broken stack rather than a naming mismatch. Relabelling the
  two hosts to `mkp1`/`mkp2` in `prometheus.yml` is cheaper than editing the
  queries and is what the panel titles assume.
- **No NVMe IOPS or latency panels** — only throughput, queue depth, and SMART
  temperature.
- **No SMART temperature panel anymore.** The role-split Block I/O row replaced
  the old NVMe row, which carried it.
- **Prometheus does not replace in-process measurement for per-run figures.** A
  1–15 s scrape is too coarse for host CPU per GB in particular; that stays
  bracketed in-process around the measured window. The scrape is for live
  mid-run visibility and an independent cross-check of the run's own JSON, not
  for the headline numbers, which the `results/` docs take from the JSON.
- SELinux is `Enforcing` on mkp1/mkp2 and needed no policy work, since
  node_exporter reads only `/var/lib/node_exporter/textfile`. A different
  textfile directory may need a label.
