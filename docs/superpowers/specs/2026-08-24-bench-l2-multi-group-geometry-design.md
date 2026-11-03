# Multi-Group Geometry for `bench l2`

## Decision

Extend `lmcache bench l2` so one logical submit can contain the same
L2-visible object-group shape as the multiprocess runtime: one `MemoryObj` per
`(chunk, object_group_id, kv_rank)`, where each object may pack several kernel
groups with different tensor layouts.

This preserves the benchmark's existing unit of concurrency:
`--in-flight 8` means eight complete model-shaped adapter submissions, not
eight submissions per group.

Existing profiles remain a legacy synthetic page-burst mode. New
object-group profiles use the production key dimensions and object packing.

## Scope

The implementation will:

- preserve raw `--num-keys` plus `--data-size-kb` behavior;
- preserve all existing uniform YAML profiles, metrics, and key namespaces;
- add object-group YAML profiles with heterogeneous L2 objects;
- build heterogeneous `MemoryObj` views from one registered L1 buffer;
- execute the same geometry in rounds, sustained, and mixed modes;
- account requested and successful bytes exactly, including partial loads;
- expose aggregate, object-group, and component geometry in structured output;
- add source-grounded profiles for representative hybrid-attention models;
- update every profile helper, operator document, and focused test that assumes
  one uniform page size.

The implementation will not:

- allocate real model tensors;
- run a serving engine or reproduce its attention-selection policy;
- reproduce token-level prefix hit folding across hybrid object groups;
- change the adapter API;
- change the existing inline `--kvcache-shape-spec` contract;
- make an end-to-end inference or HBM-transfer claim.

The new YAML contract distinguishes:

- **component**: one kernel-group payload packed inside an object;
- **object group**: the L2 key and `MemoryObj` unit identified by
  `ObjectKey.object_group_id`;
- **submit**: an ordered batch of objects across chunks, object groups, and KV
  ranks passed to one adapter task.

Engine groups and kernel groups remain profile provenance. The adapter sees
only the resulting object keys and flat object byte sizes.

## Profile Contract

### Existing uniform profiles

The current profile form remains valid without modification:

```yaml
model:
  name: deepseek-ai/DeepSeek-V3
architecture:
  num_layers: 61
  attention: mla
  kv_lora_rank: 512
  qk_rope_head_dim: 64
quantization:
  dtype: float8_e4m3fn
  dtype_bytes: 1
chunking:
  tokens_per_chunk: 256
page:
  page_size_bytes: 147456
burst:
  layers_per_burst: 61
  burst_bytes: 8994816
```

The resolver converts this form into one legacy synthetic group. Existing
metrics, object order, key generation, store/load corpus compatibility, and
CLI behavior remain unchanged.

### Object-group profiles

A production-shaped profile uses top-level `runtime` and `object_groups`
sections:

```yaml
model:
  name: example/hybrid-model

runtime:
  lmcache_tokens_per_chunk: 256
  task_archetype: lookup_load
  chunks_per_submit: 2
  kv_ranks_per_chunk: 1
  separate_object_groups: false
  full_sw_kv: false

object_groups:
  - object_group_id: 0
    name: full_attention
    sw_size_chunks: -1
    components:
      - name: main_kv
        role: key_value
        cache_owning_layers: 60
        architecture:
          attention: gqa
          kv_size: 2
          num_kv_heads: 4
          head_size: 128
        quantization:
          dtype: bfloat16
          dtype_bytes: 2
        block_geometry:
          tokens_per_block: 128
          slots_per_block: 128
          transfer_tokens_per_chunk: 256
        component_size_bytes: 31457280

      - name: sparse_index
        role: key_only_index
        cache_owning_layers: 60
        architecture:
          attention: gqa
          kv_size: 1
          num_kv_heads: 1
          head_size: 128
        quantization:
          dtype: bfloat16
          dtype_bytes: 2
        block_geometry:
          tokens_per_block: 128
          slots_per_block: 128
          transfer_tokens_per_chunk: 256
        component_size_bytes: 3932160

    object_size_bytes: 35389440

burst:
  objects_per_submit: 2
  burst_bytes: 70778880
```

