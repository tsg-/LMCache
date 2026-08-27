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
- Use a dedicated `BASE_PATH`. Store runs create objects below it. It is not
  required when `L2_ADAPTER` supplies a complete adapter JSON.
- Use a fresh `PREFIX` for every independent store run. Load uses the same
  prefix as the store that created the corpus.
- Object keys are namespaced by `PREFIX` and the profile's SHA-256, printed as
  `key namespace` on every run. Reading a corpus back therefore requires the
  same prefix *and* the same profile file; a mismatch misses rather than
  reporting another model's bytes as a hit. Keep passing the plain `PREFIX` to
  the helpers and to `geom_readback.py`, which derives the same namespace from
  its `--profile`. Readback therefore covers corpora these helpers created; a
  corpus written by calling `bench l2` directly carries no profile scope and is
  not verifiable here.
- The default adapter is `fs_native` with `use_odirect: true`; override it
  with `L2_ADAPTER` when benchmarking another adapter. Worker counts
  (`NUM_WORKERS`, `WORKERS_TOTAL`, `WORKERS_PER`) only reach the default JSON;
  set workers inside the adapter JSON when supplying your own.

Inspect the available profiles:

```bash
PYTHON=.venv/bin/python bash scripts/ipu-poc/run_model_geometry.sh profiles
```

## Verify the Setup

On a host you have not benchmarked before, confirm corpus identity holds on the
target filesystem before recording any number:

```bash
PYTHON=.venv/bin/python BASE_PATH=/mnt/lmcache-kvcache \
  bash scripts/ipu-poc/verify_geometry_corpus.sh
```

It runs `tests/scripts/test_model_geometry_scripts.py`, stores a Llama-405B
corpus, then shows that the storing profile reads it back while a mismatched
profile gets zero hits and a readback failure. It ends in `== PASS ==` or exits
nonzero. Omit `BASE_PATH` to use a temporary directory instead of the storage
under test.

The mismatch uses Mixtral deliberately: it shares Llama-405B's 256 KiB page and
its key range is a subset, so an unscoped prefix reports 56 of 56 hits over
another model's bytes. A pair with differing page sizes misses on length alone
and would pass even if the scoping regressed.

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

Build a corpus with `store`, then read it back with the same profile and the
same prefix. Size the store for the read it has to serve, covered in the next
section, and raise `IN_FLIGHT` and `NUM_WORKERS` deliberately for the system
under test.

```bash
export BASE_PATH=/mnt/lmcache-kvcache
export PYTHON=.venv/bin/python
export PREFIX=deepseek-$(date +%s)

ROUNDS=2 bash scripts/ipu-poc/run_model_geometry.sh store \
  scripts/ipu-poc/models/deepseek_v3_fp8.yaml
```

For the read, set `PREFIX` to the same value the store run used. Every run
prints both `prefix` and the derived `key namespace`; reuse the former. Passing
the namespace as `PREFIX` appends the SHA twice and misses the corpus:

```bash
PREFIX=deepseek-1723651200 \
BASE_PATH=/mnt/lmcache-kvcache PYTHON=.venv/bin/python \
DURATION_SEC=60 \
  bash scripts/ipu-poc/run_model_geometry.sh sustained-load \
  scripts/ipu-poc/models/deepseek_v3_fp8.yaml
```

## Sizing a Store for a Sustained Load

A sustained load wraps around the key space `store` created, which is
`(ROUNDS + WARMUP_ROUNDS) * IN_FLIGHT * objects_per_submit` keys.
`sustained-load` does not take `ROUNDS`, so it uses the CLI defaults of one
measured and one warmup round and reads `2 * IN_FLIGHT * objects_per_submit`
keys. Store with `ROUNDS=2` and the same `IN_FLIGHT` the load will use.

An undersized corpus does not fail. The load misses the keys that were never
written and `Throughput aggregate (MB/s)` still charges those bytes, so the
headline number rises as the hit rate falls. Read `Total success` against
`Total keys` on every load:

| Store | Sustained load | Total keys | Total success |
|---|---|---:|---:|
| `ROUNDS=1 IN_FLIGHT=1` | `IN_FLIGHT=4` | 25132 | 3172 |
| `ROUNDS=2 IN_FLIGHT=4` | `IN_FLIGHT=4` | 172691 | 172691 |

