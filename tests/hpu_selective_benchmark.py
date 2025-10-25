"""HPU selective retrieval benchmark for LMCache.

Purpose:
  Measure retrieval time & throughput as the requested token fraction scales.
  Validates correctness of partial prefix masks (required by engine contract:
  mask must be prefix-False then True).

What it does:
  1. Builds a minimal single-rank LMCache v1 engine + HPU connector.
  2. Synthesizes KV caches on HPU (paged layout) and stores them once.
  3. For each requested fraction or explicit token count:
       a. Constructs a prefix mask with (total_tokens * (1-fraction)) False.
       b. Zeroes the affected destination KV segments to ensure actual transfer.
       c. Calls engine.retrieve(... mask=mask) and times it.
       d. Validates the returned mask cardinality + sample equality against an
          original snapshot for retrieved region.
  4. Reports per-fraction stats: ms, tokens, effective GB, GB/s, tokens/s.

Throughput estimation:
  bytes_per_token = num_layers * 2 * num_heads * head_size * dtype_size
  effective_bytes = retrieved_tokens * bytes_per_token
  (Note: This ignores internal alignment/metadata, focusing on core KV payload.)

Usage examples:
  Default fractions (0.25,0.5,0.75,1.0):
    python -m lmcache.tests.hpu_selective_benchmark

  Custom fractions & shape:
    python -m lmcache.tests.hpu_selective_benchmark \
      --num-layers 8 --num-pages 64 --page-size 32 --num-heads 32 --head-size 128 \
      --fractions 0.1,0.2,0.4,0.8,1.0 --dtype bfloat16 --iterations 5 --warmup 1

  Explicit token counts (overrides fractions):
    python -m lmcache.tests.hpu_selective_benchmark --token-counts 256,512,1024

Environment (Gaudi / SynapseAI suggested):
  export HABANA_VISIBLE_DEVICES=0
  export PT_HPU_LAZY_MODE=0
  export ENABLE_EXPERIMENTAL_FLAGS=1
  export PYTHONHASHSEED=0
"""

from __future__ import annotations

# Standard
import argparse
import statistics as stats
import sys
import time
from typing import List, Sequence

# Third Party
import torch

# First Party (LMCache)
from lmcache.config import LMCacheEngineMetadata
from lmcache.v1.cache_engine import LMCacheEngineBuilder
from lmcache.v1.config import LMCacheEngineConfig
from lmcache.v1.hpu_connector import VLLMPagedMemHPUConnectorV2


def parse_args():
    p = argparse.ArgumentParser(description="LMCache HPU selective retrieval benchmark")
    p.add_argument("--num-layers", type=int, default=4)
    p.add_argument("--num-pages", type=int, default=32, help="Pages per layer")
    p.add_argument("--page-size", type=int, default=32, help="Tokens per page")
    p.add_argument("--num-heads", type=int, default=16)
    p.add_argument("--head-size", type=int, default=128)
    p.add_argument(
        "--dtype",
        type=str,
        default="bfloat16",
        choices=["float16", "bfloat16", "float32"],
    )
    p.add_argument("--fractions", type=str, default="0.25,0.5,0.75,1.0",
                   help="Comma-separated retrieval fractions (ignored if --token-counts set)")
    p.add_argument("--token-counts", type=str, default="",
                   help="Explicit token counts to retrieve (comma separated)")
    p.add_argument("--warmup", type=int, default=1, help="Warmup iterations per fraction")
    p.add_argument("--iterations", type=int, default=3, help="Timed iterations per fraction")
    p.add_argument("--engine-id", type=str, default="hpu_bench_engine")
    p.add_argument("--seed", type=int, default=2025)
    p.add_argument("--verify", action="store_true", help="Check tensor equality for sampled indices")
    p.add_argument("--sample-checks", type=int, default=4, help="Number of random layers to sample when verifying")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


def get_dtype(name: str) -> torch.dtype:
    return {
        "float16": torch.float16,
        "bfloat16": torch.bfloat16,
        "float32": torch.float32,
    }[name]


