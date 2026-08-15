# Profile-Shaped L2 Benchmarks

Use `lmcache bench l2 --kvcache-shape-profile` when the benchmark should
submit one uniform KV-cache page for every layer in a model retrieval burst.
The profile controls only `objects_per_submit` and `page_size_bytes`; it does
not allocate model tensors or reproduce an inference server.

`run_model_geometry.sh` is the small single-process helper. It resolves and
prints the profile before invoking `lmcache bench l2`. `run_geom_multi.sh`
starts several local processes with a shared read corpus; it is not a
synchronized or physical multi-initiator benchmark.

## Prerequisites

- Run from a checkout that contains the selected profile.
- Set `PYTHON` to an interpreter with this checkout's LMCache installed.
- Use a dedicated `BASE_PATH`. Store runs create objects below it.
- Use a fresh `PREFIX` for every independent store run. Load uses the same
  prefix as the store that created the corpus.
- The default adapter is `fs_native` with `use_odirect: true`; override it
  with `L2_ADAPTER` when benchmarking another adapter.

Inspect the available profiles:

```bash
PYTHON=.venv/bin/python bash scripts/ipu-poc/run_model_geometry.sh profiles
```

## Profiles

| Profile | Model | Objects/submit | Page | Submit payload |
|---|---|---:|---:|---:|
| `deepseek_v3_fp8.yaml` | DeepSeek-V3 FP8 | 61 | 144 KiB | 8.58 MiB |
| `mixtral_8x22b_fp8.yaml` | Mixtral 8x22B FP8 | 56 | 256 KiB | 14 MiB |
| `llama3_70b_fp8.yaml` | Llama-3.1 70B FP8 | 80 | 512 KiB | 40 MiB |
| `llama3_405b_fp8.yaml` | Llama-3.1 405B FP8 | 126 | 256 KiB | 31.5 MiB |

Confirm a profile's resolved geometry before any run:

```bash
PYTHON=.venv/bin/python \
  bash scripts/ipu-poc/run_model_geometry.sh show \
  scripts/ipu-poc/models/deepseek_v3_fp8.yaml
```

## Single-Process Workflow

Build a corpus with `store`, then use the same profile, prefix, rounds, and
warmup rounds for `load`. The examples use one round only to make the command
shape clear; increase `ROUNDS`, `IN_FLIGHT`, and `NUM_WORKERS` deliberately for
the system under test.

```bash
BASE_PATH=/mnt/lmcache-kvcache
PYTHON=.venv/bin/python

PREFIX=deepseek-$(date +%s) \
BASE_PATH=$BASE_PATH PYTHON=$PYTHON \
  bash scripts/ipu-poc/run_model_geometry.sh store \
  scripts/ipu-poc/models/deepseek_v3_fp8.yaml
```

For a load, set `PREFIX` to the corpus prefix printed or recorded during the
store run:

```bash
PREFIX=deepseek-1723651200 \
BASE_PATH=/mnt/lmcache-kvcache PYTHON=.venv/bin/python \
  bash scripts/ipu-poc/run_model_geometry.sh sustained-load \
  scripts/ipu-poc/models/deepseek_v3_fp8.yaml
```

Use the same commands with each shipped profile:

```bash
PREFIX=mixtral-$(date +%s) BASE_PATH=/mnt/lmcache-kvcache PYTHON=.venv/bin/python \
  bash scripts/ipu-poc/run_model_geometry.sh store \
  scripts/ipu-poc/models/mixtral_8x22b_fp8.yaml

PREFIX=llama70b-$(date +%s) BASE_PATH=/mnt/lmcache-kvcache PYTHON=.venv/bin/python \
  bash scripts/ipu-poc/run_model_geometry.sh store \
  scripts/ipu-poc/models/llama3_70b_fp8.yaml

PREFIX=llama405b-$(date +%s) BASE_PATH=/mnt/lmcache-kvcache PYTHON=.venv/bin/python \
  bash scripts/ipu-poc/run_model_geometry.sh store \
  scripts/ipu-poc/models/llama3_405b_fp8.yaml
```

## Sustained Mixed Workload

Mixed mode reads an existing corpus under `PREFIX` and writes only to the
fresh, distinct `WRITE_PREFIX`. It requires a duration and records a requested
byte ratio. It does not make a store/load integrity claim by itself.

```bash
PREFIX=mixtral-read-corpus \
WRITE_PREFIX=mixtral-write-$(date +%s) \
BASE_PATH=/mnt/lmcache-kvcache PYTHON=.venv/bin/python \
DURATION_SEC=60 READ_WRITE_RATIO=5:1 IN_FLIGHT=8 NUM_WORKERS=16 \
  bash scripts/ipu-poc/run_model_geometry.sh mixed \
  scripts/ipu-poc/models/mixtral_8x22b_fp8.yaml
```

## Local Multi-Process Fan-Out

This starts local sustained-load processes that share the same read corpus. It
splits `WORKERS_TOTAL` evenly across them and writes one JSON result and log per
process. It does not synchronize the launch or establish separate machines.

```bash
PREFIX=llama70b-read-corpus \
BASE_PATH=/mnt/lmcache-kvcache PYTHON=.venv/bin/python \
INITIATORS=2 WORKERS_TOTAL=16 IN_FLIGHT=8 DURATION_SEC=60 \
  bash scripts/ipu-poc/run_geom_multi.sh load \
  scripts/ipu-poc/models/llama3_70b_fp8.yaml
```

For a real benchmark result, retain the command line, resolved profile SHA,
result JSON, host counters, and target storage telemetry. The helpers do not
make acceptance decisions or replace workload-specific validation.
