# MMG-400 telemetry

Prometheus, Grafana, and host/device exporters for operators running `bench l2`
on the five-node MMG-400 testbed. `mmgt` exports NVMe-oF storage through four
ACCs; `mmgi0`–`mmgi3` generate load. `mmgi1` also runs the monitoring containers.
The dashboard shows operational telemetry, not inference TTFT.

## Starting state

The quickstart assumes collectors are installed and the kit is available at
`mmgi1:/root/lmcache-telemetry`. That deployment is an unmanaged copy: compare
files and preserve a rollback copy before updating it. For collector installation,
run the commands below from this repository's root with root SSH access.

On `mmgi1`, provide Docker with Compose, `ssh`, `lsof`, `openssl`, `curl`, and
Python 3. SSH forwarding must work to all five hosts, including `mmgi1` itself.
Prometheus retains up to 30 days or 100 GB, whichever limit is reached first.
Check `docker info --format '{{.DockerRootDir}}'` and available space there;
use a capacity-checked data filesystem, such as `/home/docker`, rather than
assuming the root filesystem can hold the telemetry history.

## Quickstart

### Start the stack

On `mmgi1`:

```bash
ssh mmgi1
cd /root/lmcache-telemetry
COLLECTOR_CHECKS='' MMG_BENCH_TUNNELS=1 ./up.sh
```

- `MMG_BENCH_TUNNELS=1` opens the four benchmark tunnels. Omit it for host health only.
- `COLLECTOR_CHECKS=''` skips remote freshness probes for a forwarding-only SSH
  key. Omit this override with a key that permits remote commands; either way,
  check collector freshness before a sweep.
- Re-running `up.sh` reuses listening tunnel ports, starts Compose, and reloads
  Prometheus. Apply config changes before a sweep; recreation can lose samples.

Tunnels do not start benchmark endpoints. Add `--serve-metrics 9101
--metrics-bind-address 127.0.0.1` to the `bench l2` invocation on `mmgi0`, using
ports `9102`, `9103`, and `9104` on the other initiators. Benchmark queries must
filter `phase="measured"` to exclude warmup.

### Open Grafana

From your laptop, forward the Grafana port:

```bash
ssh -N -L 3000:127.0.0.1:3000 mmgi1
```

