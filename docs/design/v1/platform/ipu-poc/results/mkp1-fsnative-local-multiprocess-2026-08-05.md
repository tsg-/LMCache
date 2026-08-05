# fs_native local multi-process reads - mkp1, 2026-08-05

## Result

Two and four independent `lmcache bench l2` processes on mkp1 concurrently
read the same immutable 28 MiB fs_native corpus over the existing
mkp1-to-mkp2 NVMe-oF path. Both 120-second cells passed the aggregate
acceptance gate at 95.94 Gbps.

This is **existing-controller, local multi-process, read-only functional
evidence**. It proves that independent LMCache adapter instances on one
initiator host can share read traffic against the prepopulated corpus without
misses or fabric errors. It is not physical multi-initiator, MP/coordinator,
shared-writer, 64-QP/R2, or 400 GbE evidence. It also does not test fresh-QP
scale or compare Falcon offload with an unoffloaded path.

| Local processes | Aggregate goodput | Start skew | Counter/app rate | Result |
|---:|---:|---:|---:|---|
| 2 | 95.9417 Gbps | 1 ms | 1.0000348 | accepted |
| 4 | 95.9436 Gbps | 1 ms | 1.0000015 | accepted |

The 100 GbE link was already saturated by one process, so flat aggregate
goodput is expected. These cells establish functional fan-in, not scaling past
the link ceiling.

## Topology And Configuration

| Item | Value |
|---|---|
| Initiator | mkp1, local independent `lmcache bench l2` processes |
| Target | mkp2 `nvmet-rdma`, two existing controllers at `200.0.0.37` |
| Initiator storage | XFS on `md0`, striped across `nvme2n1` and `nvme3n1` |
| Fabric | 100 GbE Falcon-backed kernel NVMe-oF, `rocep69s0f0` |
| Corpus | `ds28m`, 11,072 keys x 28 MiB = 303 GiB, manifest-validated |
| Adapter per process | `fs_native`, `num_workers=4`, `use_odirect=true` |
| Load geometry per process | `--only load --num-keys 1 --data-size-kb 28672 --in-flight 4 --rounds 2768 --warmup-rounds 0` |
| Window | 10 s discarded warmup, 120 s measured |

The driver derives `rounds = corpus_keys / in_flight` so each process wraps
inside the prepopulated 11,072-key range. It does not store, mount, reconnect,
or create a controller or QP.

## Per-Process Results

| Cell | Process | Goodput | Keys successful / total | Window |
|---|---:|---:|---:|---:|
| 2 processes | 0 | 46.73 Gbps | 23,877 / 23,877 | 120.009 s |
| 2 processes | 1 | 49.21 Gbps | 25,143 / 25,143 | 120.009 s |
| 4 processes | 0 | 20.73 Gbps | 10,590 / 10,590 | 120.015 s |
| 4 processes | 1 | 22.91 Gbps | 11,706 / 11,706 | 120.019 s |
| 4 processes | 2 | 23.52 Gbps | 12,020 / 12,020 | 120.018 s |
| 4 processes | 3 | 28.79 Gbps | 14,708 / 14,708 | 120.014 s |

The split is not fair, particularly in the four-process cell. The acceptance
criterion is aggregate integrity and counter agreement; do not use these
per-process rates as a fairness result.

## Acceptance Evidence

The local release barrier records each measured-window marker. The report
rejects a cell unless all processes exit zero, remain in sustained mode,
complete every requested key, start within two seconds, leave the corpus count
unchanged, and have a counter rate within 5 percent of the sum of per-process
successful-byte rates.

For both cells:

- Corpus count stayed at 11,072 before and after.
- `RetransSegs`, `Nak Sequence Error`, `RTO`, `RNR received`, `Rcvd Out of
  order packets`, and `InProtoErrors` all remained zero.
- `InRdmaWrites` was fitted over the common interior of every process's
  measured window, excluding startup, discarded warmup, and edge-refresh lag.

Raw artifacts remain on mkp1:

```text
/root/mkp1-sustained/dnt21785882969/
/root/mkp1-sustained/dnt41785883140/
```

Each directory contains every process's JSON result, timestamped log, exit
status, the RDMA poll trace, and `aggregate.json`.

The installed benchmark source was `66cba0a5`. The local driver and reporter
were uncommitted when copied to mkp1; their deployed SHA-256 values were:

```text
96c18666db4feecc90982e6cc0ca7f869aca48f756f7b75d27d2b658787dc17f  run_multi_initiator_load.sh
7e184c45dc02c90be7d0964f4657b8968d651f4257eec5286be5bd536b5abe94  multi_initiator_report.py
```

## Next Boundary

Replicate this orchestration on the 4x400 GbE platform using separate
initiator nodes and exclusive L2 pools. A shared writable namespace remains
out of scope: it needs a target-side metadata authority for conditional create,
allocation, WAL/replay, and GC. This result does not validate that authority.
