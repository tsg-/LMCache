"""Minimal HPU-only LMCache round‑trip harness.

Usage (on a Gaudi/HPU host with the SynapseAI / Habana PyTorch stack installed):

    python -m lmcache.tests.hpu_roundtrip_test \
        --num-layers 2 --num-pages 4 --page-size 16 --num-heads 4 --head-size 8 \
        --dtype float16

This will:
  1. Build a v1 LMCacheEngine configured for CPU staging + HPU KV source.
  2. Allocate synthetic per‑layer KV caches on HPU in the expected paged layout:
       [2, num_pages, page_size, num_heads, head_size]
  3. Snapshot original KV contents, store them into LMCache (offload to CPU).
  4. Zero the on‑device KV caches.
  5. Retrieve from LMCache back into the HPU tensors.
  6. Validate a full tensor equality (within tolerance) to prove correctness of
     HPU connector + engine integration (fallback or c_ops path).

The script is self‑contained and does not depend on vLLM. It focuses purely on
LMCache + HPU connector plumbing.
"""

from __future__ import annotations

# Standard
import argparse
import math
import sys
import time
from typing import List

# Third Party
import torch

# First Party (LMCache)
from lmcache.config import LMCacheEngineMetadata
from lmcache.v1.cache_engine import LMCacheEngineBuilder
from lmcache.v1.config import LMCacheEngineConfig
from lmcache.v1.hpu_connector import VLLMPagedMemHPUConnectorV2


def _fail(msg: str):
    print(f"[HPU-HARNESS][FAIL] {msg}", file=sys.stderr)
    sys.exit(1)


def parse_args():
    p = argparse.ArgumentParser(description="HPU LMCache round-trip test harness")
    p.add_argument("--num-layers", type=int, default=2)
    p.add_argument("--num-pages", type=int, default=4, help="Pages per layer")
    p.add_argument("--page-size", type=int, default=16, help="Tokens per page")
    p.add_argument("--num-heads", type=int, default=4)
    p.add_argument("--head-size", type=int, default=8)
    p.add_argument("--dtype", type=str, default="float16", choices=[
        "float16", "bfloat16", "float32"
    ])
    p.add_argument("--engine-id", type=str, default="hpu_test_engine")
    p.add_argument("--seed", type=int, default=2025)
    p.add_argument("--tolerance", type=float, default=1e-3)
    p.add_argument(
        "--verbose", action="store_true", help="Print per-phase diagnostics"
    )
    return p.parse_args()


def get_dtype(name: str) -> torch.dtype:
    return {
        "float16": torch.float16,
        "bfloat16": torch.bfloat16,
        "float32": torch.float32,
    }[name]


