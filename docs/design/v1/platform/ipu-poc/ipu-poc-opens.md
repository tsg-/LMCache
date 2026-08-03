---
marp: true
theme: default
paginate: true
style: |
  :root {
    --color-bg: #ffffff;
    --color-bg-alt: #f6f8fa;
    --color-border: #d0d7de;
    --color-accent: #0550ae;
    --color-accent2: #1a7f37;
    --color-accent3: #cf222e;
    --color-accent4: #6639ba;
    --color-blue: #0969da;
    --color-text: #1f2328;
    --color-muted: #57606a;
    --color-warn: #9a6700;
  }

  section {
    background: var(--color-bg);
    color: var(--color-text);
    font-family: 'Segoe UI', 'Inter', system-ui, sans-serif;
    font-size: 19px;
    padding: 36px 48px 88px 48px;
  }

  /* Title */
  section.title {
    background: linear-gradient(150deg, #f0f6ff 0%, #ffffff 55%, #f6f8fa 100%);
    border-top: 6px solid var(--color-blue);
    display: flex;
    flex-direction: column;
    justify-content: center;
  }
  section.title h1 {
    font-size: 2.1em;
    font-weight: 800;
    color: var(--color-blue);
    line-height: 1.15;
    margin: 0 0 0.25em 0;
    border: none;
  }
  section.title h2 {
    font-size: 1.1em;
    font-weight: 400;
    color: var(--color-text);
    margin: 0 0 0.15em 0;
    border: none;
  }
  section.title .byline {
    color: var(--color-muted);
    font-size: 0.82em;
    margin-top: 1.8em;
    border-top: 1px solid var(--color-border);
    padding-top: 0.9em;
  }

  /* Small — dense slides (long <pre>, wide tables, deep card grids).
     Scales everything proportionally by dropping the base font size. */
  section.small {
    font-size: 15px;
    padding: 28px 40px 76px 40px;
  }
  section.small pre { font-size: 0.70em; line-height: 1.35; padding: 0.7em 0.9em; }
  section.small ul li { margin: 0.18em 0; line-height: 1.30; }
  section.small table { font-size: 0.78em; }
  section.small .card { font-size: 0.80em; padding: 0.6em 0.85em; }

  /* Headings */
  h1 {
    font-size: 1.5em;
    font-weight: 700;
    color: var(--color-blue);
    border-bottom: 2px solid var(--color-border);
    padding-bottom: 0.25em;
    margin-bottom: 0.5em;
  }
  h2 {
    font-size: 1.0em;
    font-weight: 600;
    color: var(--color-accent4);
    margin: 0.5em 0 0.25em 0;
  }
  h3 {
    font-size: 0.82em;
    font-weight: 700;
    color: var(--color-accent2);
    margin: 0.35em 0 0.15em 0;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }

  /* Code */
  code {
    background: #eef2f7;
    color: var(--color-accent);
    border: 1px solid #c8d5e8;
    border-radius: 4px;
    padding: 0.1em 0.4em;
    font-size: 0.82em;
    font-family: 'JetBrains Mono', 'Fira Code', monospace;
  }
  pre {
    background: #f6f8fa;
    border: 1px solid var(--color-border);
    border-left: 4px solid var(--color-blue);
    border-radius: 6px;
    padding: 0.9em 1.1em;
    font-size: 0.74em;
    line-height: 1.5;
    overflow: hidden;
  }
  pre code {
    background: transparent;
    border: none;
    padding: 0;
    color: var(--color-accent);
  }

  /* Blockquote */
  blockquote {
    background: #f0f6ff;
    border-left: 4px solid var(--color-blue);
    border-radius: 0 6px 6px 0;
    margin: 0.5em 0;
    padding: 0.55em 1em;
    color: var(--color-text);
    font-size: 0.86em;
  }
  blockquote p { margin: 0; }
  blockquote strong { color: var(--color-blue); }

  /* Lists */
  ul { padding-left: 1.3em; margin: 0.2em 0; }
  ul li { margin: 0.28em 0; line-height: 1.38; }
  ul li::marker { color: var(--color-blue); }

  /* Tables */
  table { font-size: 0.82em; }
  th { background: var(--color-bg-alt); }

  /* Columns */
  .cols {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 22px;
    margin-top: 0.5em;
  }
  .card {
    background: var(--color-bg-alt);
    border: 1px solid var(--color-border);
    border-radius: 8px;
    padding: 0.75em 1em;
    font-size: 0.86em;
  }
  .card ul { margin: 0.2em 0; }
  .card h2, .card h3 { margin-top: 0; }
  .card-green  { border-top: 3px solid var(--color-accent2); }
  .card-blue   { border-top: 3px solid var(--color-blue); }
  .card-red    { border-top: 3px solid var(--color-accent3); }
  .card-purple { border-top: 3px solid var(--color-accent4); }
  .card-amber  { border-top: 3px solid var(--color-warn); }

  /* Footer / paginate */
  section::after {
    color: var(--color-muted);
    font-size: 0.68em;
  }
  footer {
    color: var(--color-muted);
    font-size: 0.66em;
    border-top: 1px solid var(--color-border);
  }
---

<!-- _class: title -->
<!-- _paginate: false -->

# Inference KV Cache Offload with Intel IPU

## LMCache remote tiering over RDMA/Falcon; preliminary MEV bring-up against the completed CX7 reference baseline

<div class="byline">
July 2026
</div>

---
<!-- _footer: "IPU KV Cache PoC" -->

# Scope questions (superseded 2026-07-21)

The following questions were raised early in scoping and have since been
answered by the two-track split. Retained here as historical context.

- Is NVMe-on-initiator a hard requirement, or open to direct RDMA? → Both
  tracks are now measured independently. See
  [nvmeof-initiator-only-alternative.md](nvmeof-initiator-only-alternative.md)
  for the initiator-only + remote NVMe-oF path.
- Was the early storage-owned baseline intended as an NVMe-oF
  bandwidth/offload proof? → No. That work was the storage-owned RDMA
  baseline (M1 raw-verbs, done). The NVMe-oF alternative is a
  parallel track with its own durability + recovery gate before any
  headline number is claimed.
- Or do they want to see the full cache serving model (dedup, admission)? →
  Full cache serving lives on the storage-owned track (M2+).

---
<!-- _footer: "IPU KV Cache PoC" -->

# Three Namespaces Used Throughout This Deck

Everything in the Architecture A section below refers to one of three
independent namespaces. They are not synonyms and they are not
sequential.

| Namespace | What it names | Values used here |
|---|---|---|
| **Architecture A / B** | Software ownership model — where cache semantics (hash, admission, allocator, WAL, map) live | A = initiator-owned, B = storage-owned |
| **Platform** | Hardware platforms evaluated under Architecture A | CX7 (Mellanox reference, completed), **MEV (Intel IPU / Falcon — this plan)**, MMG (Intel IPU / MMG-400 / IPT, follow-on ~Aug 2026) |
| **Stage 0–5** | MEV kernel-path delivery milestones | Stage 0 kickoff → Stage 5 workload evidence |

<br/>

**Reading rule:** the plan of record is now the MEV kernel-path
bring-up. CX7 is a completed cross-platform reference (retained for
sanity); the IPU offload phase (D-init / D-tgt / D-both, endpoint TBD)
runs against the MEV T7 kernel-path baseline on the same hosts. MMG is
a follow-on when silicon lands.

**On every MMG slide:** endpoint offload is pending D-init / D-tgt /
D-both. **MMG never owns cache semantics** in any option — it is a
transport engine.

---
<!-- _class: small -->
<!-- _footer: "IPU KV Cache PoC" -->

# Initiator-Owned Cache Semantics, Remote NVMe-oF L2

## Architecture A vs B

| | A: Initiator-owned + remote NVMe-oF L2 | B: Storage-owned RDMA + LMCache server |
|---|---|---|
| Storage role | Passive NVMe-oF namespace (`nvmet-rdma`) | Smart cache (hash, admission, eviction) |
| Initiator role | Owns cache metadata, allocator, WAL durability | Thin (expose MR, request by hash) |
| Durable commit | WAL: intent → payload+FUA → checksum+FUA → commit+flush → publish → ACK. COW deferred. | BLAKE3-on-commit gated by target admission |
| Multi-initiator dedup | Out of scope | Yes (global hash index on server) |
| NVMe framing | Yes (`nvmet-rdma` capsules) | No (raw RDMA verbs) |
| Target admission / lease | None; initiator-local BLAKE3 verify only | Present |
| IPU offload surface | `nvme_rdma` and/or `nvmet-rdma` (D-init / D-tgt / D-both) | Target LMCache + admission |

**Customer-requested lower-risk path.** Measurement objective:
host-CPU-per-GB reduction and MR/QP churn removal on the offloaded
endpoint(s), against the **MEV kernel-path T7 baseline** produced
by this plan (same hardware). CX7 T7 is retained as a cross-platform
reference. B remains the alternative if target admission, dedup, or
multi-initiator semantics prove necessary.

**Not "works today."** The `raw_block` L2 adapter publishes its
in-memory index right after header+payload writes; durable metadata
is a periodic mirrored checkpoint with no fsync/FLUSH/FUA ordering
against payload. A requires the WAL commit-record + map-publish
machinery before it can claim durable cache correctness on crash.

---
<!-- _footer: "IPU KV Cache PoC" -->

# Architecture A — Host vs IPU Roles (per node)

<pre>
INITIATOR NODE                              TARGET (NVMe-oF) NODE
──────────────                              ─────────────────────

Initiator Xeon (full cache brain):          Target Xeon (transport + block only):
 • LMCache engine, token hashing              • Linux nvmet + nvmet-rdma, NQN ACL, namespace export
 • Local {key,digest} idempotency             • Linux kernel block layer, SSD driver
 • L1 (initiator DRAM) management + LRU       • No LMCache, no hash table, no admission
 • L2 allocator + key→LBA map                 • No LRU, no cache DRAM tier
 • WAL: intent, checksum, commit, recovery    • SSD is the durable L2 medium
 • Issues NVMe-oF I/O; returns final ACK

Initiator IPU (D-init or D-both, MEV/MMG):  Target IPU (D-tgt or D-both, MEV/MMG):
 • Terminates NVMe-oF initiator path          • Terminates NVMe-oF target path
   (replaces / accelerates nvme_rdma)           (replaces / accelerates nvmet-rdma)
 • MRs registered by LMCache                  • Drives SSD via NVMe-oF passthrough
 • No cache logic, no WAL, no map             • No cache logic, no WAL, no map
</pre>

<br/>

**Endpoint offload is pending D-init / D-tgt / D-both.** MMG never
owns cache semantics in any option — it is a transport engine.

---
<!-- _class: small -->
<!-- _footer: "IPU KV Cache PoC" -->

# Architecture A — STORE (Initiator-Owned Durable Commit)

<pre>
LMCache (Init Xeon)         Init nvme_rdma      NVMe-oF/RDMA      Target nvmet-rdma        SSD (L2)
───────────────────         ──────────────      ────────────      ─────────────────        ────────
1. store(tokens, kv)
2. GPU HBM → init DRAM
3. Hash → BLAKE3 D;
   {key,D} idempotency
4. L1 put(key, PENDING)     (not lookup-visible)
5. WAL intent + FLUSH  ──► NVMe WRITE+FLUSH ──►                ──► write+flush ──►         intent durable
6. Payload (FUA, 256K) ──► NVMe WRITE ────────►
                           ◄── RDMA READ ─────                 (target pulls init MR)
                           ──── 256K payload ─►                ──► block write+FUA ─►      payload durable
7. Checksum (FUA)     ───► NVMe WRITE (FUA) ──►                ──► write+FUA ─────►       cksum durable
8. WAL COMMIT + FLUSH ───► NVMe WRITE+FLUSH ──►                ──► write+flush ──►         commit durable
                           ◄── flush cqe ─────                                              c5 BEGINS
9. Publish key→LBA + L1 PENDING→VISIBLE (atomic)                                            c5 ENDS
10. Terminal ACK
</pre>

<div class="cols">
<div class="card card-blue">

### c5 window (step 8 cqe → step 9)

COMMITTED record durable on media, map/L1 not yet flipped. Recovery
reconstructs the new value from the WAL exactly once. Fault-matrix
test T4 exercises this boundary.

</div>
<div class="card card-purple">

### ACK-loss retry

Keyed on `{key, digest}`: absent → start; PENDING → join / retry;
VISIBLE → no-op success; different digest for same key → reject.
Never allocate or WAL-write twice.

</div>
</div>

**Wire direction ≠ pull semantics.** `nvmet-rdma` issues the RDMA
Read at step 6 as its normal transport implementation of the NVMe
Write. No target-side cache decision precedes it. Architecture B is
the pull-with-admission model.

---
<!-- _class: small -->
<!-- _footer: "IPU KV Cache PoC" -->

# Architecture A — RETRIEVE (Initiator-Owned Lookup)

<pre>
LMCache (Init Xeon)         Init nvme_rdma      NVMe-oF/RDMA      Target nvmet-rdma        SSD (L2)
───────────────────         ──────────────      ────────────      ─────────────────        ────────
1. retrieve(tokens)
2. Hash tokens → BLAKE3 keys
3. L1 lookup (exclude PENDING)

L1 HIT:  get from L1 → DMA to GPU HBM → resume    (no fabric traffic)

L1 MISS → key→LBA map lookup:
  L2 HIT:  4. io_uring NVMe read (O_DIRECT, 256K)
                          ─── NVMe READ ────►                    ── read ──►                return block
                          ◄── 256K RDMA WRITE ──                 (target → init MR)
           5. Recompute BLAKE3; compare to committed digest
              OK:   optional promote to L1, DMA to GPU HBM
              FAIL: mark stale, discard map entry, return miss
  L2 MISS: return miss → caller recomputes KV
</pre>

<br/>

**No target-side lookup, hash table, admission, or L1 branch.**
The target only serves NVMe-oF commands and moves blocks to/from
the SSD.

---
<!-- _footer: "IPU KV Cache PoC" -->

# Architecture A — MEV Kernel-Path Delivery Stages (2026-07-23)

Stages 0–5 land on the MEV lab (Intel IPU, PCIe Gen4, 1x 100 GbE
Falcon; 2x Samsung PM9A3 Gen4 SSDs on the target — sized to saturate
100 GbE on the read path). CX7 is a completed reference baseline; the
IPU offload phase and MMG are separate follow-on plans.

- **Stage 0** — Freeze contract (namespaces, NQNs, ownership); pass all
  §5.2 gates including Falcon/perftest sanity and fabric fault-injection
  capability
- **Stage 1** — Prove safe NVMe-oF lifecycle over `irdma`/Falcon (T1).
  **Two-week abort rule:** if `nvme_rdma`/`nvmet_rdma` over `irdma`
  doesn't work in that window, D2-kernel-path aborts and the plan
  pivots to a userspace-target/initiator revision
- **Stage 2** — Remote-L2 I/O baseline (block-I/O sweep, SHA-256 verify)
- **Stage 3** — WAL-based durable publication (intent → payload+FUA →
  checksum+FUA → commit+flush → publish → ACK)
- **Stage 4** — Two tracks: T4 crash matrix at all 6 A.3 cutpoints
  (incl. c5 committed-but-not-visible) + T5 fabric-fault matrix on
  named in-flight NVMe operations
- **Stage 5** — LMCache integration + workload evidence; **T7 MEV
  kernel-path baseline** for the follow-on offload phase (same
  hardware) and MMG (cross-platform reference)

<br/>

Architecture B (storage-owned pull) proceeds on its own track with M1
raw-verbs baselines done and M2 admission gating in progress.

---
<!-- _class: small -->
<!-- _footer: "IPU KV Cache PoC" -->

# Open Questions for Anthropic

<div class="cols">
<div class="card card-blue">

### Architecture choice

Does the target need cache-level decisions (dedup, admission, LRU)
BEFORE serving data, or is a passive NVMe-oF namespace acceptable?

An NVMe-oF target's RDMA Read to fetch a Write payload is standard
transport behavior, not cache-semantic pull.

→ Cache-level admission required: prioritize B.
→ Otherwise: A stays eligible.

</div>
<div class="card card-purple">

### Software stack for A

Linux kernel `nvme_rdma` / `nvmet_rdma` (fastest validation), or
SPDK userspace (max control over polling, queueing, CPU)?

Preference may differ per endpoint (initiator vs target).

</div>
</div>

<div class="cols">
<div class="card card-green">

### Success criteria

- Min host-CPU reduction at comparable throughput
- Max p99 latency regression for small I/O
- Max sustained-throughput regression for large I/O
- Reconnect-timeout ceiling; time-to-cache-online after cold restart

</div>
<div class="card card-amber">

### Decision workload

Read-heavy retrieval, write-heavy durable store, or a mixed
KV-cache trace?

</div>
</div>

---
<!-- _footer: "IPU KV Cache PoC" -->

# Architecture B — Raw-Verbs Test Plan (Parallel Track)

Storage-owned RDMA path, shown here for completeness. Not part of
the Architecture A NVMe-oF POC (Stages 0–5).

<div class="cols">
<div class="card card-blue">

### Hard gates (non-zero exit)

- **Manifest preflight** — NUMA, MTU 4096, GID, port active
- **RC-QP connect + completions** — READ/WRITE actually finish
- **Digest match** — payload integrity

<br/>

### Diagnostic (JSON + `eligible_for_baseline=false`)

- NIC counters ±10% wire-byte comparison
- MR-flag evidence
- `BENCH_RDMA_CONTROL` split

<br/>

Row persisted; ingestion filters on `eligible_for_baseline` — no silent promotion.

</div>
<div class="card card-purple">

### Sweep families

- **CX7 baseline** — READ 72 / 128 / 144 / 256 KiB × qd 1, 4, 16
- **Asymmetric** — CX7 source ↔ IPU storage, same shape

<br/>

### IPU progression

- **MEV IPU** — 2× 100 GbE, first Falcon-offload target
- **MMG IPU** — 1× 400 GbE, single-port line-rate proof
- **MMG next-gen** — 4× 400 GbE, aggregate scaling

<br/>

Runner scope freezes after M1: two sweeps, one runner. Transport-neutral verifier and NIXL publish deferred to M4.

</div>
</div>

---
<!-- _class: small -->
<!-- _footer: "IPU KV Cache PoC" -->

# The LMCache storage-backend benchmark — vs. our goals

**Project goal:** validate that D1 (RAID0 remote NVMe-oF, 100 → 400 → 1600 GbE)
sustains vLLM's KV traffic — **store on prefill, retrieve on decode, at
production tail latency**.

<div class="cols">
<div class="card card-green">

### What it DOES answer

- **On-disk bandwidth ceiling** of the backend read / write implementation
- **Bytes / op and file layout** — one flat-dir file per KV chunk, real DeepSeek-V3 shape (28 MiB @ 256-token bf16)
- **Effect of `O_DIRECT` vs page cache** on the write path
- **Working set > DRAM** — sized by chunk count, tunable
- **One concurrency dial** — submission-side (backend I/O pool is a fixed default of 4 workers today, not a CLI knob)

</div>
<div class="card card-amber">

### What it DOES NOT answer — and matters for us

- **End-to-end retrieve latency to the GPU** — no CPU→GPU staging; H2D PCIe / NVLink cost invisible. That step is where a real serve-loop actually pays.
- **Sustained mixed R/W** — strictly two-phase (write-all, then read-all). Real inference traffic overlaps them.
- **Tail latency (p50 / p95 / p99)** — only aggregate ops/s + total elapsed. Cannot tell if the median is fine while p99 is 10×.
- **Back-pressure / pipeline stalls** — no counter surfaced. Cannot tell where the pipeline stalls at 12 GB/s.
- **Memory-pressure eviction** — CPU pool defaults skip this path.
- **Capacity eviction** — can be provoked, but the harness reports attempted operations, not successful reads (needs success accounting first).
- **Cold-cache reads today** — no cache-invalidation between phases → reads hit page cache unless `O_DIRECT` engages **and** the buffer is aligned (it isn't — see next slide).

</div>
</div>

<div class="cols">
<div class="card card-blue">

### What this means for the plan

- The bench is **necessary but not sufficient for an LMCache / vLLM scaling claim at 400 / 1600 GbE.** It bounds the disk-tier ceiling; it does **not** bound the end-to-end retrieve latency vLLM will see. D1's own remote-NVMe baseline exit is a separate, already-scoped step.
- Once the read path is fixed, we get: (a) cold-cache read GB/s at various I/O-pool sizes, (b) put-side bandwidth vs. concurrency.
- We still need a **separate integrated test** (or a harness extension) to cover CPU → GPU staging, sustained mixed R/W, and tail-latency behavior.

</div>
</div>

---
<!-- _class: small -->
<!-- _footer: "IPU KV Cache PoC" -->

# Stage 2 bring-up finding — the harness silently masks read failures

**Setup.** mkp1 (initiator) ↔ mkp2 (target), 100 GbE RoCEv2, md0 RAID0
+ XFS on 2× PM9A3, DeepSeek-V3 KV geometry (28 MiB / chunk), `O_DIRECT`.

<div class="cols">
<div class="card card-red">

### What we saw

- **Executive one-liner: writes moved data over the wire; reads did not.** We corrected the test before making any performance claim.
- Read cell reported **7,746 ops/s at c=1** (≈ 228 GB/s — impossible on 100 GbE)
- RDMA verbs counters: **write phase moved 15.03 GB over the wire; read phase moved ZERO**
- Every "read" was actually an `EINVAL` on the `O_DIRECT` `readinto` syscall

### Root cause

- The read destination allocator returns a non-page-aligned CPU tensor
- The backend opens the file with `O_DIRECT`; kernel rejects the unaligned user buffer with `EINVAL`
- Exception is caught in the read helper, logged at ERROR, slot returns `None`
- Harness times elapsed regardless of success / failure → ops/s looks great

</div>
<div class="card card-purple">

### What we can still trust

- **Write path** — harness aligns its write buffers manually; the backend put path is exercised
- **Single-drive O_DIRECT write** = 2.66 GB/s — an observation, not a bottleneck claim (needs a matched **single-drive** direct-write fio control before it can be compared)

### What we can't (from this harness, today)

- Cold-cache read bandwidth end-to-end
- CPU → GPU staging (never invoked)
- Tail latency, sustained mixed R/W, memory-pressure eviction
- Last night's smoke doc's **6.21 GB/s single-drive O_DIRECT read = 89 % of fio** is invalid and needs retraction

</div>
</div>

<div class="cols">
<div class="card card-green">

### Fixes required before Stage 2 reads are trustworthy

1. **Page-align the read buffer.** `O_DIRECT` requires page alignment; pinning alone is not sufficient.
2. **Invalidate cache between phases** — page-drop each written path before the read phase begins.
3. **Add success / failure accounting** — count `None` returns; fail the run on any failed read so this defect cannot recur unnoticed.
4. **Upstream correctness bug** — the read helper swallows `OSError` and returns `None`. File upstream regardless of the benchmark.

</div>
<div class="card card-blue">

### Retractions from last night's smoke doc

- ❌ **"6.21 GB/s single-drive O_DIRECT read = 89 % of fio ceiling"** — was 512 immediate `EINVAL`s
- ❌ **"The backend does NOT collapse at chunk size"** — not proven; the read path was never measured
- ✅ **Still valid: write observation.** 2.66 GB/s O_DIRECT single-drive is a real number (RDMA counters agree). No comparative claim against fio yet.
- 📋 Doc needs an addendum

</div>
</div>

---
