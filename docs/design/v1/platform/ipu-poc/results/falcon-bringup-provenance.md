# Falcon bring-up provenance — mkp1 ↔ mkp2 100 GbE IPU link

**What this is.** A sanitized, reviewable record of the Falcon bring-up on the
`mkp1`/`mkp2` pair, extracted from a raw operator session log
(`Falcon_Setup_MKP.txt`) that cannot be committed: it contains host root
credentials, chassis serial numbers, and internal-only URLs. Everything here is
topology, software versions, commands, and measured results — nothing sensitive.
Cite this file, not the raw log.

**Why it matters.** It is the evidence that the `mkp1`↔`mkp2` data plane is a
**Falcon-backed direct IPU-to-IPU link** rather than a commodity E810/RoCEv2
path. Earlier revisions of the result documents in this directory inferred
"E810, RoCEv2, not Falcon" from host driver naming (`idpf`, `irdma`,
`rocep69s0f0`); that inference was wrong. Those names are the host-facing
software stack layered *above* Falcon, and the part is an IPU.

**Date of the captured session.** Feature Pack 0.8 drop 3 era, prior to
2026-08-02. The session log is undated internally; treat every number below as a
**historical baseline**, not a statement about the currently running feature
pack. See §5.

---

## 1. Topology

```
HOST 1 IPU QSFP Port 0 (100 GbE)  <---->  HOST 2 IPU QSFP Port 0 (100 GbE)
```

Direct attach, no switch. Data-plane addresses `200.0.0.35` (host 1) ↔
`200.0.0.37` (host 2) on `ens2f0`.

The Falcon app's own topology rendering, reproduced from the session log, shows
the host stack sitting above Falcon rather than replacing it:

```
     |---------------|  idpf   |-----------|             |------------| idpf   |-----------|
     |  Host1        |---------| falcon    |PF0      PF0 |  falcon    |--------|  Host2    |
     |  RDMA App     |---------|  MKP      |-------------|   MKP      |--------| RDMA App  |
      200.0.0.35                                                                200.0.0.37
     |---------------|  irdma  |-----------|             |------------| irdma  |-----------|
```

## 2. Platform and software versions

| Attribute | Value |
| --- | --- |
| Hosts | Inspur NF5280M7, Intel Xeon Gold 6430, 128 CPUs (64C/128T), x86_64 |
| Feature Pack | `feature_pack_release_0_8_drop3` |
| RDMA device | `rocep69s0f0` |
| Host stack | `idpf` (control plane) + `irdma` (verbs) |
| `vendor_id` | `0x8086` |
| `vendor_part_id` | `5202` |
| `hw_ver` | `0x21` |
| `fw_ver` | `1.145` |
| `transport` (as reported by verbs) | `InfiniBand (0)` |
| `active_mtu` / `max_mtu` | 4096 |
| Link type | Ethernet |

`vendor_part_id 5202` with PCI ID `8086:1452` under driver `idpf` identifies an
IPU. It is not an E810.

Falcon app state, from the bring-up sequence on both ACCs:

```
falcon server secure channel true
start_rtcmd response: success=True, message=RtCmd started
```

## 3. Perftest RC baselines

All runs: `rocep69s0f0`, connection type **RC**, `Mtu 4096[B]`, link type
Ethernet, 65536-byte messages, `--report_gbits`.

| Test | Direction | Iterations | BW peak (Gb/s) | BW average (Gb/s) | MsgRate (Mpps) |
| --- | --- | --- | ---: | ---: | ---: |
| `ib_send_bw` | .37 → .35 | 1000 | 0.00 [^1] | **96.37** | 0.183811 |
| `ib_send_bw` | .35 → .37 | 1000 | 96.01 | **95.98** | 0.183061 |
| `ib_write_bw` | .35 → .37 | 5000 | 95.89 | **95.89** | 0.182900 |
| `ib_read_bw` | .35 → .37 | 1000 | 92.85 | **92.84** | 0.177083 |

[^1]: Reported as `0.00` by perftest on that side; the average is the usable
figure.

These meet the plan's §5.2 target shape (`ib_send_bw` ~96, `ib_write_bw` ~96,
`ib_read_bw` ~93 Gb/s at 64 KiB) — **for the feature pack captured in that
session.**

## 4. Bring-up procedure

From the Feature Pack 0.8 README, with `config.yaml` per host already set to
load the P4 package, IDPF and irdma drivers, and to configure the Falcon app
with the correct host PF MAC address:

```
# Step 1 — load the P4 package and set up the node policy
python feature_pack.py -fp_init_falcon

# Step 2 — start the Falcon rtcmd app
python feature_pack.py -falcon_rdma_app
```

Operational note: on gRPC errors, delete the generated certs folder on the ACC
(`/opt/ipu_accel_server/certs/`) and re-run init — the tool re-copies
`/opt/ipu_accel_server` to the ACC during the init phase.

## 5. What this does and does not establish

**Establishes:**

- The two IPU QSFP port 0s are directly cabled at 100 GbE.
- The Falcon `rtcmd` app was running on both ACCs, with `idpf`/`irdma` layered
  above it, carrying `200.0.0.35`↔`200.0.0.37` on `ens2f0` — the same device and
  addresses every result in this directory used.
- The part is an IPU (`vendor_part_id 5202`, PCI `8086:1452`), not an E810.
- RC perftest baselines at ~96/96/93 Gb/s on Feature Pack 0.8 drop 3.

**Does NOT establish:**

- **The on-wire packet format.** The evidence shows the Falcon transport app
  carrying the link; it does not capture headers. Do not assert "not
  RoCEv2/UDP" — the supportable wording is "Falcon-backed direct IPU link,
  `idpf` + `irdma` host stack, RoCE-style verbs on top."
- **Which feature pack is running now.** The session is a historical capture.
  The plan's §5.2 Falcon/perftest gate asks for reproduction on the *current*
  feature pack, and therefore remains **unmet**: closing it needs a manifest
  pinning the running feature pack plus a fresh perftest run.
- **Any Falcon offload capability.** Falcon transport being available is not
  Falcon offload being implemented. In every measurement on this rig to date the
  IPU acts solely as the `irdma` verbs device under the kernel
  `nvme_rdma`/`nvmet_rdma` path — no completion-polling offload, no admission
  offload. Do not cite this file as progress on the offload beads.
- **Anything about the 400 GbE / 4×400 GbE MMG platform**, which remains
  unavailable. No figure here extrapolates to it.
