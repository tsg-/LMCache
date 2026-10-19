# SPDX-License-Identifier: Apache-2.0
"""Resolve L2 request geometry from a model profile YAML file.

Two profile forms are supported and a file must use exactly one.

The legacy *page-burst* form describes a single uniform page repeated once
per layer -- the shape ``bench l2`` could drive before object groups
existed. The *object-group* form describes what a production L2 submit
actually carries: one object per ``(chunk, object group, kv rank)``, where
each object packs one or more components whose sizes need not agree. Both
resolve to the same internal shape, an :class:`L2SubmitGeometry` holding an
ordered descriptor per object, so the runners never branch on which form a
run came from.
"""

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

PROFILE_MODE_LEGACY = "legacy_page_burst"
PROFILE_MODE_OBJECT_GROUP = "object_group"

#: Shape of the submit a profile describes. ``store`` is a write of freshly
#: computed chunks, ``lookup_load`` is a read of chunks a lookup has already
#: located, and ``shared_envelope`` is a transfer whose objects are shared
#: across ranks rather than owned by one.
TASK_ARCHETYPES = ("store", "lookup_load", "shared_envelope")

_ATTENTION_KINDS = ("gqa", "mla", "opaque")

# ``burst`` is deliberately absent from both tuples: it carries the same
# optional redundant cross-check in either form, so its presence says
# nothing about which form a file uses.
_LEGACY_SECTIONS = ("architecture", "quantization", "chunking", "page")
_OBJECT_GROUP_SECTIONS = ("runtime", "object_groups")


class GeometryProfileError(ValueError):
    """Raised when a geometry profile cannot describe L2 objects."""


