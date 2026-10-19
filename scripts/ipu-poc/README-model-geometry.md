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

It runs `tests/scripts/test_model_geometry_scripts.py`, stores a Mixtral 8x22B
corpus, then shows that the storing profile reads it back while a mismatched
profile gets zero hits and a readback failure. It ends in `== PASS ==` or exits
nonzero. Omit `BASE_PATH` to use a temporary directory instead of the storage
under test.

The mismatched profile is `models/fixtures/mixtral_8x22b_pagetest_256k.yaml`, a
byte-different copy of the storing profile. Both resolve to 56 objects at a
256 KiB page and cover the same key range, so an unscoped prefix reports 56 of
56 hits over the first corpus's bytes and only the SHA scoping can produce the
zero. A pair differing in page size or burst depth misses on length alone and
would pass even if the scoping regressed. The fixture sits outside `models/` so
it is never offered as a model to benchmark.

## Profiles

Profiles come in two forms. A **page burst** describes one uniform page
repeated once per layer. An **object group** profile describes what a
production submit actually carries: one object per `(chunk, object group, kv
rank)`, each packing one or more components whose sizes need not agree.
`show` prints which form a file uses on its `geometry:` line.

| Profile | Model | Form | Objects/submit | Object | Submit payload |
|---|---|---|---:|---:|---:|
| `mixtral_8x22b_fp8_64k.yaml` | Mixtral 8x22B FP8, 32-token chunk | page burst | 56 | 64 KiB | 3.5 MiB |
| `mixtral_8x22b_fp8_128k.yaml` | Mixtral 8x22B FP8, 64-token chunk | page burst | 56 | 128 KiB | 7 MiB |
| `deepseek_v3_fp8.yaml` | DeepSeek-V3 FP8 | page burst | 61 | 144 KiB | 8.58 MiB |
| `mixtral_8x22b_fp8.yaml` | Mixtral 8x22B FP8 | page burst | 56 | 256 KiB | 14 MiB |
| `mixtral_8x22b_fp8_512k.yaml` | Mixtral 8x22B FP8, 256-token chunk | page burst | 56 | 512 KiB | 28 MiB |
| `deepseek_v3_fp8_packed.yaml` | DeepSeek-V3 FP8 | object group | 1 | 8.58 MiB | 8.58 MiB |
| `mixtral_8x22b_fp8_tp8_packed.yaml` | Mixtral 8x22B FP8, TP=8 | object group | 8 | 3.5 MiB | 28 MiB |
| `minimax_m3_bf16_tp8.yaml` | MiniMax-M3 bf16, TP=8 | object group | 8 | 9.34 MiB | 74.7 MiB |
| `minimax_m3_bf16_tp8_odirect.yaml` | MiniMax-M3 bf16, TP=8, padded | object group | 8 | 9.34 MiB | 74.7 MiB |

The four Mixtral profiles change only chunk size (32/64/128/256 tokens), so page
size is the single variable: 64/128/256/512 KiB. DeepSeek's MLA cache is
576 B/token, which has no integer 64 KiB page. Profiles occupy separate key
namespaces, so one prefix holds all corpora.

The packed DeepSeek and Mixtral profiles are object-rate experiments, not part
of the default five-profile page-burst sweep below.

MiniMax-M3 is the reason the object-group form exists. Its 60-layer main K/V
and its 57-layer key-only DSA indexer are both full attention, so LMCache
packs them into ONE object per `(chunk, kv rank)` -- a shape no uniform page
size can express. Two consequences for an operator:

- The indexer dtype is **unconfirmed**. The profile sizes it at the repo's fp8
  DSA layout (132 B/token). If M3 registers it as bf16 the packed object is
  11,599,872 B instead. Confirm against a captured vLLM registration dump
  before quoting an M3 number externally. The profile header carries the
  citations.