Open [the target dashboard](http://127.0.0.1:3000/d/mmgt-storage-target).
Anonymous access is read-only. On first run, `up.sh` generates an admin password
in `.env` if none exists; `GRAFANA_PASSWORD` supplies an alternative.

To stop the containers on `mmgi1`, run `docker compose stop` in the kit directory.
This preserves history and leaves SSH tunnels running. `up.sh` restarts the stack;
do not use `docker compose down -v`, which deletes Prometheus and Grafana volumes.

## Preflight health checks

On `mmgi1`, open `http://127.0.0.1:9090/graph` or query its API. For laptop access,
add `-L 9090:127.0.0.1:9090` to the Grafana SSH command. Verify:

| Query | Expected Value | Meaning |
|---|---|---|
| `up{job="node"}` | 5 series, each `1` | All 5 hosts (`mmgt`, `mmgi0`–`mmgi3`) reachable |
| `up{job="acc_grpc",host="mmgt"}` | `1` | Target ACC HTTP exporter reachable |
| `acc_telemetry_exporter_complete{job="acc_grpc",host="mmgt"}` | `1` | All four ACC reads succeeded |
| `acc_telemetry_collector_success{job="acc_grpc",host="mmgt"}` | 4 series, each `1` | Each target ACC read succeeded |
| `count by (acc) (acc_cpu_busy_percent{job="node",host="mmgt",core=~"cpu[0-9]+"})` | 4 series, each `4` | CPU telemetry covers four cores on each ACC |
| `node_textfile_scrape_error{job="node"}` | 5 series, each `0` | Textfiles parse successfully |
| `time() - node_textfile_mtime_seconds{job="node"}` | Check each file's age | Detect stopped collectors even when `up=1` |

For the age check, require expected files to be present as well as fresh.
Use the limits in [`up.sh`](up.sh): on `mmgt`, PCM memory/PCIe, NUMA, and NIC
files should be no older than 15 s; NVMe SMART, 45 s; ACC CPU, 90 s.
Initiator QP files should be no older than 90 s. Require
`pcm_memory_collector_success` and `pcm_pcie_collector_success` to be `1`:
fresh failure files are not valid samples.

An ACC read failure reports `success=0` and omits that card's counters rather
than replaying stale data. Aggregate RDMA panels are suppressed when the
exporter is incomplete; per-card panels are gated by that card's success.
Benchmark endpoints are process-local and report `up=0` between cells. During
a cell, check all four targets; a dead tunnel is not an idle benchmark.

## Interpreting the dashboard

### Payload counters and direction

Use ACC gRPC byte counters for Falcon payload telemetry. The dashboard selects
`acc_tele_field{job="acc_grpc",host="mmgt",field="bytes_from_ulp_rc"}` and
`field="bytes_to_ulp"`; the exporter also publishes them as
`acc_telemetry_bytes_total` with a `counter` label.

OS netdev statistics (`/sys/class/net/.../statistics/*`) do not account for
Falcon offloaded RDMA payload on this rig. `ethtool -S` hardware counters are a
different family: their coverage depends on the named counter and driver, and
they are not the payload authority here. `irdma` RDMA operation counters count
transactions, not bytes; there is no fixed Falcon bytes-per-operation conversion.
In-capsule writes can carry data without a separate RDMA READ operation.

| NVMe-oF operation | Target action | Target ACC counter |
|---|---|---|
| READ | RDMA WRITE into initiator memory | `bytes_from_ulp_rc` |
| WRITE | RDMA READ from initiator memory | `bytes_to_ulp` |

Target and initiator counters are two views of the same transfer; do not add
them. ACC payload bytes, successful application bytes, and physical NVMe bytes
have different measurement boundaries and need not be equal.

### Rate windows and benchmark results

Throughput summary tiles use `rate(...[30s])`, not phase-bracketed deltas.
At transitions they can include prior-phase traffic; new series require at least
two samples, and short phases need not reach a steady displayed value.
Prometheus scrapes every 4 s; the HTTP ACC exporter reads all four cards on
each request. The legacy gRPC shadow loops update every 2 s until retired.

Use CLI measured-phase JSON for application results and
[`measure_mmg_fleet.py`](../../../../../../scripts/ipu-poc/measure_mmg_fleet.py)
for common-window application, ACC, and NVMe snapshots. It divides each ACC
delta by that card's read-timestamp span. Capture during the sustained overlap,
require `valid=true`, and keep unrelated target I/O out of the snapshot window;
end-of-run counters alone do not establish four-host phase alignment.

## Topology and ports

Prometheus scrapes remote services through SSH forwards created by `up.sh`. On Linux, forwards bind to Docker's local bridge gateway to prevent exposure to the external management network.

### Node exporters and benchmark endpoints

| Host | Role | Node Exporter Port | Benchmark Tunnel (Local → Remote) |
|---|---|---:|---|
| `mmgt` | Storage Target | `19106` | — |
| `mmgi0` | Initiator 0 | `19107` | `19110 → 9101` |
| `mmgi1` | Initiator 1 (Monitor) | `19108` | `19120 → 9102` |
| `mmgi2` | Initiator 2 | `19109` | `19130 → 9103` |
| `mmgi3` | Initiator 3 | `19100` | `19140 → 9104` |

The target ACC HTTP exporter uses `19111 → mmgt:9188`, job `acc_grpc`.

### Target ACC addresses

| ACC Label | Target Netns | Fabric IP | Loopback Port |
|---|---|---|---:|
| `acc1` | `IPU2` | `200.0.6.3` | `15001` |
| `acc2` | `IPU1` | `200.0.5.3` | `15002` |
| `acc3` | `IPU3` | `200.0.7.3` | `15003` |
| `acc4` | `IPU4` | `200.0.8.3` | `15004` |

## Collector setup and maintenance

### Base host collectors

On each new host, install node_exporter and the base PCM, NUMA, NVMe SMART,
QP, and NIC collectors. Replace `HOST` and `FABRIC_IFACE` before running:

```bash
scp -r docs/design/v1/platform/ipu-poc/instrumentation/host HOST:/tmp/obs-kit
ssh HOST '
  RDMA_FABRIC_IFACE=FABRIC_IFACE SKIP_ACC_TELEMETRY=1 \
    bash /tmp/obs-kit/install.sh'
```

Use the fabric interface, never the SSH management interface. The installer
requires root, installs package dependencies, enables timers, and prints health
and freshness checks. `SKIP_ACC_TELEMETRY=1` omits the generic ACC collector;
the rig-specific ACC setup is separate. Initiator CPU panels require
[`install_acc_stats.sh`](../../../../../../scripts/ipu-poc/install_acc_stats.sh)
with that host's ACC target specification, not the default target-host mapping.

### Target collectors (`mmgt`)

After the base installation, add the target PCM PCIe and four-port NIC collectors.
Verify compatible PCM binaries first: the PCIe parser expects the CSV emitted by
`/usr/local/bin/pcm-pcie-20260811`. The supplied memory drop-in selects
`/usr/local/bin/pcm-memory-20260811`. Install those binaries or adapt their paths
to validated equivalents. Replace the four interface placeholders with the
target's current fabric interfaces; the shipped service's interface list can
be stale after a board replacement.

```bash
KIT=docs/design/v1/platform/ipu-poc/instrumentation
scp "$KIT/host/bin/"{pcm_pcie,mmgt_nic}_textfile.sh mmgt:/tmp/
scp "$KIT/host/systemd/"{pcm-pcie,mmgt-nic-stats}.{service,timer} mmgt:/tmp/
scp "$KIT/host/systemd/pcm-memory.service.d/pcm.conf" mmgt:/tmp/
ssh mmgt 'bash -s' <<'REMOTE'
set -euo pipefail
test -x /usr/local/bin/pcm-pcie-20260811
test -x /usr/local/bin/pcm-memory-20260811
install -m 0755 /tmp/{pcm_pcie,mmgt_nic}_textfile.sh /usr/local/bin/
install -m 0644 /tmp/{pcm-pcie,mmgt-nic-stats}.{service,timer} /etc/systemd/system/
install -d /etc/systemd/system/{pcm-memory,mmgt-nic-stats}.service.d
install -m 0644 /tmp/pcm.conf /etc/systemd/system/pcm-memory.service.d/
printf '[Service]\nEnvironment="MMGT_NIC_IFACES=FABRIC_IFACE_1 FABRIC_IFACE_2 FABRIC_IFACE_3 FABRIC_IFACE_4"\n' \
  > /etc/systemd/system/mmgt-nic-stats.service.d/ifaces.conf
systemctl daemon-reload
systemctl enable --now pcm-pcie.timer mmgt-nic-stats.timer
systemctl start pcm-memory.service pcm-pcie.service mmgt-nic-stats.service
REMOTE
```

Bootstrap ACC CPU collection and gRPC shadow loops from the repository root.
Export `IMC_PASSWORD` in the calling shell. The ACCs must already have working
IMC access and feature-pack protobuf modules at
`/opt/falcon/tools/controller/python_out`; `ACC_GRPC_PROTO_SOURCE` overrides that
path on the ACC. The installer copies the modules through the tunnel services
and creates the Python venv on `mmgt`.

```bash
ACC_GRPC_SHADOW=1 scripts/ipu-poc/install_acc_stats.sh mmgt \
  'IPU2:acc1:200.0.6.3,IPU1:acc2:200.0.5.3,IPU3:acc3:200.0.7.3,IPU4:acc4:200.0.8.3'
scp "$KIT/host/bin/"acc_{grpc_exporter,telemetry_textfile}.py mmgt:/tmp/
scp scripts/ipu-poc/acc-grpc-exporter.service mmgt:/tmp/
ssh mmgt 'bash -s' <<'REMOTE'
set -euo pipefail
install -m 0755 /tmp/acc_{grpc_exporter,telemetry_textfile}.py /usr/local/bin/
install -m 0644 /tmp/acc-grpc-exporter.service /etc/systemd/system/
install -m 0644 /opt/acc-grpc-telemetry/proto/acc1/telemetry_pb2*.py /opt/acc-grpc-telemetry/proto/
systemctl daemon-reload
systemctl enable --now acc-grpc-exporter.service
systemctl restart acc-grpc-exporter.service
curl --noproxy '*' -fsS http://127.0.0.1:9188/metrics
REMOTE
```

Run the [preflight checks](#preflight-health-checks) before proceeding. The
bootstrap re-enables `acc-transport.timer` and the gRPC shadow services;
it is not a post-cutover maintenance command.

### Exporter parity and retirement

During a sustained loaded window of at least ten minutes, compare the HTTP
exporter with the four shadow files on `mmgt`. Stage and run the gate there:

```bash
scp scripts/ipu-poc/check_acc_exporter_parity.py mmgt:/tmp/
ssh mmgt 'python3 /tmp/check_acc_exporter_parity.py \
  --textfile-dir /var/lib/node_exporter/textfile-shadow \
  --duration 600 --output /tmp/acc-exporter-parity.json'
scp mmgt:/tmp/acc-exporter-parity.json ./acc-exporter-parity.json
```

Require exit status 0 and `passed=true` in the JSON, and retain it with the run
artifacts. Then, outside an active benchmark, retire the legacy byte collectors:

```bash
ssh mmgt 'bash -s' <<'REMOTE'
set -euo pipefail
systemctl disable --now acc-transport.timer acc-grpc-telemetry@{acc1,acc2,acc3,acc4}.service
systemctl stop acc-transport.service
if test -f /var/lib/node_exporter/textfile/acc_transport.prom; then
  mv -n /var/lib/node_exporter/textfile/acc_transport.prom \
    /var/lib/node_exporter/textfile/acc_transport.prom.retired
  test ! -f /var/lib/node_exporter/textfile/acc_transport.prom
fi
REMOTE
```

Keep `acc-grpc-tunnel@*` and `acc-stats.timer` running: CPU telemetry still uses
the SSH collector. If the move fails because a retirement backup already exists,
preserve that backup separately before retrying. Recheck preflight after
retirement. If the HTTP path fails, roll back collector retirement with:

```bash
ssh mmgt 'bash -s' <<'REMOTE'
set -euo pipefail
systemctl enable --now acc-transport.timer acc-grpc-telemetry@{acc1,acc2,acc3,acc4}.service
systemctl start acc-transport.service
REMOTE
```

This restores legacy collection, not the dashboard's `acc_grpc` scrape path.
Repair that exporter or restore a known-good matching dashboard/config backup
before trusting panels; preserved shadow files alone do not restore monitoring.

### Utilities

- [`check_mmg_long_range.py`](../../../../../../scripts/ipu-poc/check_mmg_long_range.py)
  checks RDMA/NVMe metric continuity against local Prometheus on `mmgi1`.
- [`annotate_mmg_phase.py`](../../../../../../scripts/ipu-poc/annotate_mmg_phase.py)
  runs on `mmgi1` and annotates the measured overlap in Grafana from fleet result
  JSON, using the local Docker listener and admin credential in the kit's `.env`.
- [`probe_acc_collector_overhead.py`](../../../../../../scripts/ipu-poc/probe_acc_collector_overhead.py)
  runs locally as root on an idle `mmgt`, with `acc_ssh_stats.py` alongside it and
  `IMC_PASSWORD` exported. It stops/restarts the exporter and ACC CPU collector
  during off/on/off windows, interrupting telemetry. Never run it during a
  benchmark; it restores those services on exit from the sampling windows.

## Troubleshooting

| Symptom | Possible cause | Check or action |
|---|---|---|
| Fabric link is `LINK_UP` but I/O fails | Link state does not establish controller or RDMA health. | Check `nvme list-subsys` and driver/kernel connection errors. Zero visible MRs is not proof of registration failure; require an actual registration error before diagnosing it. |
| Local `curl /metrics` returns 403 | Localhost traffic intercepted by environment proxy. | Use `curl --noproxy '*' localhost:<port>/metrics`. |
| InfiniBand exporter shows no data | `irdma` exports `hw_counters/` instead of `ports/<p>/counters/`. | Use `rdma_hwcounters_textfile.sh` instead of node_exporter's built-in infiniband collector. |
| Grafana panels break after reload | Datasource UID changed. | Ensure datasource UID remains `PROM` in `provisioning/datasources/prometheus.yml`. |
| Targets wrong after config edit | Incorrect targets, failed reload, or a stale bind mount. | Check `prometheus.yml`, then re-run `up.sh` before the next sweep. It reloads config and recreates Prometheus if mounted content differs. |
| Textfile metrics missing or frozen | Missing/failed timers, stale files, or permissions/SELinux errors. | Check per-file ages, `systemctl list-timers`, and the collector journal. Check SELinux denials before changing contexts. |
