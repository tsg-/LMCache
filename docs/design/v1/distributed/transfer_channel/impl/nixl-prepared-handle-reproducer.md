# NIXL Prepared-Handle Metadata Reproducer

`scripts/nixl_remote_metadata_reproducer.py` isolates the repeated
`add_remote_agent()` sequence investigated by `LMCache-szh`.

It creates a target NIXL agent and a source agent with a registered 4 KiB DRAM
page, then:

1. Loads the source metadata into the target.
2. Prepares local and remote descriptor-list handles.
3. Reloads either identical metadata or metadata from a second UCX agent with
   the same logical source name.
4. Calls `make_prepped_xfer()` using the pre-existing handles.

The second load runs once on the preparation thread and once on a distinct
thread. Each experiment writes one JSON record. NIXL exceptions are data, not
harness failures, so a complete four-case run exits successfully even when a
case returns `NIXL_ERR_NOT_FOUND`.

```bash
env UCX_TLS=rc_mlx5,ud_mlx5,sm \
  UCX_NET_DEVICES=mlx5_1:1 \
  NIXL_NET_BACKEND=UCX \
  python scripts/nixl_remote_metadata_reproducer.py
```

This is not a full LMCache P2P reproduction. It omits ZMQ handshake ordering,
the P2P controller, and a real cross-host transfer. It can therefore confirm
that a repeated metadata load invalidates an existing prepared handle, but a
negative result cannot disprove the production failure.

## Initial result

On 2026-07-19, the four cases completed on `bmg0` with NIXL 1.3.1 and the UCX
backend:

| Second metadata load | Thread | Result | Prepared transfer |
|---|---|---|---|
| Identical | Same | Succeeded | `make_prepped_xfer()` succeeded |
| Identical | Different | Succeeded | `make_prepped_xfer()` succeeded |
| Different, same agent name | Same | `NIXL_ERR_NOT_ALLOWED` | `make_prepped_xfer()` succeeded |
| Different, same agent name | Different | `NIXL_ERR_NOT_ALLOWED` | `make_prepped_xfer()` succeeded |

The mismatched case uses two independent UCX agents with the same logical
source name. NIXL rejects the second connection information before replacing
the existing remote-agent entry, and no `NIXL_ERR_NOT_FOUND` or invalidated
prepared handle was observed. This rules out that minimal sequence as a
sufficient trigger, but not the production P2P failure: the harness does not
include the bidirectional ZMQ handshake, separate process lifetimes, or a
cross-host transfer request.