- Under that fp8 assumption the packed object is 9,790,464 B, which is **not**
  4096-aligned, because the 132 B/token indexer breaks the alignment the main
  K/V would have had alone. `bench l2` validates alignment per object, so an
  O_DIRECT run is rejected; use a buffered adapter and `L1_ALIGN_BYTES=1`. The
  rejection follows from the dtype above rather than from M3 itself — the bf16
  alternative is 4096-aligned (11,599,872 = 4096 x 2832), so confirming bf16
  would put O_DIRECT back on the table.

  For an O_DIRECT number now, without waiting on that confirmation, use
  `minimax_m3_bf16_tp8_odirect.yaml`. It adds a 3,072 B synthetic padding
  component (~0.031% overhead) that rounds the packed object up to
  9,793,536 B = 4096 x 2391. A smaller `--l1-align-bytes` does not work
  around this: 9,790,464 is a multiple of 512, so the harness's own check
  would accept it, but `local_disk_backend.py` checks against the
  filesystem's block size (`stat.f_bsize`, 4096 on XFS regardless of the
  device's logical sector size) and still rejects it. Padding is the only
  fix that clears both checks.

The byte-accounting consumers -- `geom_readback.py`, `geom_calib.py`,
`geom_report.py`, and `geom_multi_report.py` -- derive application bytes from a
single page size, so they refuse an object-group run rather than picking one of
its object sizes. Byte-verify that geometry with a combined store+load run and
`--no-skip-verify` instead.

`models/legacy/` holds Llama-3.1 70B and 405B profiles, kept for reference and
excluded from the sweep. `models/fixtures/` holds the corpus-identity gate
fixture. Neither directory is picked up by `profiles` or by a `models/*.yaml`
glob.

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
the profiles under one prefix occupy distinct key spaces and cannot read each
other's corpora. Store each, then read it back over a fixed window.

For the two-initiator inventory coordinator, a reproducible concurrency matrix
is configured entirely through the environment. This example runs each of the
five O_DIRECT profiles plus MiniMax-M3 at 8, 16, and 24 submits in flight,
after a 60-second warmup and for a 120-second measurement window:

```bash
export RUN_ID=geometry-$(date +%Y%m%d-%H%M%S)
IN_FLIGHTS='8 16 24' WARMUP_SEC=60 DURATION_SEC=120 INCLUDE_MINIMAX=1 \
  bash scripts/ipu-poc/run_geometry_inventory.sh sweep \
  scripts/ipu-poc/inventories/mmg-two-initiator.env
```

The output name ends in `-if<in_flight>.json`. The coordinator uses a distinct
key prefix for every in-flight level, so a later store cannot collide with the
same model's earlier level. MiniMax-M3 selects
`minimax_m3_bf16_tp8_odirect.yaml`, which adds the documented 3,072 B
alignment pad and uses the same O_DIRECT `fs_native` adapter as the other
rows. Its fp8 DSA-indexer geometry assumption remains a comparison caveat.

The loop below uses the default O_DIRECT `fs_native` adapter. Include the
padded MiniMax profile when you want its O_DIRECT result; the unpadded
`minimax_m3_bf16_tp8.yaml` remains available only for inspecting the exact
unrounded geometry.

```bash
export BASE_PATH=/mnt/lmcache-kvcache
export PYTHON=.venv/bin/python
export PREFIX=sweep-$(date +%s)
export IN_FLIGHT=8 NUM_WORKERS=16
mkdir -p results

for profile in \
  scripts/ipu-poc/models/mixtral_8x22b_fp8_64k.yaml \
  scripts/ipu-poc/models/mixtral_8x22b_fp8_128k.yaml \
  scripts/ipu-poc/models/deepseek_v3_fp8.yaml \
  scripts/ipu-poc/models/mixtral_8x22b_fp8.yaml \
  scripts/ipu-poc/models/mixtral_8x22b_fp8_512k.yaml \
  scripts/ipu-poc/models/minimax_m3_bf16_tp8_odirect.yaml
do
  model=$(basename "$profile" .yaml)
  echo "===== $model ====="
  ROUNDS=2 bash scripts/ipu-poc/run_model_geometry.sh store "$profile"
  OUTPUT=results/$PREFIX-$model.json DURATION_SEC=60 \
    bash scripts/ipu-poc/run_model_geometry.sh sustained-load "$profile"
done
```

At `ROUNDS=2 IN_FLIGHT=8` the five swept corpora need about 977 MiB in total,
and that scales linearly with both. Per profile:

| Profile | Keys stored | Corpus |
|---|---:|---:|
| `mixtral_8x22b_fp8_64k.yaml` | 896 | 56 MiB |
| `mixtral_8x22b_fp8_128k.yaml` | 896 | 112 MiB |
| `deepseek_v3_fp8.yaml` | 976 | 137 MiB |
| `mixtral_8x22b_fp8.yaml` | 896 | 224 MiB |
| `mixtral_8x22b_fp8_512k.yaml` | 896 | 448 MiB |
| `minimax_m3_bf16_tp8_odirect.yaml` | 128 | 1195 MiB |

Size the corpus past host DRAM or drop caches between models, or the load
measures the page cache. Check the hit rate before reading any rate:

```bash
"$PYTHON" - results/$PREFIX-*.json <<'EOF'
import json, sys

for path in sys.argv[1:]:
    m = json.load(open(path))["metrics"]
    op = m["op_0"]
    print(f"{m['geometry']['model_name']:52s} "
          f"{op['total_success']}/{op['total_keys']} keys  "
          f"{op['throughput_aggregate_mbps']:.0f} MB/s")
EOF
```

### One Model at a Time

To run a single profile, or to re-run one after a change, keep the sweep's
`PREFIX` and name the profile. Each pair is a store followed by a read of the
same corpus:

```bash
export BASE_PATH=/mnt/lmcache-kvcache PYTHON=.venv/bin/python
export PREFIX=sweep-1723651200 IN_FLIGHT=8 NUM_WORKERS=16
R=scripts/ipu-poc/run_model_geometry.sh
M=scripts/ipu-poc/models

# Mixtral 8x22B FP8, 32-token chunk — 56 x 64 KiB
ROUNDS=2 bash $R store $M/mixtral_8x22b_fp8_64k.yaml
DURATION_SEC=60 bash $R sustained-load $M/mixtral_8x22b_fp8_64k.yaml

# Mixtral 8x22B FP8, 64-token chunk — 56 x 128 KiB
ROUNDS=2 bash $R store $M/mixtral_8x22b_fp8_128k.yaml
DURATION_SEC=60 bash $R sustained-load $M/mixtral_8x22b_fp8_128k.yaml

# DeepSeek-V3 FP8 — 61 x 144 KiB
ROUNDS=2 bash $R store $M/deepseek_v3_fp8.yaml
DURATION_SEC=60 bash $R sustained-load $M/deepseek_v3_fp8.yaml

# Mixtral 8x22B FP8 — 56 x 256 KiB
ROUNDS=2 bash $R store $M/mixtral_8x22b_fp8.yaml
DURATION_SEC=60 bash $R sustained-load $M/mixtral_8x22b_fp8.yaml

# Mixtral 8x22B FP8, 256-token chunk — 56 x 512 KiB
ROUNDS=2 bash $R store $M/mixtral_8x22b_fp8_512k.yaml
DURATION_SEC=60 bash $R sustained-load $M/mixtral_8x22b_fp8_512k.yaml
```

For O_DIRECT, use the padded MiniMax-M3 profile. Its 3,072 B `align_pad`
component brings each object to 9,793,536 B (4096 x 2391), so it works with
the default adapter and the same environment as the other profiles:

```bash
# MiniMax-M3 bf16 TP=8 — 8 x 9.34 MiB padded objects, three components
ROUNDS=2 bash $R store $M/minimax_m3_bf16_tp8_odirect.yaml
DURATION_SEC=60 bash $R sustained-load $M/minimax_m3_bf16_tp8_odirect.yaml
```

The original unpadded MiniMax file describes 9,790,464 B objects. It cannot
use O_DIRECT on a 4096 B filesystem; if you intentionally run it with a
buffered adapter, do not compare that result with the O_DIRECT rows.

Re-running one profile's `store` under a prefix that already holds its corpus
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
PREFIX=deepseek-read-corpus \
BASE_PATH=/mnt/lmcache-kvcache PYTHON=.venv/bin/python \
INITIATORS=2 WORKERS_TOTAL=16 IN_FLIGHT=8 DURATION_SEC=60 \
  bash scripts/ipu-poc/run_geom_multi.sh load \
  scripts/ipu-poc/models/deepseek_v3_fp8.yaml
```

For a real benchmark result, retain the command line, resolved profile SHA,
result JSON, host counters, and target storage telemetry. The helpers do not
make acceptance decisions or replace workload-specific validation.