@dataclass(frozen=True)
class L2GeometryProfile:
    """Resolved uniform L2 geometry and source provenance.

    ``bench l2`` drives flat byte buffers, so a source is accepted only when
    every object in a full retrieval burst has the same page size.

    Exactly one provenance form is populated, and the two differ in strength.
    An inline tensor-group spec records the canonicalized ``shape_spec``, which
    fully determines the geometry, so such a run is reproducible from the
    structured output alone. A YAML profile records ``source_path`` and
    ``sha256`` instead -- not the file's bytes -- so the output identifies the
    profile and can verify a candidate copy of it, but does not carry it.
    Reproducing that run needs the file as well.
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


@dataclass(frozen=True)
class L2GeometryComponent:
    """One kernel group packed into an L2 object.

    A component is the unit a profile can size from first principles: a
    contiguous run of layers sharing an attention kind, dtype, and block
    geometry. Several of them are concatenated into one object when the
    engine registers them in the same object group, which is why the
    declaration order here is significant -- it mirrors the packed
    ``MemoryLayoutDesc`` order.

    ``tokens_per_block``, ``slots_per_block``, ``transfer_tokens_per_chunk``,
    and ``slots_per_object`` are zero for an ``opaque`` component: a
    recurrent or linear-attention state has no per-token layout, so a block
    geometry would be fiction. Such a component is sized from its declared
    ``component_size_bytes`` alone.
    """

    name: str
    role: str
    attention: str
    cache_owning_layers: int
    dtype: str
    dtype_bytes: int
    tokens_per_block: int
    slots_per_block: int
    transfer_tokens_per_chunk: int
    slots_per_object: int
    component_size_bytes: int


@dataclass(frozen=True)
class L2ObjectGroupGeometry:
    """One object group: the components that share a single L2 object.

    ``sw_size_chunks`` is the cross-chunk attention window that put these
    components in the same group -- ``-1`` for full attention, ``0`` for a
    state that does not span chunks, and a positive count for a sliding
    window. It is recorded rather than derived because grouping is an engine
    decision the benchmark reproduces, not one it makes.
    """

    object_group_id: int
    name: str
    sw_size_chunks: int
    components: tuple[L2GeometryComponent, ...]
    object_size_bytes: int


@dataclass(frozen=True)
class L2ObjectDescriptor:
    """One L2 object in a submit, with the key coordinates that identify it."""

    chunk_ordinal: int
    object_group_id: int
    kv_rank_ordinal: int
    size_bytes: int


@dataclass(frozen=True)
class L2SubmitGeometry:
    """The complete object vector one logical L2 submit carries.

    This is the single internal shape every geometry source resolves to:
    raw ``--num-keys``/``--data-size-kb``, a legacy page-burst profile, an
    inline shape spec, and an object-group profile all produce one of these,
    so buffer sizing, key construction, and byte accounting have exactly one
    form to handle.

    ``objects`` is ordered ``chunk -> object group -> kv rank``, matching the
    order the engine issues keys in, so an object's position in this vector
    is also its position in the flat L1 buffer.

    ``page_size_bytes`` and ``tokens_per_chunk`` are legacy accessors. A
    legacy source fills both; an object-group source leaves ``page_size_bytes``
    at zero, because a heterogeneous submit has no single page size, and
    reports its chunk size through ``tokens_per_chunk``.
    """

    profile_mode: str
    source_path: str
    sha256: str
    shape_spec: str
    model_name: str
    task_archetype: str
    tokens_per_chunk: int
    chunks_per_submit: int
    kv_ranks_per_chunk: int
    separate_object_groups: bool
    full_sw_kv: bool
    object_groups: tuple[L2ObjectGroupGeometry, ...]
    objects: tuple[L2ObjectDescriptor, ...]
    page_size_bytes: int

    @property
    def objects_per_submit(self) -> int:
        """Return the number of L2 objects in one logical submit."""
        return len(self.objects)

    @property
    def object_sizes_bytes(self) -> tuple[int, ...]:
        """Return each object's size in submit order."""
        return tuple(descriptor.size_bytes for descriptor in self.objects)

    @property
    def task_size_bytes(self) -> int:
        """Return the total payload one logical submit transfers."""
        return sum(descriptor.size_bytes for descriptor in self.objects)

    @property
    def is_uniform(self) -> bool:
        """Return whether every object in the submit is the same size."""
        return len({descriptor.size_bytes for descriptor in self.objects}) <= 1

    @property
    def data_size_kb(self) -> int:
        """Return the legacy page size in the CLI's binary-KiB unit."""
        return self.page_size_bytes // _BYTES_PER_KIB


def submit_geometry_from_uniform(profile: L2GeometryProfile) -> L2SubmitGeometry:
    """Lift a resolved uniform profile into the common submit shape.

    The uniform page burst becomes one synthetic object group whose objects
    are indexed by chunk ordinal, so the flat page-burst identity survives:
    ``objects_per_submit`` and the per-object size are unchanged, and every
    object still lands in the same buffer slot it did before object groups
    existed.

    Components are left empty. A uniform profile is fully described by its
    page size and chunk size, and inventing a component breakdown for it
    would put unverified layer attribution into the structured output.

    Args:
        profile: Geometry resolved from a legacy profile, an inline shape
            spec, or raw CLI arguments.

    Returns:
        The equivalent :class:`L2SubmitGeometry`.
    """
    group = L2ObjectGroupGeometry(
        object_group_id=0,
        name="uniform_page_burst",
        sw_size_chunks=-1,
        components=(),
        object_size_bytes=profile.page_size_bytes,
    )
    objects = tuple(
        L2ObjectDescriptor(
            chunk_ordinal=index,
            object_group_id=0,
            kv_rank_ordinal=0,
            size_bytes=profile.page_size_bytes,
        )
        for index in range(profile.objects_per_submit)
    )
    return L2SubmitGeometry(
        profile_mode=PROFILE_MODE_LEGACY,
        source_path=profile.source_path,
        sha256=profile.sha256,
        shape_spec=profile.shape_spec,
        model_name=profile.model_name,
        # A page-burst profile declares no archetype. Reporting one would
        # be an invention, so the field stays empty and consumers omit it.
        task_archetype="",
        tokens_per_chunk=profile.tokens_per_chunk,
        chunks_per_submit=profile.objects_per_submit,
        kv_ranks_per_chunk=1,
        separate_object_groups=False,
        full_sw_kv=False,
        object_groups=(group,),
        objects=objects,
        page_size_bytes=profile.page_size_bytes,
    )


