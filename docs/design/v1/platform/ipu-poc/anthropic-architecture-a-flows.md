---
marp: true
theme: default
paginate: true
footer: "Anthropic System Flows | Architecture A"
style: |
  :root {
    --blue: #0969da;
    --green: #1a7f37;
    --purple: #6639ba;
    --orange: #bc4c00;
    --ink: #1f2328;
    --muted: #57606a;
    --line: #d0d7de;
    --panel: #f6f8fa;
  }

  section {
    background: #ffffff;
    color: var(--ink);
    font-family: "Segoe UI", "Inter", system-ui, sans-serif;
    font-size: 22px;
    padding: 38px 52px 84px;
  }

  section.title {
    border-top: 7px solid var(--blue);
    display: flex;
    flex-direction: column;
    justify-content: center;
  }

  section.title h1 {
    border: 0;
    color: var(--blue);
    font-size: 2.1em;
    margin-bottom: 0.2em;
  }

  h1 {
    color: var(--blue);
    font-size: 1.45em;
    border-bottom: 2px solid var(--line);
    padding-bottom: 0.2em;
    margin-bottom: 0.45em;
  }

  h2 {
    color: var(--purple);
    font-size: 0.95em;
    margin: 0.3em 0 0.15em;
  }

  ul {
    margin: 0.2em 0;
    padding-left: 1.25em;
  }

  li {
    margin: 0.25em 0;
    line-height: 1.3;
  }

  li::marker { color: var(--blue); }

  code {
    background: #eef2f7;
    border: 1px solid #c8d5e8;
    border-radius: 4px;
    color: #0550ae;
    padding: 0.08em 0.32em;
  }

  .cols {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 22px;
  }

  .four-cols {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
  }

  .card {
    background: var(--panel);
    border: 1px solid var(--line);
    border-radius: 7px;
    padding: 0.65em 0.85em;
    font-size: 0.86em;
  }

  .card h2 { margin-top: 0; }
  .card.blue { border-top: 4px solid var(--blue); }
  .card.green { border-top: 4px solid var(--green); }
  .card.purple { border-top: 4px solid var(--purple); }
  .card.orange { border-top: 4px solid var(--orange); }

  .flow {
    background: #f6f8fa;
    border-left: 4px solid var(--blue);
    font-size: 0.95em;
    line-height: 1.5;
    padding: 0.6em 0.8em;
  }

  .takeaway {
    background: #f0f6ff;
    border-left: 4px solid var(--blue);
    color: var(--ink);
    font-size: 0.91em;
    margin-top: 0.55em;
    padding: 0.5em 0.8em;
  }

  section::after {
    color: var(--muted);
    font-size: 0.65em;
  }
---

<!-- _class: title -->
<!-- _paginate: false -->

# Anthropic System Flows

## Architecture A enablement walkthrough

Initiator-owned cache semantics with remote NVMe-oF L2

<br/>

**Purpose:** align silicon, firmware, software, and system teams on the
flows the platform must enable.

---

# What This Meeting Is For

<div class="cols">
<div class="card blue">

## Walk through

- Where KV bytes and metadata move
- What completes each operation
- What an IPU may offload later
- Which platform assumptions need validation

</div>
<div class="card purple">

## Not deciding today

- Architecture A versus B
- Final IPU endpoint selection
- Benchmark results or product claims
- Detailed POC execution plan

</div>
</div>

<div class="takeaway">
<strong>Working model:</strong> Architecture A keeps cache ownership on the
initiator. The IPU is a candidate transport engine, not a new cache owner.
</div>

---

# System View: L0, L1, L2

![bg right:43% contain](diagrams/architecture-a-cx7-hardware-topology.svg)

## Current CX7 baseline

- **L0:** GPU/TPU HBM, consumed by inference.
- **L1:** initiator Xeon DRAM, registered DMA buffers and local cache.
- **L2:** remote NVMe namespace, reached through NVMe-oF/RDMA.
- **Initiator Xeon:** LMCache, map, allocator, admission, WAL/recovery.
- **Target Xeon:** `nvmet-rdma` and SSD block layer; no LMCache agent.

<div class="takeaway">
The baseline intentionally puts no IPU on the data path. It establishes the
transport and cache contract that an IPU implementation must preserve.
</div>

---

# Retrieve Flow: Remote L2 to GPU/TPU HBM