In JSON output, `throughput_success_mbps` is written only when the two counts
diverge. Its presence means the run missed keys and
`throughput_aggregate_mbps` overstates the result.

## Full Model Sweep

One `PREFIX` covers every profile: the namespace is scoped by profile SHA, so
four models under one prefix occupy four distinct key spaces and cannot read
each other's corpora. Store each model, then read it back over a fixed window.

```bash
export BASE_PATH=/mnt/lmcache-kvcache
export PYTHON=.venv/bin/python
export PREFIX=sweep-$(date +%s)
export IN_FLIGHT=8 NUM_WORKERS=16
mkdir -p results

for profile in scripts/ipu-poc/models/*.yaml; do
  model=$(basename "$profile" .yaml)
  echo "===== $model ====="
  ROUNDS=2 bash scripts/ipu-poc/run_model_geometry.sh store "$profile"
  OUTPUT=results/$PREFIX-$model.json DURATION_SEC=60 \
    bash scripts/ipu-poc/run_model_geometry.sh sustained-load "$profile"
done
```

At `ROUNDS=2 IN_FLIGHT=8` the four corpora need about 1.5 GiB in total, and
that scales linearly with both. Per model:

| Profile | Keys stored | Corpus |
|---|---:|---:|
| `deepseek_v3_fp8.yaml` | 976 | 137 MiB |
| `mixtral_8x22b_fp8.yaml` | 896 | 224 MiB |
| `llama3_70b_fp8.yaml` | 1280 | 640 MiB |
| `llama3_405b_fp8.yaml` | 2016 | 504 MiB |

Size the corpus past host DRAM or drop caches between models, or the load
measures the page cache. Check the hit rate before reading any rate:

```bash
"$PYTHON" - results/$PREFIX-*.json <<'EOF'
import json, sys

for path in sys.argv[1:]:
    m = json.load(open(path))["metrics"]
    op = m["op_0"]
    print(f"{m['geometry']['model_name']:42s} "
          f"{op['total_success']}/{op['total_keys']} keys  "
          f"{op['throughput_aggregate_mbps']:.0f} MB/s")
EOF
```

### One Model at a Time

To run a single model, or to re-run one after a change, keep the sweep's
`PREFIX` and name the profile. Each pair is a store followed by a read of the
same corpus:

```bash
export BASE_PATH=/mnt/lmcache-kvcache PYTHON=.venv/bin/python
export PREFIX=sweep-1723651200 IN_FLIGHT=8 NUM_WORKERS=16
R=scripts/ipu-poc/run_model_geometry.sh
M=scripts/ipu-poc/models

# DeepSeek-V3 FP8 — 61 x 144 KiB
ROUNDS=2 bash $R store $M/deepseek_v3_fp8.yaml
DURATION_SEC=60 bash $R sustained-load $M/deepseek_v3_fp8.yaml

# Mixtral 8x22B FP8 — 56 x 256 KiB
ROUNDS=2 bash $R store $M/mixtral_8x22b_fp8.yaml
DURATION_SEC=60 bash $R sustained-load $M/mixtral_8x22b_fp8.yaml

# Llama-3.1 70B FP8 — 80 x 512 KiB
ROUNDS=2 bash $R store $M/llama3_70b_fp8.yaml
DURATION_SEC=60 bash $R sustained-load $M/llama3_70b_fp8.yaml

# Llama-3.1 405B FP8 — 126 x 256 KiB
ROUNDS=2 bash $R store $M/llama3_405b_fp8.yaml
DURATION_SEC=60 bash $R sustained-load $M/llama3_405b_fp8.yaml
```

Re-running one model's `store` under a prefix that already holds its corpus
measures `fs_native`'s existence check rather than the write path, because a
store whose key exists reports success without writing. Use a fresh `PREFIX`
for a new store measurement; reuse it only to read.

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
With a supplied `L2_ADAPTER` it refuses `WORKERS_TOTAL` and `WORKERS_PER`
rather than report a split it cannot apply.

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