def raw_submit_geometry(num_keys: int, page_size_bytes: int) -> L2SubmitGeometry:
    """Build submit geometry for raw ``--num-keys``/``--data-size-kb`` runs.

    Args:
        num_keys: Objects per logical submit.
        page_size_bytes: Size of every object.

    Returns:
        A uniform :class:`L2SubmitGeometry` with no profile provenance.
    """
    return submit_geometry_from_uniform(
        L2GeometryProfile(
            source_path="",
            sha256="",
            shape_spec="",
            model_name="bench-model",
            tokens_per_chunk=0,
            objects_per_submit=num_keys,
            page_size_bytes=page_size_bytes,
        )
    )


def resolve_submit_geometry(path: str) -> L2SubmitGeometry:
    """Resolve either profile form from *path* into the common submit shape.

    The file is read once and its top-level sections decide the form. A file
    carrying sections from both forms is rejected rather than resolved under
    whichever the reader checked first: silently picking one would let a
    half-migrated profile benchmark a geometry nobody declared.

    Args:
        path: Path to the YAML profile.

    Returns:
        The resolved submit geometry.

    Raises:
        GeometryProfileError: If the file is unreadable or malformed, mixes
            the two geometry forms, or declares object groups that do not
            describe a consistent submit.
    """
    raw, document, profile_path = _read_profile(path)
    root = _mapping(document, "profile")
    if _object_group_form(root):
        return _resolve_object_group_profile(raw, root, profile_path)
    return submit_geometry_from_uniform(
        _resolve_legacy_profile(raw, root, profile_path)
    )


def resolve_geometry_profile(path: str) -> L2GeometryProfile:
    """Load and validate a uniform-page model geometry profile.

    The supported profile contract is a model name, token chunk size,
    uniform ``page_size_bytes``, and ``layers_per_burst``. The resolver validates
    the declared page size against the model's MLA or GQA fields when they
    are present.

    Args:
        path: Path to the YAML profile.

    Returns:
        A resolved geometry suitable for ``bench l2``.

    Raises:
        GeometryProfileError: If the file is unreadable, malformed, declares
            object groups, does not describe uniform page objects, or cannot
            be represented in whole KiB by the L2 CLI.
    """
    raw, document, profile_path = _read_profile(path)
    root = _mapping(document, "profile")
    if _object_group_form(root):
        raise GeometryProfileError(
            f"geometry profile {path!r} declares object groups, which cannot "
            f"be reduced to a single uniform page size; use "
            f"resolve_submit_geometry instead"
        )
    return _resolve_legacy_profile(raw, root, profile_path)


def _read_profile(path: str) -> tuple[bytes, Any, Path]:
    """Read and parse a profile once, returning its bytes, tree, and path."""
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

    return raw, document, profile_path


def _object_group_form(root: dict[str, Any]) -> bool:
    """Return whether *root* uses the object-group form, rejecting a mix."""
    has_object_groups = any(section in root for section in _OBJECT_GROUP_SECTIONS)
    has_legacy = any(section in root for section in _LEGACY_SECTIONS)
    if has_object_groups and has_legacy:
        raise GeometryProfileError(
            "a geometry profile must use exactly one geometry form: either "
            f"the legacy {', '.join(_LEGACY_SECTIONS)} sections or "
            f"{' plus '.join(_OBJECT_GROUP_SECTIONS)}"
        )
    return has_object_groups


def _resolve_legacy_profile(
    raw: bytes, root: dict[str, Any], profile_path: Path
) -> L2GeometryProfile:
    """Resolve the legacy uniform page-burst sections of *root*."""
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


