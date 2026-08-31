# bench l2 — model KV geometry + telemetry handoff

Branch **`feat/bench-l2-geometry-handoff`** (base: **`dev`**) is split into six commits:

| Commit | Contents |
|--------|----------|
| 1 | `bench l2` KV shape spec + sustained / mixed load (`--kvcache-shape-profile`, geometry profiles) |
| 2 | `--serve-metrics` Prometheus endpoint (`metrics.py` + tests) |
| 3 | Host + Grafana **instrumentation kit** |
| 4 | **Automation**: mkp drivers, fio capacity sweep, NVMe-oF setup/quiesce |
| 5 | Architecture A Falcon **store/retrieve** diagrams (`.puml` + `.svg`) |
| 6 | This README + `install_bench_l2_handoff.sh` |

Further detail: **`README-model-geometry.md`** (geometry sweeps), **`README-fio-capacity-sweep.md`** (fio matrix), **`docs/design/v1/platform/ipu-poc/instrumentation/README.md`** (observability).

## One-shot install

From the repo root on a benchmark initiator or dev machine:

```bash
bash scripts/ipu-poc/install_bench_l2_handoff.sh
source .venv-bench-l2/bin/activate   # or your LMCACHE_VENV
```

The script creates a venv, installs `pyyaml` / `grpcio`, installs torch if the venv has none, runs `pip install -e . --no-build-isolation`, and smoke-checks that `lmcache bench l2` exposes geometry and metrics flags.

Note it does **not** skip the native extensions when torch is absent — `fs_native` is backed by `lmcache_fs`, so they have to build, which is why torch is installed first rather than worked around.

**Python 3.12+** required. Install **torch** first on GPU paths, since the extensions compile against whichever torch is found; left to itself pip pulls a multi-GB CUDA wheel even on a GPU-less host. Host observability and fio are separate prerequisites (below).

On a host that has never run this, two things block the install before it starts:

- **Toolchain.** The extensions need a C++ compiler and the Python headers: `dnf install -y python3.12 python3.12-devel gcc gcc-c++ cmake make git`. Omitting `-devel` fails late, with a missing-header error.
- **Proxy.** A proxy set in `/etc/dnf/dnf.conf` does not apply to pip. Export `http_proxy` / `https_proxy` into the installing shell, or pip retries pypi.org and gives up while dnf appears fine.

## KV geometry CLI

Grammar matches `lmcache bench server --kvcache-shape-spec` (`lmcache.v1.kv_layer_groups`).

**Profile (recommended)** — YAML under `scripts/ipu-poc/models/`; sets page size, burst depth, and derived CLI sizes:

```bash
lmcache bench l2 \
  --l2-adapter '{"type":"fs_native","base_path":"/mnt/kvcache","use_odirect":true}' \
  --kvcache-shape-profile scripts/ipu-poc/models/deepseek_v3_fp8.yaml \
  --key-prefix smoke001 --only store --num-keys 1 --in-flight 4 \
  --l1-align-bytes 4096 --warmup-rounds 0 --rounds 1 --no-skip-verify
```

**Inline spec:**

```bash
lmcache bench l2 ... --kvcache-shape-spec '(1,1024,256,1,576):uint8:61'
```

**Sustained / namespace drivers** use `--key-prefix`, `--duration-sec`, `--warmup-rounds`, and optionally `--read-write-ratio` (mixed). See `README-model-geometry.md` and `run_payload_sweep.sh`.

## Live metrics (`serve-metrics`)

```bash
lmcache bench l2 ... --only load \
  --serve-metrics 9101 --metrics-bind-address 127.0.0.1
```

Tunnel to Prometheus with `BENCH_TUNNELS=1` in `instrumentation/up.sh`. Multi-initiator scripts use `METRICS_BASE_PORT + id` (default **9101**).

## Automation scripts

All under `scripts/ipu-poc/` unless noted. Edit **site-specific** `VENV`, corpus path `B=`, and adapter JSON in each driver header.

| Script | Purpose |
|--------|---------|
| `run_model_geometry.sh` | Generic profile-shaped store, load, sustained-load, and mixed runs |
| `run_geom_multi.sh` | Generic local multi-process sustained-load or mixed fan-out |
| `run_payload_sweep.sh` | Large-object (28 MiB) proxy sweep |
| `run_sustained_load.sh` | Fixed-corpus sustained read |
| `run_multi_initiator_load.sh` | Parallel read processes + metrics |
| `run_multi_initiator_mixed.sh` | 5:1 mixed window |
| `geom_*.py`, `*_report.py`, `interior_rate.py` | Parse, calibrate, post-process |
| `run_fio_capacity_sweep.sh` | Block-path capacity sweep (does not touch `kvcache/` corpus) |
| `setup_nvmeof_target.sh`, `quiesce_nvmeof_target.sh` | mkp NVMe-oF wrappers |
| `scripts/nvmeof_target_provision.sh` | Target export (repo root) |
| `scripts/nvmeof_initiator_attach.sh` | Initiator connect (repo root) |

**Run order:** NVMe-oF provision → attach → filesystem → `bench l2` / geometry drivers. Quiesce before queue-depth or reconnect experiments.

## Architecture diagrams

- `docs/design/v1/platform/ipu-poc/diagrams/architecture-a-nvmeof-falcon-store-flow.{puml,svg}`
- `docs/design/v1/platform/ipu-poc/diagrams/architecture-a-nvmeof-falcon-retrieve-flow.{puml,svg}`

## Telemetry kit

`docs/design/v1/platform/ipu-poc/instrumentation/`