Object-group IDs must be unique, dense, and start at zero. Names must be
non-empty and unique within their scope. Object-group order is numeric ID
order. Component order is significant because it mirrors the packed
`MemoryLayoutDesc` order recorded by LMCache.

`task_archetype` is one of `store`, `lookup_load`, or `shared_envelope`.
`kv_ranks_per_chunk` is the number of ranks carried by one adapter task, not
the model's total TP degree. Production store tasks are normally bucketed to
one KV rank, while lookup/load tasks may contain every rank. A profile must
state which task boundary its captured geometry represents. The benchmark can
execute the declared shape in any operation for corpus setup and comparison,
but it claims controller-level task-shape fidelity only for the declared
archetype. `shared_envelope` is the explicit synthetic choice for mixed
read/write testing.

A profile must use exactly one geometry form: either the legacy top-level
`architecture`, `quantization`, `chunking`, and `page` sections or the new
`runtime` plus `object_groups` form. Mixing both forms is rejected instead of
silently choosing one.

`cache_owning_layers` counts the tensors physically packed into that
component. Profiles exclude cross-layer sharing aliases because the target
owner already carries their bytes. Different components are separate objects
only when they belong to different LMCache object groups. For example, with
object-group separation disabled, MiniMax-M3's main K/V and key-only indexer
are two components packed into one object-group `MemoryObj`.

`burst.objects_per_submit` and `burst.burst_bytes` are optional redundant
checks. When present, they must equal the values derived from the object
groups, chunks, and KV ranks.

### Component and object validation

For a formula-backed component:

```text
slots_per_object =
    transfer_tokens_per_chunk * slots_per_block / tokens_per_block
```

The division must be exact. `transfer_tokens_per_chunk` is the logical token
range actually copied for one LMCache object. It equals
`lmcache_tokens_per_chunk` for full attention and full-window storage, but may
be smaller for a sub-chunk sliding-window transfer.

For a `gqa` component:

```text
component_size_bytes =
    cache_owning_layers
  * kv_size
  * num_kv_heads
  * head_size
  * dtype_bytes
  * slots_per_object
```

For an `mla` component:

```text
component_size_bytes =
    cache_owning_layers
  * (kv_lora_rank + qk_rope_head_dim)
  * dtype_bytes
  * slots_per_object
```

`architecture.cached_elems_per_token` remains an allowed MLA shorthand and is
cross-checked when the component fields are also present.

Architecture dimensions describe the tensor registered for one KV rank, after
the serving engine applies tensor-parallel partitioning. Profiles must not
insert global model dimensions and divide them again in the benchmark.

Some recurrent-state, mixed-dtype MLA, or compressed sparse-attention
components are byte-opaque after the serving engine lays them out. Such a
component declares `architecture.attention: opaque` and supplies
`component_size_bytes` directly. Profile comments must identify the
authoritative registration/runtime capture and all engine, dtype,
tensor-parallel, block-size, chunk-size, and object-group-separation settings
on which the size depends.

`object_size_bytes` equals the sum of its component sizes.
`objects_per_submit` equals:

```text
chunks_per_submit * number_of_object_groups * kv_ranks_per_chunk
```

`burst_bytes` is `chunks_per_submit * kv_ranks_per_chunk` times the sum of the
object-group sizes.

Every L2 object size must be a multiple of `--l1-align-bytes`. New profiles do
not need object sizes to be an integer number of KiB because they are not
represented through `--data-size-kb`.

## Resolved Geometry

Add immutable resolved types:

- `L2GeometryComponent`: name, role, cache-owning layer count, component size,
  and formula/runtime provenance fields;
- `L2ObjectGroupGeometry`: object-group ID, name, sliding-window metadata,
  ordered components, and total object size;
- `L2SubmitGeometry`: source provenance, model name, task archetype, chunk and
  KV-rank fan-out, object-group runtime settings, object groups, and ordered
  object descriptors.

Each object descriptor contains:

- chunk ordinal;
- object-group ID;
- KV-rank ordinal;
- object size in bytes.

`L2SubmitGeometry` exposes:

- `objects_per_submit`;
- `object_sizes_bytes` in production key order;
- `task_size_bytes`;
- `is_uniform`;
- legacy-only page-size and token-count accessors used by compatibility paths.

Raw CLI geometry becomes the same internal shape: one synthetic group
containing `--num-keys` objects of `--data-size-kb * 1024` bytes. Downstream
execution therefore consumes one object-size vector regardless of the input
form.

Existing YAML and inline shape-spec inputs retain their current uniform
page-burst behavior. Heterogeneous inline shape specs remain rejected because
the grammar describes kernel groups but cannot express object-group
membership, chunks per submit, or KV-rank fan-out. Object-group execution is
enabled only by the explicit YAML contract.

## Object Construction and Buffer Layout

Add a data helper that accepts an ordered object-size sequence. Starting at a
caller-provided base offset, it creates contiguous `TensorMemoryObj` views
using prefix sums:

```text
object 0: [base, base + size[0])
object 1: [base + size[0], base + size[0] + size[1])
...
```

The existing uniform helper remains as a compatibility wrapper.

One submit consumes `task_size_bytes`. One in-flight wave consumes
`in_flight * task_size_bytes`. Store and load each receive a separate wave, so
the registered L1 buffer remains:

```text
2 * in_flight * task_size_bytes
```

All object starts remain aligned because the buffer base and every preceding
object size are aligned.

Fill patterns continue to use flattened object position. Store and load
batches therefore retain byte-level round-trip verification without requiring
group-aware comparison logic.

## Keys and Corpus Compatibility

Legacy raw, YAML, and inline geometry continue using `make_object_keys`
unchanged.

New object-group profiles use a separate deterministic key builder. For each
logical chunk:

- `chunk_hash` is derived from the global chunk index and is shared by all
  object groups and KV ranks for that chunk;
- `object_group_id` comes from the resolved object group;
- `kv_rank` is produced with `ObjectKey.ComputeKVRank` from
  `kv_ranks_per_chunk`;
- keys are flattened in production order:
  `chunk -> object group -> kv_rank`.

This has two consequences:

1. existing raw and uniform-profile corpora remain readable with their current
   prefix and geometry;
2. object-group store and load runs address the same corpus when they use the
   same profile, prefix, rounds, warmup, and in-flight settings.

Changing group order or geometry changes the meaning of positions but not the
key namespace. Operators must treat the profile SHA as part of corpus
identity. Documentation will require matching profile SHA between store and
load.

## Execution Semantics

For each logical submit:

1. select `chunks_per_submit` consecutive chunk indices;
2. expand each chunk across object groups and KV ranks;
3. generate matching keys and heterogeneous object views in
   `chunk -> object group -> kv_rank` order;
4. call one `submit_store_task`, `submit_load_task`, or
   `submit_lookup_and_lock_task`.

Rounds mode still issues `in_flight` submits and drains the wave. Sustained
mode still holds `in_flight` complete submits outstanding. Mixed mode still
shares one global in-flight window between reads and writes.

Read and write submissions use the same geometry, so the existing
submit-count issue ratio remains the payload-byte ratio.

For new object-group profiles, lookup hit/miss selection is chunk-atomic:
every object group and KV rank for a selected chunk is either drawn from the
potentially existing range or from the guaranteed-miss range. This preserves
the production invariant that a usable chunk hit covers all required object
groups. Legacy geometry retains its existing key-based lookup semantics.

Sustained loads wrap by logical submit slot, not by individual object.
Sustained stores advance by `chunks_per_submit`, preserving the existing
fresh-key guarantee while keeping every group and rank for one chunk on the
same hash.

## Exact Byte Accounting

`BenchResult` will distinguish object counts from payload bytes.

Add:

- `payload_bytes_per_submit`, defaulting to
  `num_keys * data_size_bytes` for existing uniform callers;
- per-round successful-byte counts;
- a running `success_bytes_total`;
- `record_success(keys, bytes_transferred=None)`.