def _resolve_object_group_profile(
    raw: bytes, root: dict[str, Any], profile_path: Path
) -> L2SubmitGeometry:
    """Resolve the ``runtime`` plus ``object_groups`` sections of *root*."""
    model = _mapping(root.get("model"), "model")
    runtime = _mapping(root.get("runtime"), "runtime")

    model_name = _nonempty_string(model.get("name"), "model.name")
    tokens_per_chunk = _positive_int(
        runtime.get("lmcache_tokens_per_chunk"), "runtime.lmcache_tokens_per_chunk"
    )
    task_archetype = _nonempty_string(
        runtime.get("task_archetype"), "runtime.task_archetype"
    )
    if task_archetype not in TASK_ARCHETYPES:
        raise GeometryProfileError(
            f"runtime.task_archetype must be one of "
            f"{', '.join(TASK_ARCHETYPES)}; got {task_archetype!r}"
        )
    chunks_per_submit = _positive_int(
        runtime.get("chunks_per_submit"), "runtime.chunks_per_submit"
    )
    kv_ranks_per_chunk = _positive_int(
        runtime.get("kv_ranks_per_chunk"), "runtime.kv_ranks_per_chunk"
    )
    separate_object_groups = _bool_field(
        runtime.get("separate_object_groups"), "runtime.separate_object_groups"
    )
    full_sw_kv = _bool_field(runtime.get("full_sw_kv"), "runtime.full_sw_kv")

    groups = _resolve_object_groups(root.get("object_groups"), tokens_per_chunk)

    objects = tuple(
        L2ObjectDescriptor(
            chunk_ordinal=chunk,
            object_group_id=group.object_group_id,
            kv_rank_ordinal=kv_rank,
            size_bytes=group.object_size_bytes,
        )
        for chunk in range(chunks_per_submit)
        for group in groups
        for kv_rank in range(kv_ranks_per_chunk)
    )

    geometry = L2SubmitGeometry(
        profile_mode=PROFILE_MODE_OBJECT_GROUP,
        source_path=str(profile_path.resolve()),
        sha256=sha256(raw).hexdigest(),
        shape_spec="",
        model_name=model_name,
        task_archetype=task_archetype,
        tokens_per_chunk=tokens_per_chunk,
        chunks_per_submit=chunks_per_submit,
        kv_ranks_per_chunk=kv_ranks_per_chunk,
        separate_object_groups=separate_object_groups,
        full_sw_kv=full_sw_kv,
        object_groups=groups,
        objects=objects,
        page_size_bytes=0,
    )
    _validate_burst_cross_check(root.get("burst"), geometry)
    return geometry


def _resolve_object_groups(
    value: Any, tokens_per_chunk: int
) -> tuple[L2ObjectGroupGeometry, ...]:
    """Resolve and validate the ordered ``object_groups`` list."""
    if not isinstance(value, list) or not value:
        raise GeometryProfileError("object_groups must be a non-empty list")

    resolved = [
        _resolve_object_group(entry, index, tokens_per_chunk)
        for index, entry in enumerate(value)
    ]

    ids = [group.object_group_id for group in resolved]
    if len(set(ids)) != len(ids):
        raise GeometryProfileError(f"duplicate object_group_id in object_groups: {ids}")
    # Object-group IDs index the engine's group table, so a sparse or
    # non-zero-based set would not correspond to any real registration.
    if sorted(ids) != list(range(len(ids))):
        raise GeometryProfileError(
            f"object_group_id values must be dense and start at zero; got {ids}"
        )
    names = [group.name for group in resolved]
    if len(set(names)) != len(names):
        raise GeometryProfileError(
            f"duplicate object group name in object_groups: {names}"
        )

    # Submit order is numeric ID order, not declaration order.
    return tuple(sorted(resolved, key=lambda group: group.object_group_id))