```bash
# MMG-400 ACC telemetry (from scripts/ipu-poc/):
IMC_PASSWORD=<imc-root-password> \
  ./install_acc_stats.sh mmgi0 ':acc1:200.0.4.3'
IMC_PASSWORD=<imc-root-password> \
  ./install_acc_stats.sh mmgi1 ':acc1:200.0.3.3'
IMC_PASSWORD=<imc-root-password> ./install_acc_stats.sh mmgt
IMC_PASSWORD=<imc-root-password> ACC_GRPC_SHADOW=1 \
  ./install_acc_stats.sh mmgt  # optional target shadow; no dashboard cutover

# On control laptop:
HOSTS="mmgt:19106 mmgi0:19107 mmgi1:19108" \
  MMG_BENCH_TUNNELS=1 MMG_BENCH_INITIATORS=1 ./up.sh
```

The installer enables a 30 s ACC core-busy timer and a separate 10 s Falcon
transport-counter timer. On `mmgt`, `ACC_GRPC_SHADOW=1` adds a persistent
IMC-to-ACC gRPC shadow collector at 5 s without changing the dashboard. The
RDMA panels use a 60 s counter-rate window; use the exact measured interval for
completed-run counter comparisons.

The MMG target's PCIe, NIC, and NUMA collectors are target-specific; use the
instrumentation README rather than the generic `host/install.sh` on `mmgt`.

## Tests

Install test deps: `pip install -r requirements/test.txt` (the handoff install script does this when the file exists).

Use the standard CI pytest ignores for a quick local pass:

```bash
export PYTEST_IGNORE="--ignore=tests/disagg --ignore=tests/v1/multiprocess/ \
  --ignore=tests/v1/distributed/ --ignore=tests/skipped \
  --ignore=tests/v1/storage_backend/test_eic.py"
```

### bench l2 CLI (KV geometry + sustained + metrics)

| Test file | What it covers |
|-----------|----------------|
| `tests/cli/commands/bench/l2_adapter_bench/test_command.py` | Argument parsing, `--kvcache-shape-profile` / inline spec, sustained and mixed flags |
| `tests/cli/commands/bench/l2_adapter_bench/test_runner.py` | Submit/wait rounds, sustained windows, operation routing |
| `tests/cli/commands/bench/l2_adapter_bench/test_result.py` | Result aggregation and structured output |
| `tests/cli/commands/bench/l2_adapter_bench/test_metrics.py` | `--serve-metrics` server, phase registration, bind address |

```bash
pytest -xvs $PYTEST_IGNORE tests/cli/commands/bench/l2_adapter_bench/
```

### Geometry automation (scripts)

| Test file | What it covers |
|-----------|----------------|
| `tests/scripts/test_geom_multi_report.py` | Multi-initiator geometry report parsing |
| `tests/scripts/test_model_geometry_scripts.py` | Profile resolution, SHA-scoped key namespaces, store/load/readback round trip, fan-out worker budgets |

```bash
pytest -xvs $PYTEST_IGNORE tests/scripts/test_geom_multi_report.py \
  tests/scripts/test_model_geometry_scripts.py
```

Before trusting a geometry number from a new host, run the corpus-identity check
on the storage under test. It runs the tests above, then stores a corpus and
shows that only the storing profile reads it back:

```bash
BASE_PATH=/mnt/lmcache-kvcache bash scripts/ipu-poc/verify_geometry_corpus.sh
```

It ends in `== PASS ==` or exits nonzero. Object keys carry no page size and the
adapter reports a hit once the requested buffer is full, so without the
profile-scoped namespace a mismatched profile reads another model's corpus and
reports a plausible bandwidth number.

### Instrumentation

| Test file | What it covers |
|-----------|----------------|
| `tests/scripts/test_acc_telemetry_textfile.py` | ACC Falcon gRPC → Prometheus textfile collector |

```bash
pytest -xvs $PYTEST_IGNORE tests/scripts/test_acc_telemetry_textfile.py
```

### NVMe-oF helpers

| Test file | What it covers |
|-----------|----------------|
| `tests/scripts/test_nvmeof_util.py` | Shared Python utilities |
| `tests/scripts/test_nvmeof_initiator_attach.py` | Initiator attach script (unit) |
| `tests/scripts/test_nvmeof_target_provision.py` | Target provision script (unit) |

```bash
pytest -xvs $PYTEST_IGNORE \
  tests/scripts/test_nvmeof_util.py \
  tests/scripts/test_nvmeof_initiator_attach.py \
  tests/scripts/test_nvmeof_target_provision.py
```

### fio capacity sweep

| Test file | What it covers |
|-----------|----------------|
| `tests/scripts/test_run_fio_capacity_sweep.py` | Sweep driver guards and corpus isolation |
| `tests/scripts/test_fio_sweep_charts.py` | Chart/report helpers |
| `tests/scripts/test_multi_initiator_mixed_report.py` | Mixed-load report parser |

```bash
pytest -xvs $PYTEST_IGNORE \
  tests/scripts/test_run_fio_capacity_sweep.py \
  tests/scripts/test_fio_sweep_charts.py \
  tests/scripts/test_multi_initiator_mixed_report.py
```

### Full handoff test bundle (one command)

```bash
pytest -xvs $PYTEST_IGNORE \
  tests/cli/commands/bench/l2_adapter_bench/ \
  tests/scripts/test_geom_multi_report.py \
  tests/scripts/test_model_geometry_scripts.py \
  tests/scripts/test_acc_telemetry_textfile.py \
  tests/scripts/test_nvmeof_util.py \
  tests/scripts/test_nvmeof_initiator_attach.py \
  tests/scripts/test_nvmeof_target_provision.py \
  tests/scripts/test_run_fio_capacity_sweep.py \
  tests/scripts/test_fio_sweep_charts.py \
  tests/scripts/test_multi_initiator_mixed_report.py
```

User-facing CLI reference: `docs/source/cli/bench.rst` (geometry and sustained sections).