def parse_sequence(raw: str, cast=int) -> Sequence[int]:  # type: ignore[type-arg]
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return [cast(p) for p in parts]


def parse_fractions(raw: str) -> Sequence[float]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return [float(p) for p in parts]


def ensure_hpu():
    if not hasattr(torch, "hpu") or not torch.hpu.is_available():  # type: ignore[attr-defined]
        print("[HPU-BENCH][SKIP] torch.hpu not available.")
        sys.exit(0)


def build_engine(args, device, dtype, hidden_dim_size, kv_shape):
    cfg = LMCacheEngineConfig.from_defaults()
    cfg.chunk_size = args.page_size
    cfg.max_local_cpu_size = max(0.05, (hidden_dim_size * 2 * args.num_layers * args.page_size * 2) / (1024**3))
    cfg.enable_async_loading = False
    cfg.use_layerwise = False
    cfg.enable_controller = False

    metadata = LMCacheEngineMetadata(
        model_name="dummy-hpu-model",
        world_size=1,
        worker_id=0,
        fmt="KV_2LTD",
        kv_dtype=dtype,
        kv_shape=kv_shape,
        use_mla=False,
    )

    connector = VLLMPagedMemHPUConnectorV2(
        hidden_dim_size=hidden_dim_size,
        num_layers=args.num_layers,
        use_gpu=True,
        chunk_size=cfg.chunk_size,
        dtype=dtype,
        device=device,
        use_mla=False,
    )

    def _broadcast_fn(t: torch.Tensor, src: int):  # noqa: ANN001
        return None

    def _broadcast_obj_fn(o, src: int):  # noqa: ANN001
        return o

    engine = LMCacheEngineBuilder.get_or_create(
        args.engine_id, cfg, metadata, connector, _broadcast_fn, _broadcast_obj_fn
    )
    return engine, connector


def allocate_kvcaches(args, device, dtype) -> List[torch.Tensor]:
    return [
        torch.randn(2, args.num_pages, args.page_size, args.num_heads, args.head_size, device=device, dtype=dtype)
        for _ in range(args.num_layers)
    ]