def main():
    args = parse_args()

    if not hasattr(torch, "hpu") or not torch.hpu.is_available():  # type: ignore[attr-defined]
        print("[HPU-HARNESS][SKIP] torch.hpu not available in this environment.")
        sys.exit(0)

    torch.manual_seed(args.seed)

    device = torch.device("hpu")
    dtype = get_dtype(args.dtype)

    num_layers = args.num_layers
    num_pages = args.num_pages
    page_size = args.page_size
    num_heads = args.num_heads
    head_size = args.head_size

    hidden_dim_size = num_heads * head_size
    total_tokens = num_pages * page_size

    if args.verbose:
        print(
            f"Config: layers={num_layers} pages={num_pages} page_size={page_size} "
            f"heads={num_heads} head_size={head_size} hidden={hidden_dim_size} tokens={total_tokens}"
        )

    # --- Build v1 LMCache config ---
    cfg = LMCacheEngineConfig.from_defaults()
    # Adapt key fields for a tiny test footprint
    cfg.chunk_size = page_size  # chunk size aligns to page_size for easier mapping
    cfg.max_local_cpu_size = 0.05  # ~50MB upper bound (well above our tiny test)
    cfg.enable_async_loading = False
    cfg.use_layerwise = False
    cfg.enable_controller = False

    # --- Metadata (fmt unused in basic path, choose 'KV_2LTD') ---
    kv_shape = (num_layers, 2, cfg.chunk_size, num_heads, head_size)
    metadata = LMCacheEngineMetadata(
        model_name="dummy-hpu-model",
        world_size=1,
        worker_id=0,
        fmt="KV_2LTD",  # semantic tag only; engine derives MemoryFormat internally
        kv_dtype=dtype,
        kv_shape=kv_shape,
        use_mla=False,
    )

    # --- HPU Connector ---
    connector = VLLMPagedMemHPUConnectorV2(
        hidden_dim_size=hidden_dim_size,
        num_layers=num_layers,
        use_gpu=True,  # allocate intermediate buffer for fallback/manual path
        chunk_size=cfg.chunk_size,
        dtype=dtype,
        device=device,
        use_mla=False,
    )

    # World-size=1 no-op broadcast helpers
    def _broadcast_fn(t: torch.Tensor, src: int):  # noqa: ANN001
        return None

    def _broadcast_obj_fn(o, src: int):  # noqa: ANN001
        return o

    engine = LMCacheEngineBuilder.get_or_create(
        args.engine_id, cfg, metadata, connector, _broadcast_fn, _broadcast_obj_fn
    )

    # Allocate synthetic KV caches (layer-major list of tensors)
    # Shape: [2, num_pages, page_size, num_heads, head_size]
    kvcaches: List[torch.Tensor] = [
        torch.randn(2, num_pages, page_size, num_heads, head_size, device=device, dtype=dtype)
        for _ in range(num_layers)
    ]

    slot_mapping = torch.arange(total_tokens, device=device, dtype=torch.long)

    # Provide kvcaches to connector/engine (sets internal pointer list)
    engine.post_init(kvcaches=kvcaches)

    # Random test tokens (values not semantically interpreted by harness)
    tokens = torch.arange(total_tokens, dtype=torch.long)

    # Snapshot BEFORE store
    original = [kv_layer.detach().clone() for kv_layer in kvcaches]

    if args.verbose:
        print("Storing KV to LMCache (offloading from HPU -> CPU)...")
    t0 = time.perf_counter()
    engine.store(
        tokens=tokens,
        mask=None,
        slot_mapping=slot_mapping,
        kvcaches=kvcaches,
    )
    store_time = time.perf_counter() - t0

    # Zero out on-device caches to ensure retrieval repopulates
    for kv in kvcaches:
        kv.zero_()

    if args.verbose:
        print("Retrieving KV back to HPU from LMCache...")
    t1 = time.perf_counter()
    ret_mask = engine.retrieve(
        tokens=tokens, mask=None, slot_mapping=slot_mapping, kvcaches=kvcaches
    )
    retrieve_time = time.perf_counter() - t1

    if ret_mask.sum().item() != total_tokens:
        _fail(
            f"Retrieve mask mismatch: expected {total_tokens} true, got {ret_mask.sum().item()}"
        )

    # Compare tensors
    max_abs_diff = 0.0
    for before, after in zip(original, kvcaches, strict=False):
        diff = (before - after).abs().max().item()
        max_abs_diff = max(max_abs_diff, diff)
        if diff > args.tolerance:
            _fail(
                f"Round-trip mismatch (max abs diff {diff:.4e} > tolerance {args.tolerance})"
            )

    total_bytes = sum(t.numel() * t.element_size() for t in kvcaches)
    gb = total_bytes / 1024**3

    print(
        "[HPU-HARNESS][OK] Round-trip success | layers=%d tokens=%d size=%.3f MB "
        "store=%.2f ms retrieve=%.2f ms max_diff=%.3e" % (
            num_layers,
            total_tokens,
            gb * 1024,  # MB
            store_time * 1000,
            retrieve_time * 1000,
            max_abs_diff,
        )
    )


if __name__ == "__main__":  # pragma: no cover
    try:
        main()
    except KeyboardInterrupt:
        print("[HPU-HARNESS] Interrupted.")
        sys.exit(130)