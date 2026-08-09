# SPDX-License-Identifier: Apache-2.0
"""Resolve uniform L2 request geometry from a model profile YAML file."""

# Future
from __future__ import annotations

# Standard
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

# Third Party
import yaml


_BYTES_PER_KIB = 1024


class GeometryProfileError(ValueError):
    """Raised when a geometry profile cannot describe uniform L2 objects."""


@dataclass(frozen=True)
class L2GeometryProfile:
    """Resolved uniform L2 geometry and source provenance.

    ``bench l2`` drives flat byte buffers, so a source is accepted only when
    every object in a full retrieval burst has the same page size.

    Exactly one provenance form is populated. A YAML profile records
    ``source_path`` and ``sha256`` and leaves ``shape_spec`` empty; an inline
    tensor-group spec has no file, so it records the canonicalized
    ``shape_spec`` instead. Either way the run is reproducible from the
    structured output alone.
    """

    source_path: str
    sha256: str
    shape_spec: str
    model_name: str
    tokens_per_chunk: int
    objects_per_submit: int
    page_size_bytes: int

    @property
    def data_size_kb(self) -> int:
        """Return the page size in the CLI's binary-KiB unit."""
        return self.page_size_bytes // _BYTES_PER_KIB

    @property
    def task_size_bytes(self) -> int:
        """Return the total payload represented by one L2 submit."""
        return self.objects_per_submit * self.page_size_bytes


def resolve_geometry_profile(path: str) -> L2GeometryProfile:
    """Load and validate a uniform-page model geometry profile.

    The supported profile contract is the one used by
    ``docs/design/tools/ipu_traffic_benchmarks/models``: a model name,
    token chunk size, uniform ``page_size_bytes``, and
    ``layers_per_burst``. The resolver validates the declared page size
    against the model's MLA or GQA fields when they are present.

    Args:
        path: Path to the YAML profile.

    Returns:
        A resolved geometry suitable for ``bench l2``.

    Raises:
        GeometryProfileError: If the file is unreadable, malformed, does not
            describe uniform page objects, or cannot be represented in whole
            KiB by the L2 CLI.
    """
    profile_path = Path(path)
    try:
        raw = profile_path.read_bytes()
    except OSError as e:
        raise GeometryProfileError(f"cannot read geometry profile {path!r}: {e}") from e

    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise GeometryProfileError(
            f"invalid YAML geometry profile {path!r}: {e}"
        ) from e

    root = _mapping(document, "profile")
    model = _mapping(root.get("model"), "model")
    architecture = _mapping(root.get("architecture"), "architecture")
    quantization = _mapping(root.get("quantization"), "quantization")
    chunking = _mapping(root.get("chunking"), "chunking")
    page = _mapping(root.get("page"), "page")
    burst = _mapping(root.get("burst"), "burst")

    model_name = _nonempty_string(model.get("name"), "model.name")
    tokens_per_chunk = _positive_int(
        chunking.get("tokens_per_chunk"), "chunking.tokens_per_chunk"
    )
    page_size_bytes = _positive_int(page.get("page_size_bytes"), "page.page_size_bytes")
    objects_per_submit = _positive_int(
        burst.get("layers_per_burst"), "burst.layers_per_burst"
    )
    num_layers = _positive_int(
        architecture.get("num_layers"), "architecture.num_layers"
    )
    dtype_bytes = _positive_int(
        quantization.get("dtype_bytes"), "quantization.dtype_bytes"
    )

    if objects_per_submit != num_layers:
        raise GeometryProfileError(
            "burst.layers_per_burst must equal architecture.num_layers for "
            "the uniform full-burst L2 profile"
        )
    if page_size_bytes % _BYTES_PER_KIB != 0:
        raise GeometryProfileError(
            "page.page_size_bytes must be a whole multiple of 1024 so it can "
            "be represented by --data-size-kb"
        )

    _validate_page_formula(
        architecture=architecture,
        dtype_bytes=dtype_bytes,
        tokens_per_chunk=tokens_per_chunk,
        page_size_bytes=page_size_bytes,
    )

    declared_burst = burst.get("burst_bytes")
    if declared_burst is not None:
        expected_burst = objects_per_submit * page_size_bytes
        if _positive_int(declared_burst, "burst.burst_bytes") != expected_burst:
            raise GeometryProfileError(
                "burst.burst_bytes does not equal layers_per_burst * page_size_bytes"
            )

    return L2GeometryProfile(
        source_path=str(profile_path.resolve()),
        sha256=sha256(raw).hexdigest(),
        shape_spec="",
        model_name=model_name,
        tokens_per_chunk=tokens_per_chunk,
        objects_per_submit=objects_per_submit,
        page_size_bytes=page_size_bytes,
    )