def _resolve_object_group(
    entry: Any, index: int, tokens_per_chunk: int
) -> L2ObjectGroupGeometry:
    """Resolve one object group and cross-check its declared object size."""
    group = _mapping(entry, f"object_groups[{index}]")
    field = f"object_groups[{index}]"

    object_group_id = _nonnegative_int(
        group.get("object_group_id"), f"{field}.object_group_id"
    )
    name = _nonempty_string(group.get("name"), f"{field}.name")
    sw_size_chunks = _int_field(group.get("sw_size_chunks"), f"{field}.sw_size_chunks")

    raw_components = group.get("components")
    if not isinstance(raw_components, list) or not raw_components:
        raise GeometryProfileError(f"{field}.components must be a non-empty list")
    components = tuple(
        _resolve_component(
            component, f"{field}.components[{position}]", tokens_per_chunk
        )
        for position, component in enumerate(raw_components)
    )

    component_names = [component.name for component in components]
    if len(set(component_names)) != len(component_names):
        raise GeometryProfileError(
            f"duplicate component name in {field}.components: {component_names}"
        )

    object_size_bytes = _positive_int(
        group.get("object_size_bytes"), f"{field}.object_size_bytes"
    )
    packed = sum(component.component_size_bytes for component in components)
    if object_size_bytes != packed:
        raise GeometryProfileError(
            f"{field}.object_size_bytes ({object_size_bytes}) does not equal "
            f"the sum of its component sizes ({packed})"
        )

    return L2ObjectGroupGeometry(
        object_group_id=object_group_id,
        name=name,
        sw_size_chunks=sw_size_chunks,
        components=components,
        object_size_bytes=object_size_bytes,
    )


def _resolve_component(
    entry: Any, field: str, tokens_per_chunk: int
) -> L2GeometryComponent:
    """Resolve one component and cross-check its declared byte size."""
    component = _mapping(entry, field)
    name = _nonempty_string(component.get("name"), f"{field}.name")
    role = _nonempty_string(component.get("role"), f"{field}.role")
    cache_owning_layers = _positive_int(
        component.get("cache_owning_layers"), f"{field}.cache_owning_layers"
    )
    architecture = _mapping(component.get("architecture"), f"{field}.architecture")
    quantization = _mapping(component.get("quantization"), f"{field}.quantization")
    attention = _nonempty_string(
        architecture.get("attention"), f"{field}.architecture.attention"
    )
    if attention not in _ATTENTION_KINDS:
        raise GeometryProfileError(
            f"{field}.architecture.attention must be one of "
            f"{', '.join(_ATTENTION_KINDS)}; got {attention!r}"
        )
    dtype = _nonempty_string(quantization.get("dtype"), f"{field}.quantization.dtype")
    dtype_bytes = _positive_int(
        quantization.get("dtype_bytes"), f"{field}.quantization.dtype_bytes"
    )
    component_size_bytes = _positive_int(
        component.get("component_size_bytes"), f"{field}.component_size_bytes"
    )

    if attention == "opaque":
        if "block_geometry" in component:
            raise GeometryProfileError(
                f"{field} is opaque, so it must not declare block_geometry: "
                f"its size comes from component_size_bytes alone"
            )
        return L2GeometryComponent(
            name=name,
            role=role,
            attention=attention,
            cache_owning_layers=cache_owning_layers,
            dtype=dtype,
            dtype_bytes=dtype_bytes,
            tokens_per_block=0,
            slots_per_block=0,
            transfer_tokens_per_chunk=0,
            slots_per_object=0,
            component_size_bytes=component_size_bytes,
        )

    block = _mapping(component.get("block_geometry"), f"{field}.block_geometry")
    tokens_per_block = _positive_int(
        block.get("tokens_per_block"), f"{field}.block_geometry.tokens_per_block"
    )
    slots_per_block = _positive_int(
        block.get("slots_per_block"), f"{field}.block_geometry.slots_per_block"
    )
    transfer_tokens_per_chunk = _positive_int(
        block.get("transfer_tokens_per_chunk"),
        f"{field}.block_geometry.transfer_tokens_per_chunk",
    )
    # A sliding-window component may move less than a full chunk, but never
    # more: the chunk is the largest unit LMCache addresses.
    if transfer_tokens_per_chunk > tokens_per_chunk:
        raise GeometryProfileError(
            f"{field}.block_geometry.transfer_tokens_per_chunk "
            f"({transfer_tokens_per_chunk}) exceeds "
            f"runtime.lmcache_tokens_per_chunk ({tokens_per_chunk})"
        )
    scaled = transfer_tokens_per_chunk * slots_per_block
    if scaled % tokens_per_block != 0:
        raise GeometryProfileError(
            f"{field} transfer does not cover an integral number of slots: "
            f"transfer_tokens_per_chunk * slots_per_block ({scaled}) is not "
            f"divisible by tokens_per_block ({tokens_per_block})"
        )
    slots_per_object = scaled // tokens_per_block

    if attention == "mla":
        elements_per_token = _mla_elements_per_token(architecture)
    else:
        elements_per_token = (
            _positive_int(architecture.get("kv_size"), f"{field}.architecture.kv_size")
            * _positive_int(
                architecture.get("num_kv_heads"),
                f"{field}.architecture.num_kv_heads",
            )
            * _positive_int(
                architecture.get("head_size"), f"{field}.architecture.head_size"
            )
        )
    expected = cache_owning_layers * elements_per_token * dtype_bytes * slots_per_object
    if component_size_bytes != expected:
        raise GeometryProfileError(
            f"{field}.component_size_bytes ({component_size_bytes}) does not "
            f"match the declared architecture, dtype, and block geometry "
            f"({expected})"
        )

    return L2GeometryComponent(
        name=name,
        role=role,
        attention=attention,
        cache_owning_layers=cache_owning_layers,
        dtype=dtype,
        dtype_bytes=dtype_bytes,
        tokens_per_block=tokens_per_block,
        slots_per_block=slots_per_block,
        transfer_tokens_per_chunk=transfer_tokens_per_chunk,
        slots_per_object=slots_per_object,
        component_size_bytes=component_size_bytes,
    )