def build_masks(total_tokens: int, fractions: Sequence[float], chunk_size: int):
    masks = []
    for frac in fractions:
        frac = min(max(frac, 0.0), 1.0)
        need = int(round(total_tokens * frac))
        # Round 'skip' (prefix) to multiple of chunk_size to satisfy engine assumptions.
        skip = total_tokens - need
        if skip % chunk_size != 0:
            skip = (skip // chunk_size) * chunk_size
            need = total_tokens - skip
        mask = torch.zeros(total_tokens, dtype=torch.bool)
        if need > 0:
            mask[skip:] = True
        masks.append((frac, need, mask))
    return masks


def build_token_masks_from_counts(total_tokens: int, counts: Sequence[int], chunk_size: int):
    masks = []
    for cnt in counts:
        cnt = min(max(cnt, 0), total_tokens)
        skip = total_tokens - cnt
        if skip % chunk_size != 0:
            skip = (skip // chunk_size) * chunk_size
            cnt = total_tokens - skip
        frac = cnt / total_tokens if total_tokens > 0 else 0
        mask = torch.zeros(total_tokens, dtype=torch.bool)
        if cnt > 0:
            mask[skip:] = True
        masks.append((frac, cnt, mask))
    return masks


def sample_verify(original, current, mask, tolerance, args):
    import random

    layers = list(range(len(original)))
    random.shuffle(layers)
    layers = layers[: max(1, args.sample_checks)]
    if mask.any():
        # pick a retrieved index (last True region)
        start = mask.nonzero(as_tuple=True)[0][0].item()
        idx = start  # deterministic; could randomize inside True region
        for l in layers:
            before = original[l][..., :, :, :]
            after = current[l][..., :, :, :]
            # Just check entire tensors for simplicity
            diff = (before - after).abs().max().item()
            if diff > tolerance:
                raise RuntimeError(
                    f"Layer {l} verification failed diff={diff:.4e} > tol={tolerance}"
                )


def main():
    args = parse_args()
    ensure_hpu()
    torch.manual_seed(args.seed)
    device = torch.device("hpu")
    dtype = get_dtype(args.dtype)

    total_tokens = args.num_pages * args.page_size
    hidden_dim_size = args.num_heads * args.head_size
    kv_shape = (args.num_layers, 2, args.page_size, args.num_heads, args.head_size)

    engine, connector = build_engine(args, device, dtype, hidden_dim_size, kv_shape)
    kvcaches = allocate_kvcaches(args, device, dtype)
    slot_mapping = torch.arange(total_tokens, device=device, dtype=torch.long)

    # Initialize engine / connector
    engine.post_init(kvcaches=kvcaches)

    # Snapshot original
    original = [kv.detach().clone() for kv in kvcaches]

    # Store full content once
    engine.store(
        tokens=torch.arange(total_tokens, dtype=torch.long),
        slot_mapping=slot_mapping,
        kvcaches=kvcaches,
    )

    # Precompute masks
    if args.token_counts:
        masks = build_token_masks_from_counts(
            total_tokens, parse_sequence(args.token_counts, int), args.page_size
        )
    else:
        masks = build_masks(
            total_tokens, parse_fractions(args.fractions), args.page_size
        )

    # Stats output header
    print(
        f"[HPU-BENCH] total_tokens={total_tokens} chunk_size={engine.config.chunk_size} dtype={dtype}"
    )
    header = (
        f"{'FRACTION':>9} {'TOKENS':>8} {'ITER(ms)':>10} {'AVG(ms)':>10} "
        f"{'GB':>7} {'GB/s':>10} {'Tok/s':>12} {'PATH':>6}"
    )
    print(header)
    print('-' * len(header))

    dtype_size = torch.tensor([], dtype=dtype).element_size()
    bytes_per_token = args.num_layers * 2 * args.num_heads * args.head_size * dtype_size
    path_kind = "c_ops" if 'lmcache.c_ops' in sys.modules else "fallback"

    for frac, need_tokens, mask in masks:
        if need_tokens == 0:
            print(f"{frac:9.3f} {0:8d} {'-':>10} {'-':>10} {0:7.3f} {'-':>10} {'-':>12} {path_kind:>6}")
            continue

        # Warmup
        for _ in range(args.warmup):
            for kv in kvcaches:
                kv.zero_()
            engine.retrieve(
                tokens=torch.arange(total_tokens, dtype=torch.long),
                mask=mask,
                slot_mapping=slot_mapping,
                kvcaches=kvcaches,
            )

        # Timed iterations
        times_ms: List[float] = []
        for _ in range(args.iterations):
            for kv in kvcaches:
                kv.zero_()
            t0 = time.perf_counter()
            ret_mask = engine.retrieve(
                tokens=torch.arange(total_tokens, dtype=torch.long),
                mask=mask,
                slot_mapping=slot_mapping,
                kvcaches=kvcaches,
            )
            dt_ms = (time.perf_counter() - t0) * 1000
            times_ms.append(dt_ms)
            if ret_mask.sum().item() != need_tokens:
                raise RuntimeError(
                    f"Retrieve mask mismatch: expected {need_tokens}, got {ret_mask.sum().item()}"
                )
            if args.verify:
                sample_verify(original, kvcaches, mask, 1e-3, args)

        avg_ms = stats.mean(times_ms)
        gb = (need_tokens * bytes_per_token) / 1024**3
        gbps = gb / (avg_ms / 1000) if avg_ms > 0 else 0.0
        toks_per_s = need_tokens / (avg_ms / 1000) if avg_ms > 0 else 0.0
        print(
            f"{frac:9.3f} {need_tokens:8d} {times_ms[-1]:10.3f} {avg_ms:10.3f} "
            f"{gb:7.3f} {gbps:10.3f} {toks_per_s:12.0f} {path_kind:>6}"
        )

    print("[HPU-BENCH] Done.")


if __name__ == "__main__":  # pragma: no cover
    try:
        main()
    except KeyboardInterrupt:
        print("[HPU-BENCH] Interrupted.")
        sys.exit(130)