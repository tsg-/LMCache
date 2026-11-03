---
marp: true
theme: default
paginate: true
style: |
  :root {
    --bg: #ffffff;
    --panel: #f6f8fa;
    --border: #d0d7de;
    --blue: #0969da;
    --green: #1a7f37;
    --red: #cf222e;
    --purple: #6639ba;
    --text: #1f2328;
    --muted: #57606a;
  }

  section {
    background: var(--bg);
    color: var(--text);
    font-family: "Segoe UI", "Inter", system-ui, sans-serif;
    font-size: 20px;
    padding: 34px 48px 78px;
  }

  h1 {
    color: var(--blue);
    font-size: 1.55em;
    line-height: 1.15;
    margin: 0 0 0.45em;
    padding-bottom: 0.22em;
    border-bottom: 2px solid var(--border);
  }

  h2 {
    color: var(--purple);
    font-size: 0.92em;
    margin: 0.45em 0 0.2em;
  }

  p { margin: 0.25em 0; line-height: 1.3; }
  ul { margin: 0.3em 0; padding-left: 1.15em; }
  li { margin: 0.28em 0; line-height: 1.27; }
  li::marker { color: var(--blue); }

  .cols {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 26px;
    align-items: start;
  }

  .flow {
    grid-template-columns: 43% 57%;
    gap: 22px;
  }

  .flow-copy { width: 41%; }

  .hero {
    color: var(--blue);
    font-size: 3.4em;
    font-weight: 800;
    line-height: 0.95;
    letter-spacing: 0;
    margin: 0.08em 0 0;
  }

  .hero-sub {
    color: var(--green);
    font-size: 1.2em;
    font-weight: 700;
    margin: 0.22em 0 0.55em;
  }

  .panel {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 0.65em 0.85em;
    margin: 0.45em 0;
  }

  .panel strong { color: var(--blue); }
  .accent-green { border-left: 4px solid var(--green); }
  .accent-red { border-left: 4px solid var(--red); }
  .accent-purple { border-left: 4px solid var(--purple); }

  .label {
    color: var(--muted);
    font-size: 0.78em;
    line-height: 1.25;
  }

  .path {
    color: var(--text);
    font-size: 0.9em;
    font-weight: 600;
    line-height: 1.38;
  }

  .caption {
    color: var(--muted);
    font-size: 0.72em;
    line-height: 1.25;
    margin-top: 0.4em;
  }

  .figure {
    text-align: center;
    padding-top: 0.3em;
  }

  .figure img {
    max-height: 395px;
    max-width: 100%;
  }

  .metric-row {
    display: grid;
    grid-template-columns: repeat(3, 1fr);
    gap: 10px;
    margin: 0.4em 0 0.55em;
  }

  .metric {
    border-top: 3px solid var(--blue);
    background: var(--panel);
    padding: 0.45em 0.55em;
    font-size: 0.78em;
  }

  .metric b {
    color: var(--blue);
    display: block;
    font-size: 1.35em;
  }

  .plan {
    grid-template-columns: 55% 45%;
    gap: 20px;
  }

  .plan-copy {
    width: 43%;
    margin-left: 57%;
  }

  .figure-caption {
    width: 53%;
    color: var(--muted);
    font-size: 0.72em;
    line-height: 1.25;
    margin-top: 14.5em;
  }

  footer {
    color: var(--muted);
    font-size: 0.66em;
    border-top: 1px solid var(--border);
  }

  section::after {
    color: var(--muted);
    font-size: 0.68em;
  }
---
<!-- _footer: "IPU KV Cache PoC | Measured on one 100 GbE MEV link" -->

# Model-page remote L2 traffic reaches the 100 GbE wire ceiling

<div class="cols">
<div>
<div class="hero">95.35 Gb/s</div>
<div class="hero-sub">99.4% of the 95.92 Gb/s fio remote-NVMe ceiling</div>

<div class="panel accent-green">
<strong>DeepSeek-V3 request geometry</strong><br>
61 objects × 144 KiB per 256-token chunk = 8.58 MiB per submit
</div>

<div class="metric-row">
<div class="metric"><b>82.29</b>W=16</div>
<div class="metric"><b>95.35</b>W=32</div>
<div class="metric"><b>44.26</b>W=64</div>
</div>

<p class="path">The active tuning knob is <code>num_workers</code>, not
<code>--in-flight</code>.</p>
</div>
<div>
<h2>Same link, same 32-worker budget</h2>
<ul>
<li>1 process × 32 workers: <strong>95.35 Gb/s</strong></li>
<li>2 processes × 16 workers: <strong>95.58 Gb/s</strong></li>
<li>4 processes × 8 workers: <strong>94.99 Gb/s</strong></li>
</ul>

<div class="panel accent-purple">
<strong>Run configuration</strong><br>
<code>fs_native</code>, W=32, <code>O_DIRECT</code>, in-flight 8, XFS on
md0 RAID0 over 2 NVMe-oF namespaces.
</div>

<div class="panel">
<strong>Evidence</strong><br>
All pages completed; six fabric-error deltas were zero; corpus remained at
292,800 objects; counter ratios stayed within 0.9970–1.0060.
</div>
</div>
</div>