def _validate_burst_cross_check(value: Any, geometry: L2SubmitGeometry) -> None:
    """Check the optional redundant ``burst`` totals against *geometry*."""
    if value is None:
        return
    burst = _mapping(value, "burst")

    declared_objects = burst.get("objects_per_submit")
    if declared_objects is not None:
        expected = geometry.objects_per_submit
        if _positive_int(declared_objects, "burst.objects_per_submit") != expected:
            raise GeometryProfileError(
                f"burst.objects_per_submit ({declared_objects}) does not equal "
                f"chunks_per_submit * object groups * kv_ranks_per_chunk "
                f"({expected})"
            )

    declared_bytes = burst.get("burst_bytes")
    if declared_bytes is not None:
        expected_bytes = geometry.task_size_bytes
        if _positive_int(declared_bytes, "burst.burst_bytes") != expected_bytes:
            raise GeometryProfileError(
                f"burst.burst_bytes ({declared_bytes}) does not equal the sum "
                f"of the submit's object sizes ({expected_bytes})"
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


def _nonnegative_int(value: Any, field: str) -> int:
    """Return a non-negative integer field or raise a profile error."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise GeometryProfileError(f"{field} must be a non-negative integer")
    return value


def _int_field(value: Any, field: str) -> int:
    """Return an integer field of any sign or raise a profile error."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise GeometryProfileError(f"{field} must be an integer")
    return value


def _bool_field(value: Any, field: str) -> bool:
    """Return a boolean field or raise a profile error.

    YAML's ``1``/``0`` are rejected rather than coerced: a profile that
    writes them almost certainly means a count, and silently reading it as a
    flag would change the resolved geometry without complaint.
    """
    if not isinstance(value, bool):
        raise GeometryProfileError(f"{field} must be true or false")
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
