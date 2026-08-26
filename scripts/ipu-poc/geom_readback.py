#!/usr/bin/env python3
"""Byte-verify stored corpus objects against the bench's deterministic fill.

Why this exists: ``bench l2 --only store`` / ``--only load`` cannot byte-verify
(only a combined store+load run with ``--no-skip-verify`` can), and the
integrity gate verifies a throwaway namespace rather than the corpus the timed
cells read. So a corrupt, truncated, or mis-offset payload in the real corpus
would still yield completion bitmaps and RDMA counter agreement -- an ACCEPTED
cell over bad data. This tool re-derives what the store pass wrote and compares
it byte-for-byte.

The fill is deterministic and documented in ``data.py``: ``make_memory_objects``
fills object *i* of a submit batch with the single byte ``(i + fill_offset) &
0xFF``, and the store path uses ``fill_offset=0``. Key indices within a submit
are contiguous starting at a multiple of ``objects_per_submit``, so the object
with key index ``idx`` holds ``idx % objects_per_submit`` repeated for its whole
page.

Scope: this is a BOUNDED check of selected submit slots, not a full-corpus
verification. It catches truncation, wrong page size, wrong geometry, and
cross-key mis-offset -- the failure modes ``--only`` hides. It does not license
the claim that every object was verified.

Usage:
    geom_readback.py --base-path DIR --key-prefix PREFIX --profile YAML
                     --submits N [--slots 0,mid,last]

``--key-prefix`` takes the same ``PREFIX`` the store run used, not the derived
key namespace it printed. This verifies corpora written by
``run_model_geometry.sh``, whose keys are scoped by profile SHA. A corpus
written by invoking ``bench l2`` directly is not profile-scoped and is out of
scope here: nothing ties it to the geometry it was stored with.

Exits nonzero on the first mismatch.
"""

# Future
from __future__ import annotations

# Standard
import argparse
import hashlib
import sys
from pathlib import Path

# First Party
from lmcache.cli.commands.bench.l2_adapter_bench.geometry import (
    GeometryProfileError,
    resolve_geometry_profile,
)


def _resolve_slots(spec: str, submits: int) -> list[int]:
    """Turn a slot spec into concrete submit-slot indices.

    Args:
        spec: Comma-separated list of ``0``, ``mid``, ``last``, or integers.
        submits: Total submit slots in the corpus.

    Returns:
        Sorted, de-duplicated slot indices.

    Raises:
        ValueError: If a token is unrecognized or out of range.
    """
    slots: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if token == "mid":
            slots.add(submits // 2)
        elif token == "last":
            slots.add(submits - 1)
        else:
            value = int(token)
            if not 0 <= value < submits:
                raise ValueError(f"slot {value} outside [0, {submits})")
            slots.add(value)
    return sorted(slots)


def _profile_namespace(key_prefix: str, profile: Path) -> str:
    """Derive the key namespace ``run_model_geometry.sh`` stores under.

    That helper scopes the bench ``--key-prefix`` by the profile's SHA-256, so
    deriving the same value here keeps ``PREFIX`` the only thing an operator
    carries between the two tools. There is deliberately no fallback to the
    unscoped namespace: two profiles with equal page sizes produce identical
    fill bytes over their shared key range, so an unscoped lookup lets one
    profile report another's corpus as verified.

    Args:
        key_prefix: The ``PREFIX`` value used for the store run.
        profile: Path to the geometry profile YAML.

    Returns:
        The key namespace, i.e. ``f"{prefix}-{sha12}-bench-model"``.
    """
    sha12 = hashlib.sha256(profile.read_bytes()).hexdigest()[:12]
    return f"{key_prefix}-{sha12}-bench-model"


def _object_path(base: Path, namespace: str, idx: int) -> Path:
    """Locate the on-disk object for key index *idx*.

    The adapter's filename encodes the chunk hash, which
    ``make_object_keys`` derives as ``idx.to_bytes(16, "big")``. Matching on
    that suffix rather than reconstructing every field keeps this robust to
    the adapter's other filename components.

    Args:
        base: Adapter ``base_path``.
        namespace: Key namespace as returned by ``_resolve_namespace``.
        idx: Key index.

    Returns:
        Path to the object.

    Raises:
        FileNotFoundError: If no object matches, or more than one does.
    """
    suffix = idx.to_bytes(16, "big").hex()
    matches = sorted(base.glob(f"{namespace}@*@{suffix}.data"))
    if not matches:
        raise FileNotFoundError(
            f"no object for key index {idx} ({suffix}) under {namespace}"
        )
    if len(matches) > 1:
        raise FileNotFoundError(
            f"key index {idx} matched {len(matches)} objects: {matches}"
        )
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-path", required=True)
    parser.add_argument("--key-prefix", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--submits", type=int, required=True)
    parser.add_argument("--slots", default="0,mid,last")
    args = parser.parse_args()

    try:
        geometry = resolve_geometry_profile(args.profile)
    except GeometryProfileError as e:
        print(f"ABORT: {e}")
        return 2
    if args.submits <= 0:
        print(f"ABORT: --submits must be positive, got {args.submits}")
        return 2

    base = Path(args.base_path)
    namespace = _profile_namespace(args.key_prefix, Path(args.profile))
    per_submit = geometry.objects_per_submit
    page = geometry.page_size_bytes

    try:
        slots = _resolve_slots(args.slots, args.submits)
    except ValueError as e:
        print(f"ABORT: {e}")
        return 2

    print(
        f"readback: prefix={args.key_prefix} namespace={namespace} page={page} B "
        f"objects/submit={per_submit} slots={slots}"
    )

    checked = 0
    for slot in slots:
        for i in range(per_submit):
            idx = slot * per_submit + i
            try:
                path = _object_path(base, namespace, idx)
            except FileNotFoundError as e:
                print(f"FAIL: {e}")
                return 1
            size = path.stat().st_size
            if size != page:
                print(f"FAIL: {path.name} is {size} B, expected {page} B")
                return 1
            expected = i & 0xFF
            data = path.read_bytes()
            # A single distinct byte value covers the whole page, so any
            # deviation -- a partial write, a neighbouring key's pattern, or
            # stale blocks -- shows up as a different value at some offset.
            bad = next((o for o, b in enumerate(data) if b != expected), -1)
            if bad >= 0:
                print(
                    f"FAIL: {path.name} byte {bad} is {data[bad]}, "
                    f"expected {expected} (slot {slot}, object {i})"
                )
                return 1
            checked += 1

    total_bytes = checked * page
    print(
        f"readback OK: {checked} objects ({total_bytes / 2**20:.1f} MiB) "
        f"match the deterministic store fill"
    )
    print(
        f"  BOUNDED: {len(slots)} of {args.submits} submit slots verified; "
        "this is not a full-corpus verification"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