Uniform callers may omit `bytes_transferred`; the existing multiplication is
used. Heterogeneous callers always provide it.

Requested bytes are:

```text
rounds mode:
    measured rounds * in_flight * payload_bytes_per_submit

sustained mode:
    completed submits * payload_bytes_per_submit
```

This preserves the existing requested-byte semantics: a timed-out rounds-mode
task remains part of the submitted wave, while sustained mode counts only
submits whose completion was observed.

Store completion is all-or-none, so a successful store contributes the full
submit payload.

For loads, the adapter bitmap is matched by index against
`object_sizes_bytes`. Successful bytes are the sum of the sizes whose bitmap
bits are set. This prevents a small-object hit and a large-object hit from
being counted as equivalent payload.

Warmup stripping must slice both successful-object and successful-byte
history. Prometheus and final summary throughput use the same running byte
totals.

Existing uniform `BenchResult` construction remains source compatible.

## Metrics and Operator Output

The aggregate `geometry` section will contain:

- profile path and SHA, or canonical inline shape spec;
- model name;
- profile mode (`legacy_page_burst` or `object_group`);
- task archetype;
- chunks per submit;
- KV ranks per chunk;
- object-group separation and full-window-storage settings;
- object-group count;
- objects per submit;
- task payload bytes;
- whether the geometry is uniform.

Legacy uniform profiles continue to emit the current top-level
`tokens_per_chunk` and `page_size_bytes` fields.

Each object group is emitted with `Metrics.add_list_section` under
`geometry_object_groups`, containing:

- object-group ID;
- name;
- sliding-window chunks;
- component count;
- object size bytes;
- bytes contributed per submit.

Components are emitted under `geometry_components`, keyed by object-group ID
and component index, with name, role, cache-owning layers, block geometry,
dtype, and component size.

The configuration summary reports `data_size_kb` only for raw and legacy
uniform geometry. Object-group output reports `task_size_bytes` and the
object-group list instead of presenting an averaged page size.

Operation summaries retain key/object counts for continuity and use exact
requested and successful byte totals for throughput.

## Model Profiles

Add one exact, revision-pinned profile for each of these models:

- `MiniMaxAI/MiniMax-M3`, TP=8, block size 128, default object-group
  separation: one object group containing main-K/V and key-only-indexer
  components;
- `google/gemma-4-E4B-it`, object-group separation enabled: sliding-window and
  full-attention objects, excluding
  cross-layer-sharing aliases;
- `Qwen/Qwen3.6-27B`, TP=1, unified block size 784, object-group separation
  enabled: opaque GDN state and full-attention objects;
- `moonshotai/Kimi-Linear-48B-A3B-Instruct`, TP=2, unified block size 944,
  object-group separation enabled: TP-dependent KDA state and MLA objects;
- `deepseek-ai/DeepSeek-V4-Flash`, object-group separation enabled, with an
  exact vLLM revision and `fp8_ds_mla` configuration: compressed
  sparse-attention components with distinct logical-token and physical-slot
  geometry.

Every new YAML file starts with comments stating:

- what the profile stresses;
- the exact model identifier;
- model and engine revisions;
- the geometry source, preferably a captured LMCache registration dump;
- required engine, dtype, tensor-parallel, and chunk-size assumptions;
- the `separate_object_groups` setting;
- the `full_sw_kv` setting;
- the intended task archetype and its per-task KV-rank count;
- `chunks_per_submit` and `kv_ranks_per_chunk`;
- that it is an L2 storage-envelope profile rather than a serving trace.

No profile is added with guessed dimensions. If a component is byte-opaque,
its size must come from checked-in runtime metadata, an official model
configuration, or a captured vLLM registration/startup result identified in
the comments.

## Error Handling

The CLI exits with usage status `2` before adapter creation when:

- `runtime` or `object_groups` is absent or empty in the object-group form;
- `task_archetype` is not `store`, `lookup_load`, or `shared_envelope`;
- object-group IDs are not dense from zero;
- an object-group or component name is duplicated;
- a required field is missing or non-positive;
- block geometry cannot produce an integral slot count;
- a formula-backed component size does not match its declared dimensions;
- an opaque component omits its component size;
- an object size does not equal its component-size sum;
- legacy and object-group geometry sections are mixed in one profile;
- a declared burst total does not match the derived total;
- any object size violates `--l1-align-bytes`;
- explicit raw geometry is combined with a profile or inline shape spec.

Timeout and adapter failures retain their current behavior.

## Testing

Tests will be written before implementation and will cover:

1. legacy profile resolution and metrics remain unchanged;
2. an object-group profile resolves the exact
   `chunk -> object group -> kv_rank` descriptor vector and total bytes;
3. task archetype and KV-rank fan-out are recorded without inferring them from
   model TP;
4. heterogeneous inline shape specs remain rejected with a clear message;
5. malformed, duplicate, inconsistent, and misaligned object groups fail before
   adapter creation;
6. heterogeneous objects occupy the expected aligned shared-buffer ranges;
7. one adapter task receives the full production-ordered key/object list with
   matching chunk hashes, object-group IDs, and KV ranks;
8. rounds, sustained, and mixed modes preserve one full geometry per submit;
9. partial load bitmaps sum successful bytes by object index;
10. warmup stripping preserves exact successful-byte accounting;
11. lookup hit/miss selection is whole-chunk atomic for object-group profiles;
12. JSON metrics contain aggregate geometry, object groups, and components;
13. store followed by load with the same object-group profile and prefix passes
    round-trip verification using the filesystem or mock adapter;
14. `run_model_geometry.sh` displays legacy and object-group profiles;
15. `geom_readback.py` verifies per-object sizes and object-group-aware keys;
16. `geom_report.py` and `geom_multi_report.py` validate task bytes and group
    geometry without requiring one `page_kb`;
17. legacy helper behavior and historical uniform result parsing remain
    compatible;
18. all new model profiles resolve and their declared object and burst totals
    match.

Focused verification:

```bash
/bin/zsh -lc \
  'PYTHONPATH=$PWD .venv/bin/python -m pytest \
   tests/cli/commands/bench/l2_adapter_bench/ -q -p no:randomly'
```

The final change also runs the relevant lint checks and `git diff --check`.

## Documentation

Update `scripts/ipu-poc/README-model-geometry.md` to:

- describe legacy page-burst and object-group profiles;
- define `--in-flight` as complete model-shaped submissions;
- define chunks per submit and KV ranks per chunk;
- show resolved object groups, components, and total payload;
- require profile SHA agreement between corpus creation and readback;
- list all shipped profiles and their stress pattern;
- retain the current storage-envelope claim boundary.

Update all profile consumers:

- `scripts/ipu-poc/run_model_geometry.sh`;
- `scripts/ipu-poc/run_geom_multi.sh`;
- `scripts/ipu-poc/geom_readback.py`;
- `scripts/ipu-poc/geom_calib.py`;
- `scripts/ipu-poc/geom_report.py`;
- `scripts/ipu-poc/geom_multi_report.py`.

Each consumer either handles the resolved object descriptor vector or
explicitly rejects object-group profiles before starting a benchmark. The
operator path must not silently fall back to one page size.

Update `bench l2` help to describe object-group YAML profiles while retaining
the current uniform restriction for inline shape specs.

## Acceptance Criteria

The change is complete when:

- existing raw and uniform-profile tests pass unchanged or with output-only
  additions;
- MiniMax-M3 executes its main-K/V and indexer components as one packed
  object-group object per chunk and KV rank under its declared default server
  configuration;
- Qwen3.6 and Kimi-Linear profiles execute distinct object-group sizes with
  production-shaped `object_group_id` keys;
- key order is `chunk -> object group -> kv_rank`;
- requested and successful throughput reflect exact heterogeneous bytes;
- all execution modes use the same resolved geometry;
- all profile helper and reporting paths handle or explicitly reject
  object-group profiles;
- all new profile files resolve without assumptions hidden outside their
  comments;
- focused tests, lint, and `git diff --check` pass.
