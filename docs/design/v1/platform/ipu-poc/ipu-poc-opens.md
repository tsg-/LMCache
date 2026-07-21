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
- Is Phase 1 scope = bandwidth/offload proof over NVMe-oF? → No.
  Phase 1 = storage-owned RDMA baselines (M1 verbs, done). The
  NVMe-oF alternative is a parallel track with its own durability +
  recovery gate before any headline number is claimed.
- Or do they want to see the full cache serving model (dedup, admission)? →
  Full cache serving lives on the storage-owned track (M2+).

---
<!-- _footer: "IPU KV Cache PoC" -->

# Architecture A vs B (2026-07-21 update)

| | A: Initiator-owned + remote NVMe-oF L2 | B: Storage-owned RDMA + LMCache server (primary) |
|---|---|---|
| Storage role | Passive NVMe-oF namespace | Smart cache (hash, admission, eviction) |
| Initiator role | Owns cache metadata + WAL/COW durability | Thin (expose MR, request by hash) |
| Code readiness | Raw-block I/O exists; NVMe-oF target provisioning, initiator attach/reconnect, atomic durable commits, and crash recovery are all **unbuilt** | M1 verbs baseline done; M2 admission gating in progress |
| Multi-initiator dedup | Requires shared allocator + mapping authority (deferred) | Yes (global hash index on server) |
| NVMe framing | Yes (command capsules + nvmet-rdma) | No (raw RDMA verbs) |
| Cache-level admission / lease / BLAKE3-on-commit | Gone — no target agent | Present |
| Track status | Alt track (`ipu-poc-nvmeof-alt` branch, `LMCache-msm` epic) | Primary track (`ipu-poc` branch) |

**Do not read Architecture A as "works today."** The `raw_block` L2 adapter
publishes its in-memory index immediately after writing header + payload;
durable metadata is a periodic mirrored checkpoint with no fsync/FLUSH/FUA
ordering against payload writes. That path cannot claim durable cache
correctness across a crash without new WAL/COW machinery.

---
<!-- _footer: "IPU KV Cache PoC" -->

# Block vs KV Command Set

- If NVMe is in the picture, KV cmd set is the natural fit
- Block requires hash-to-LBA mapping layer (new code, no benefit)
- LMCache addresses everything by token hash, not LBA

---
<!-- _footer: "IPU KV Cache PoC" -->

# Backpressure at 32 SSDs

- 16x2 Gen5 NVMe = ~450 GB/s aggregate write
- Network delivers 50 GB/s; SSDs absorb 9x that
- "Pipe overwhelms storage" does not apply here

<br/>
Pull model value shifts to:
1. Dedup before transfer (80%+ prefix reuse workloads)
2. Multi-initiator dedup (N writers, same prefix = 1 copy)
3. Tiering control (admission decides what stays in DRAM L1 vs spills to SSD L2)

---
<!-- _footer: "IPU KV Cache PoC" -->

# Second LMCache on Initiator?

- Local HBM/DRAM as L0/L1, remote node as L2
- Hot prefixes served locally (no network RTT)
- Architecturally clean, not needed for initial PoC
- Decision: defer to production phase or include in Phase 2?

---
<!-- _footer: "IPU KV Cache PoC" -->

# Suggested PoC Phasing

**Phase 1: Bandwidth proof (Architecture A)**
- NVMe-oF + IPU, existing raw_block code path
- Proves: line rate, CPU offload
- Aligns with flow Nima confirmed

<br/>
**Phase 2: Smart cache (Architecture B)**
- RDMA + LMCache server, new transport code
- Proves: dedup, admission, multi-initiator scaling
- Intel's value-add pitch

---
<!-- _footer: "IPU KV Cache PoC" -->

# Open Questions for Anthropic

<br/>

**Transport architecture:**
- Is NVMe-on-initiator (NVMe-oF fabric) a hard requirement, or are you open to direct RDMA between registered memory regions?
- If NVMe: block command set or KV command set? LMCache addresses by token hash natively, not LBA.

<br/>

**PoC outcomes:**
- Bandwidth/offload proof only (IPU sustains 400G line rate, CPU out of data path)?
- Or full disaggregated cache serving (server-side dedup, admission control, multi-initiator scaling)?

<br/>

**Initiator-side caching:**
- Should the compute node run its own local cache tier (HBM/DRAM as L0/L1, remote KV Cache Node as L2)?
- Or single-tier remote only for the PoC?

<br/>

**Hardware config:**
- Confirm target SSD config on KV Cache Node (16x2 Gen5 NVMe assumed)
- Multi-initiator (N compute nodes to 1 KV Cache Node) in scope, or single pair?

---
<!-- _footer: "IPU KV Cache PoC" -->

# Next Steps

1. Confirm with Nima: Phase 1 scope sufficient for initial demo?
2. Align internally on phasing (A then B, or B directly?)
3. Draft technical one-pager for account team
4. Scope hardware needs (2-node testbed, 32 SSDs, MMG-400 connectivity)

---