<p class="caption"><strong>Classification:</strong> Falcon-offloaded kernel NVMe-oF, existing-controller, local multi-process <code>fs_native</code> sustained read. No unoffloaded control, fresh-QP, physical multi-initiator, 64-QP, 400 GbE, or mixed-read/write claim. Corpus: 40.2 GiB O_DIRECT re-read corpus, approximately 33 re-reads per 120 s window.</p>

<!--
[Sources]
- scripts/ipu-poc/README-model-geometry.md: DeepSeek geometry, measured W=32,
  fan-in, integrity, and corpus-caveat data.
- fio raw remote-NVMe baseline in the same result record.
-->

---
<!-- _footer: "IPU KV Cache PoC | Retrieve path: measured and byte-verified" -->

# Retrieve places SSD data directly into initiator receive memory

![bg right:56% contain](diagrams/architecture-a-nvmeof-falcon-retrieve-flow.svg)

<div class="flow-copy">
<div class="panel accent-green">
<strong>1. NVMe-oF READ</strong><br>
The capsule carries an SGL for the initiator receive memory region.
</div>

<div class="panel accent-green">
<strong>2. Read at the target</strong><br>
SSD data reaches target DRAM through the target Xeon storage stack.
</div>

<div class="panel accent-green">
<strong>3. RDMA WRITE to the initiator</strong><br>
The target posts the transfer into the supplied receive memory region.
</div>

<p class="path">The read path was measured at model-page geometry and
byte-verified. There is no host-side post-completion payload copy, unlike an
NVMe/TCP C2H path.</p>

<p class="caption">The diagram labels the Falcon interface range as
400–1600 Gb/s. This evidence is from one 100 GbE MEV link.</p>
</div>

<!--
[Sources]
- diagrams/architecture-a-nvmeof-falcon-retrieve-flow.puml and .svg:
  Architecture A retrieve sequence.
- scripts/ipu-poc/README-model-geometry.md: measured and byte-verified
  read-path evidence.
-->

---
<!-- _footer: "IPU KV Cache PoC | Store path: transport mechanism exercised in prepopulation" -->

# Store pulls initiator data before writing it to SSD

![bg right:56% contain](diagrams/architecture-a-nvmeof-falcon-store-flow.svg)

<div class="flow-copy">
<div class="panel accent-purple">
<strong>1. NVMe-oF WRITE</strong><br>
The capsule identifies the initiator transmit memory region.
</div>

<div class="panel accent-purple">
<strong>2. RDMA READ at the target</strong><br>
The target pulls the payload into target DRAM.
</div>

<div class="panel accent-purple">
<strong>3. Target storage write</strong><br>
The target Xeon writes the payload to the NVMe namespace.
</div>

<p class="path">Standard kernel NVMe-oF transport. No cache decision or
storage service runs on the IPU.</p>

<div class="panel accent-red">
<strong>Current evidence boundary</strong><br>
The mechanism ran during prepopulation. A byte-verified sustained 5:1 result is
still pending.
</div>

<p class="caption">The diagram's 400–1600 Gb/s label is the interface range;
today's evidence is one 100 GbE MEV link.</p>
</div>

<!--
[Sources]
- diagrams/architecture-a-nvmeof-falcon-store-flow.puml and .svg:
  Architecture A store sequence.
- scripts/ipu-poc/README-model-geometry.md: sustained mixed verification
  boundary.
-->

---
<!-- _footer: "IPU KV Cache PoC | Current 100 GbE evidence; future MMG scale-out plan" -->

# The 100 GbE proof point now drives the 4x400 GbE plan

![bg left:54% contain](diagrams/mkp-fsnative-4x400-test-architecture.svg)

<div class="plan-copy">
<div class="panel accent-green">
<strong>Current proof point</strong><br>
One 100 GbE MEV link reaches 95.35 Gb/s at DeepSeek-V3 model-page geometry.
</div>

<div class="panel accent-purple">
<strong>Phase 1 target</strong><br>
1x400 GbE, 256 KiB, 100% read, 64 aggregate QPs, at least 45 GB/s sustained
goodput.
</div>

<div class="panel">
<strong>Scale-out system</strong><br>
2-socket Xeon owns <code>nvmet</code>/<code>nvmet_rdma</code>, target control,
and storage. 4x MMG-400 IPUs carry Falcon transport only. 16x Gen5 x4 NVMe
SSDs provide the target media.
</div>

<div class="panel accent-red">
<strong>4x400 GbE follow-on</strong><br>
Requires at least 180 GB/s of local block and NVMe-oF loopback capacity before
the link result is meaningful.
</div>
</div>

<p class="figure-caption">Scale-out namespace-pool mapping. Each physical
initiator starts with its own 4-SSD pool; shared reads are immutable and mixed
writes use fresh, disjoint prefixes or namespaces.</p>

<!--
[Sources]
- diagrams/mkp-fsnative-4x400-test-architecture.mmd and .svg:
  multi-initiator namespace-pool mapping.
- Anthropic NVMe-oF PoC plan: future 1x400 and 4x400 scope, hardware,
  64-QP headline cell, and scale-out capacity gate.
-->
