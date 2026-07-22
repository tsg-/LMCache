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

# KV Cache Offload over RDMA

## Details on LMCache based prototype with IPU as RDMA/Falcon NIC

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
| **Platform** | Hardware platforms evaluated separately under Architecture A | CX7 (Mellanox baseline, this POC), MEV (Intel IPU / Falcon), MMG (Intel IPU / MMG-400 / IPT) |
| **Stage 0–5** | CX7 delivery milestones only | Stage 0 kickoff → Stage 5 workload evidence |

<br/>

**Reading rule:** MEV and MMG are follow-on platform integrations
under Architecture A, not "later CX7 stages" and not "Architecture C
or D." Each IPU platform independently chooses which endpoint(s) it
offloads (D-init, D-tgt, or D-both — see the plan doc's Appendix D).

**On every MMG slide:** endpoint offload is pending D-init / D-tgt /
D-both. **MMG never owns cache semantics** in any option — it is a
transport engine.

---
<!-- _footer: "IPU KV Cache PoC" -->

# Initiator-Owned Cache Semantics, Remote NVMe-oF L2

## Architecture A vs B

| | A: Initiator-owned + remote NVMe-oF L2 | B: Storage-owned RDMA + LMCache server |
|---|---|---|
| Storage role | Passive NVMe-oF namespace (`nvmet-rdma`) | Smart cache (hash, admission, eviction) |
| Initiator role | Owns cache metadata, allocator, WAL durability | Thin (expose MR, request by hash) |
| Durable-commit protocol | WAL (intent → payload+FUA → checksum+FUA → commit record + flush → map publish → ACK). COW deferred post-POC. | BLAKE3-on-commit gated by target-side admission |
| Multi-initiator dedup | Out of scope (single-initiator exclusive namespace) | Yes (global hash index on server) |
| NVMe framing | Yes (command capsules + `nvmet-rdma`) | No (raw RDMA verbs) |
| Cache-level admission / lease / BLAKE3-on-commit | Initiator-local admission and checksum verification; no target-side admission, lease, or verifier | Present |
| IPU offload surfaces | Initiator `nvme_rdma` and/or target `nvmet-rdma` termination (MEV: Falcon reliable transport; MMG: IPT) — endpoint pending D-init / D-tgt / D-both | Target-side LMCache + admission control |
| Track status | Initiator-owned NVMe-oF POC | Storage-owned track |

**Architecture A is the customer-requested lower-risk path.** The
concrete measurement objective is host-CPU-per-GB reduction and
`nvme_rdma` / `nvmet_rdma` MR/QP churn removal on the offloaded
endpoint(s), against the CX7 T7 baseline. Architecture B remains the
alternative if target-side cache admission, deduplication, or
multi-initiator cache semantics prove necessary.

**Do not read Architecture A as "works today."** The `raw_block` L2 adapter
publishes its in-memory index immediately after writing header + payload;
durable metadata is a periodic mirrored checkpoint with no fsync/FLUSH/FUA
ordering against payload writes. Architecture A therefore requires the WAL
commit-record + map-publish machinery specified in `nvmeof-poc-plan.md`
before it can claim durable cache correctness across a crash.

---
<!-- _footer: "IPU KV Cache PoC" -->

# Architecture A — Host vs IPU Roles (per node)

<pre>
INITIATOR NODE                              TARGET (NVMe-oF) NODE
──────────────                              ─────────────────────

Initiator Xeon (full cache brain):          Target Xeon (transport + block only):
 • LMCache engine, token hashing              • Linux nvmet + nvmet-rdma, NQN ACL, namespace export
 • Local {key,digest} idempotency (§A.5)      • Linux kernel block layer, SSD driver
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

**Endpoint offload is pending D-init / D-tgt / D-both** (see plan
Appendix D.2). MMG never owns cache semantics in any option — it is a
transport engine.

---
<!-- _footer: "IPU KV Cache PoC" -->

# Architecture A — STORE (Initiator-Owned Durable Commit)

<pre>
LMCache (Initiator Xeon)          Initiator nvme_rdma        NVMe-oF/RDMA         Target nvmet-rdma          SSD (L2 media)
────────────────────────          ───────────────────        ────────────         ─────────────────          ──────────────
1. store(tokens, kv_tensor)
2. GPU HBM → initiator DRAM (fence)
3. Hash tokens; BLAKE3 digest D;
   {key,digest} idempotency check (§A.5)
4. L1 put(key, PENDING)              ← not lookup-visible
5. WAL intent {key, LBA, D} + FLUSH
                                    ─── NVMe WRITE + FLUSH ────►                  ─── write + flush ──►      intent durable

6. Payload write, FUA, 256 KiB
                                    ─── NVMe WRITE command ────►
                                    ◄── RDMA READ request ─────                    target pulls initiator MR
                                    ─── 256 KiB payload ───────►
                                                                                ─── block write + FUA ──►      payload durable

7. Checksum-record write, FUA
                                    ─── NVMe WRITE (FUA) ──────►                  ─── write + FUA ────►      checksum durable

8. WAL COMMITTED record + FLUSH
                                    ─── NVMe WRITE + FLUSH ────►                  ─── write + flush ──►      commit durable
                                    ◄── flush completion ──────                   ◄── completion ─────       (c5 BEGINS on completion)

9. Publish key→LBA map ATOMICALLY  ◄── c5 ENDS
   with L1 PENDING → VISIBLE flip
10. Terminal ACK to caller
</pre>

<h3>Three contract details the diagram assumes</h3>

- **c5 window** (step 8 flush completion → step 9): the COMMITTED
  record is durable on media but the in-memory map/L1 has not yet
  flipped. Recovery reconstructs the new value from the WAL exactly
  once. Fault-matrix test T4 exercises this boundary.
- **ACK-loss retry (§A.5).** Client retry keyed on `{key, digest}`:
  absent → start; matches PENDING → join / retryable; matches VISIBLE
  → no-op success; different digest for same key → reject. Never
  allocate or WAL-write twice.
- **Wire direction ≠ pull semantics.** The target's `nvmet-rdma`
  issues an RDMA Read as its normal transport implementation of the
  NVMe Write at step 6. No target-side cache decision precedes it.
  Architecture B is the pull-with-admission model.

---
<!-- _footer: "IPU KV Cache PoC" -->

# Architecture A — RETRIEVE (Initiator-Owned Lookup)

<pre>
LMCache (Initiator Xeon)          Initiator nvme_rdma        NVMe-oF/RDMA         Target nvmet-rdma          SSD (L2 media)
────────────────────────          ───────────────────        ────────────         ─────────────────          ──────────────
1. retrieve(tokens)
2. Hash tokens → chunk keys (BLAKE3)
3. L1 lookup (initiator DRAM, exclude PENDING)

├── L1 HIT (page in initiator DRAM):
│    4. get from L1 → DMA to GPU HBM → resume        (no fabric traffic)
│
├── L1 MISS → initiator key→LBA map lookup:
│    │
│    ├── L2 HIT (map entry present):
│    │    5. io_uring NVMe read (O_DIRECT, 256 KiB)
│    │                              ─── NVMe READ ─────►                    ─── read ──────►                 return block
│    │                              ◄── 256 KiB RDMA WRITE ──                target writes block into initiator MR
│    │    6. Recompute BLAKE3; compare to committed digest
│    │       ├── OK:   optional promote to L1, DMA to GPU HBM
│    │       └── FAIL: mark stale, discard map entry, return miss
│    │
│    └── L2 MISS (no map entry):
│         return miss → caller recomputes KV
</pre>

<br/>

**No target-side lookup, no target hash table, no target admission,
no target-side L1 hit/miss branches.** The target only serves NVMe-oF
commands and moves blocks to/from the SSD.

---
<!-- _footer: "IPU KV Cache PoC" -->

# Architecture A — CX7 Delivery Stages (2026-07-22)

Stages 0–5 are the CX7 platform delivery for Architecture A.
MEV and MMG are separate platform integrations. See `nvmeof-poc-plan.md`.

- **Stage 0** — Freeze contract (namespace, NQNs, ownership boundary)
- **Stage 1** — Prove safe NVMe-oF lifecycle (idempotent attach/detach,
  ACL/media guards). Hard no-go: `nvmet` / `nvmet_rdma` must load.
- **Stage 2** — Remote-L2 I/O baseline (block-I/O sweep, SHA-256 verify)
- **Stage 3** — WAL-based durable publication (intent → payload+FUA →
  checksum+FUA → commit record + flush → map publish → ACK)
- **Stage 4** — Fault + recovery matrix at all 6 WAL cutpoints, incl.
  c5 committed-but-not-visible and allocator-collision check on replay
- **Stage 5** — LMCache integration + workload evidence; T7 baseline
  measurements for the later MEV / MMG platform plans

<br/>

Architecture B (storage-owned pull) proceeds on its own track with M1
raw-verbs baselines done and M2 admission gating in progress.

---
<!-- _footer: "IPU KV Cache PoC" -->

# Open Questions for Anthropic

**Architecture choice:**
- Does the target need to make cache-level decisions (dedup,
  admission, LRU) BEFORE serving data, or is a passive NVMe-oF
  namespace acceptable?
  Note: an NVMe-oF target issuing an RDMA Read to fetch a Write
  payload is standard transport behavior, not cache-semantic pull.
  → If cache-level admission is required, prioritize B.
  → If not, A stays eligible.

**Software implementation for Architecture A:**
- Linux kernel `nvme_rdma` / `nvmet_rdma` (fastest validation), or
  SPDK userspace (max control over polling, queueing, CPU)?
- Preference may differ per endpoint (initiator vs target).

**Performance and operational success criteria:**
- Minimum host-CPU reduction at comparable throughput
- Maximum p99 latency regression for small, latency-sensitive I/O
- Maximum sustained-throughput regression for large, concurrent I/O
- Reconnect-timeout ceiling; time-to-cache-online after cold restart

**Decision workload:**
- Read-heavy retrieval, write-heavy durable store, or the mixed
  KV-cache trace from plan §6.2?

---
<!-- _footer: "IPU KV Cache PoC" -->

# Architecture B — Raw-Verbs Test Plan (Parallel Track)

Storage-owned RDMA path, shown here for completeness. Not part of the
Architecture A NVMe-oF POC — its test plan is `nvmeof-poc-plan.md`
Stages 0–5.

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
<!-- _footer: "IPU KV Cache PoC" -->

# Next Steps

1. Confirm with Nima: is the initial demo scope (Architecture A on
   CX7, Stages 0–5) sufficient for the customer decision review?
2. Align internally on track ordering (A first, or B directly?)
3. Draft technical one-pager for account team
4. Scope hardware needs for Architecture A CX7 delivery (2-node
   testbed, one exclusive namespace; SSD count TBD with customer).
   MEV / MMG platform hardware scoped separately per plan Appendix D.

---