def resolve_inline_shape_spec(spec: str) -> L2GeometryProfile:
    """Resolve an inline tensor-group shape spec into uniform L2 geometry.

    Accepts the same grammar as ``lmcache bench server --kvcache-shape-spec``
    (``(kv_size,NB,BS,NH,HS):dtype:layer_count``, groups separated by ``;``)
    and reduces it to a flat object count and per-object byte size. The
    per-layer page is ``kv_size * BS * NH * HS * element_size``; ``NB`` is the
    paged-KV pool's block count, not part of one page, so it does not
    participate.

    ``bench l2`` submits flat byte buffers, so every declared group must
    resolve to the same page size *and* the same block size. A heterogeneous
    spec is rejected rather than averaged, and every shape field must be
    positive: the shared parser accepts zero and negative dimensions, which
    would otherwise reach the adapter as a zero-byte or negative page.

    Args:
        spec: Inline tensor-group specification.

    Returns:
        A resolved geometry recording the canonicalized spec as its
        provenance, with the file-provenance fields empty.

    Raises:
        GeometryProfileError: If the spec is malformed, declares a
            non-positive dimension or layer count, declares groups whose page
            or block sizes differ, or yields a page that is not a whole number
            of KiB.
    """
    # Deferred: parse_kvcache_shape_spec pulls in the native ops extension,
    # which must not be required merely to load the CLI.
    # First Party
    from lmcache.v1.kv_layer_groups import (
        format_kvcache_shape_spec,
        parse_kvcache_shape_spec,
    )

    try:
        groups = parse_kvcache_shape_spec(spec)
    except ValueError as e:
        raise GeometryProfileError(f"invalid --kvcache-shape-spec {spec!r}: {e}") from e

    page_sizes: set[int] = set()
    block_sizes: set[int] = set()
    objects_per_submit = 0
    for group in groups:
        desc = group.shape_desc
        # The shared parser does no range checking, so a zero or negative
        # dimension would silently produce a zero-byte or negative page that
        # passes the KiB check below. NB does not reach the page size, but a
        # non-positive one is still rejected: it is recorded in ``shape_spec``
        # as replayable provenance, and would not be valid for bench server.
        dimensions = {
            "kv_size": desc.kv_size,
            "NB": desc.nb,
            "BS": desc.bs,
            "NH": desc.nh,
            "HS": desc.hs,
        }
        for field, value in dimensions.items():
            if value <= 0:
                raise GeometryProfileError(
                    f"--kvcache-shape-spec group {field} must be positive; "
                    f"got {value} in {spec!r}"
                )
        layer_count = len(group.layer_indices)
        if layer_count <= 0:
            raise GeometryProfileError(
                "--kvcache-shape-spec group layer count must be positive; "
                f"got {layer_count} in {spec!r}"
            )
        page_sizes.add(desc.kv_size * desc.bs * desc.nh * desc.hs * desc.element_size)
        block_sizes.add(desc.bs)
        objects_per_submit += layer_count

    if len(page_sizes) != 1:
        raise GeometryProfileError(
            "--kvcache-shape-spec must declare a uniform page size for "
            f"bench l2; got {sorted(page_sizes)} bytes across groups"
        )
    # Equal page bytes do not imply equal BS: (256,1,576) and (128,2,576)
    # both give 147456 B. Reporting one arbitrary tokens_per_chunk would
    # misdescribe the run, so require agreement.
    if len(block_sizes) != 1:
        raise GeometryProfileError(
            "--kvcache-shape-spec must declare a uniform block size (BS) for "
            f"bench l2; got {sorted(block_sizes)} tokens across groups"
        )
    page_size_bytes = page_sizes.pop()
    if page_size_bytes % _BYTES_PER_KIB != 0:
        raise GeometryProfileError(
            "the resolved page size must be a whole multiple of 1024 so it "
            "can be represented by --data-size-kb"
        )

    return L2GeometryProfile(
        source_path="",
        sha256="",
        shape_spec=format_kvcache_shape_spec(groups),
        model_name="inline-shape-spec",
        tokens_per_chunk=block_sizes.pop(),
        objects_per_submit=objects_per_submit,
        page_size_bytes=page_size_bytes,
    )