<div class="flow">
<strong>1.</strong> LMCache resolves a key on the initiator.<br/>
<strong>2.</strong> L2 read brings bytes over NVMe-oF/RDMA into registered
initiator DRAM.<br/>
<strong>3.</strong> Initiator verifies the committed digest and promotes the
page to L1 as appropriate.<br/>
<strong>4.</strong> GPU connector DMA moves the page from host DRAM to L0 HBM.
</div>

<div class="cols">
<div class="card blue">

## Data path

L2 NVMe → fabric → L1 DRAM → L0 HBM

The byte path is staged through host DRAM in the current design.

</div>
<div class="card green">

## Enablement question

Can the platform move registered buffers and complete the transfer without
host CPUs loading, storing, or copying KV payload bytes?

</div>
</div>

---

# Store Flow: GPU/TPU HBM to Durable L2

<div class="flow">
<strong>1.</strong> GPU/TPU DMA stages a KV page in initiator DRAM.<br/>
<strong>2.</strong> Initiator records intent, writes payload and checksum to
remote NVMe, then persists the commit record.<br/>
<strong>3.</strong> Only after the commit barrier does it publish the new
key-to-LBA mapping and return the terminal ACK.
</div>

<div class="cols">
<div class="card purple">

## Ownership stays on Xeon

Key hashing, admission, allocator, WAL, retry semantics, and recovery remain
initiator-owned.

</div>
<div class="card orange">

## Platform-relevant boundary

The IPU may carry NVMe-oF transport and queue/completion work. It must not
silently change ordering, error visibility, or the meaning of ACK.

</div>
</div>

---

# Failure Flow: What Must Remain True

<div class="cols">
<div class="card blue">

## Fabric failure

- In-flight I/O surfaces an error
- Link recovers at contracted parameters
- Initiator reconnects and replays
- No reduced-rate success is accepted as recovery

</div>
<div class="card green">

## Process restart

- Pre-commit writes do not become visible
- A committed generation reconstructs once
- Superseded extents are not reused too early
- Client retry remains idempotent

</div>
</div>

<div class="takeaway">
The offload design is acceptable only if it preserves these software-visible
outcomes, including the ordering of errors and completions.
</div>

---

# What Is Intentionally Not on the IPU

<div class="cols">
<div class="card purple">

## Cache semantics

- LMCache Engine / StorageManager
- Key-to-LBA map and allocator
- WAL, replay, durable-ACK authority
- Admission, deduplication, eviction, multi-initiator coordination

</div>
<div class="card blue">

## Cache residency

- GPU/TPU HBM management
- Host DRAM as the DMA staging area
- KV pages as an IPU-memory cache

The IPU streams payloads; its small local cache is not an L1/L2 extension.

</div>
</div>

<div class="takeaway">
<strong>Customer preference (2026-06-22):</strong> eliminate host-DRAM staging
on the payload path if achievable; do <em>not</em> substitute IPU local memory
as the staging tier. MMG-400's 32&nbsp;MB SRAM is too tight and treating it as
an L1/L2 extension is out of scope for this generation.
</div>

<div class="takeaway">
<strong>IPU role:</strong> offload byte movement and transport work, not cache
ownership.
</div>

---

# What Each Team Should Pressure-Test

<div class="four-cols">
<div class="card blue">

## Silicon

DMA engines, queue/CQ scale, memory ordering, cache behavior.

</div>
<div class="card green">

## Firmware

Link recovery, error propagation, counters, reset boundaries.

</div>
<div class="card purple">

## Software

API boundary, buffer registration, completion ownership, observability.

</div>
<div class="card orange">

## System / board

IPU and SSD topology, NUMA, link rate, MTU, fault domains.

</div>
</div>

<div class="takeaway">
<strong>Discussion prompt:</strong> What platform constraint would invalidate
one of these flows or force a different software boundary?
</div>

---

# Follow-Up We Need From This Group

1. Identify any hardware, firmware, or board assumption that is false.
2. Confirm what evidence proves zero CPU payload touch on the chosen endpoint.
3. Identify required error, completion, and telemetry semantics.
4. Name owners for the IPU endpoint contract and platform topology.

<div class="takeaway">
No platform decision is requested in this meeting. The output is a concrete
enablement contract that lets the follow-on MEV/MMG work start without changing
the cache contract.
</div>