def _mapping(value: Any, field: str) -> dict[str, Any]:
    """Return a string-keyed mapping or raise a profile error."""
    if not isinstance(value, dict):
        raise GeometryProfileError(f"{field} must be a mapping")
    return value


def _nonempty_string(value: Any, field: str) -> str:
    """Return a non-empty string field or raise a profile error."""
    if not isinstance(value, str) or not value.strip():
        raise GeometryProfileError(f"{field} must be a non-empty string")
    return value


def _positive_int(value: Any, field: str) -> int:
    """Return a positive integer field or raise a profile error."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise GeometryProfileError(f"{field} must be a positive integer")
    return value


def _mla_elements_per_token(architecture: dict[str, Any]) -> int:
    """Return cached elements per token for an MLA profile.

    MLA caches one compressed latent per token per layer, shared across all
    attention heads, so the head count never multiplies in. The element count
    is ``kv_lora_rank + qk_rope_head_dim``. It is derived from those two
    components when present, and cross-checked against the redundant
    ``cached_elems_per_token`` total when the profile also declares it.

    Args:
        architecture: The profile's ``architecture`` mapping.

    Returns:
        Cached elements per token per layer.

    Raises:
        GeometryProfileError: If neither the components nor the total are
            present, or if a declared total contradicts the components.
    """
    lora_rank = architecture.get("kv_lora_rank")
    rope_dim = architecture.get("qk_rope_head_dim")
    declared_total = architecture.get("cached_elems_per_token")

    if lora_rank is None and rope_dim is None:
        if declared_total is None:
            raise GeometryProfileError(
                "an mla profile must declare kv_lora_rank and "
                "qk_rope_head_dim, or architecture.cached_elems_per_token"
            )
        return _positive_int(declared_total, "architecture.cached_elems_per_token")

    derived = _positive_int(lora_rank, "architecture.kv_lora_rank") + _positive_int(
        rope_dim, "architecture.qk_rope_head_dim"
    )
    if declared_total is not None:
        total = _positive_int(declared_total, "architecture.cached_elems_per_token")
        if total != derived:
            raise GeometryProfileError(
                "architecture.cached_elems_per_token does not equal "
                "kv_lora_rank + qk_rope_head_dim"
            )
    return derived


def _validate_page_formula(
    architecture: dict[str, Any],
    dtype_bytes: int,
    tokens_per_chunk: int,
    page_size_bytes: int,
) -> None:
    """Check the declared page against the profile's MLA or GQA fields."""
    if architecture.get("attention") == "mla":
        elements_per_token = _mla_elements_per_token(architecture)
    else:
        elements_per_token = (
            _positive_int(architecture.get("kv_size"), "architecture.kv_size")
            * _positive_int(
                architecture.get("num_kv_heads"), "architecture.num_kv_heads"
            )
            * _positive_int(architecture.get("head_size"), "architecture.head_size")
        )

    expected = elements_per_token * dtype_bytes * tokens_per_chunk
    if page_size_bytes != expected:
        raise GeometryProfileError(
            "page.page_size_bytes does not match the declared architecture, "
            "dtype_bytes, and tokens_per_chunk"
        )
