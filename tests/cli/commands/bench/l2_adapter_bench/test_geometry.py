# SPDX-License-Identifier: Apache-2.0
"""Tests for object-group submit geometry resolution."""

# Standard
from pathlib import Path
import textwrap

# Third Party
import pytest

# First Party
from lmcache.cli.commands.bench.l2_adapter_bench.geometry import (
    PROFILE_MODE_LEGACY,
    PROFILE_MODE_OBJECT_GROUP,
    GeometryProfileError,
    raw_submit_geometry,
    resolve_geometry_profile,
    resolve_submit_geometry,
    submit_geometry_from_uniform,
)

# Two object groups with deliberately different object sizes, so the
# resolver, the descriptor vector, and the byte totals are all exercised
# against heterogeneous objects rather than a uniform burst that happens
# to be split in two.
#
# full_attention: 4 layers * kv_size 2 * 2 heads * 128 head * 2 B * 256
#                 slots = 1048576 B
# sliding_window: 2 layers * kv_size 2 * 2 heads * 128 head * 2 B * 128
#                 slots = 262144 B
_HYBRID_PROFILE = textwrap.dedent(
    """
    model:
      name: example/hybrid

    runtime:
      lmcache_tokens_per_chunk: 256
      task_archetype: lookup_load
      chunks_per_submit: 2
      kv_ranks_per_chunk: 2
      separate_object_groups: true
      full_sw_kv: false

    object_groups:
      - object_group_id: 0
        name: full_attention
        sw_size_chunks: -1
        components:
          - name: main_kv
            role: key_value
            cache_owning_layers: 4
            architecture:
              attention: gqa
              kv_size: 2
              num_kv_heads: 2
              head_size: 128
            quantization:
              dtype: bfloat16
              dtype_bytes: 2
            block_geometry:
              tokens_per_block: 128
              slots_per_block: 128
              transfer_tokens_per_chunk: 256
            component_size_bytes: 1048576
        object_size_bytes: 1048576

      - object_group_id: 1
        name: sliding_window
        sw_size_chunks: 1
        components:
          - name: sw_kv
            role: key_value
            cache_owning_layers: 2
            architecture:
              attention: gqa
              kv_size: 2
              num_kv_heads: 2
              head_size: 128
            quantization:
              dtype: bfloat16
              dtype_bytes: 2
            block_geometry:
              tokens_per_block: 128
              slots_per_block: 128
              transfer_tokens_per_chunk: 128
            component_size_bytes: 262144
        object_size_bytes: 262144

    burst:
      objects_per_submit: 8
      burst_bytes: 5242880
    """
)

_LEGACY_PROFILE = textwrap.dedent(
    """
    model:
      name: deepseek-ai/DeepSeek-V3
    architecture:
      num_layers: 61
      attention: mla
      kv_lora_rank: 512
      qk_rope_head_dim: 64
    quantization:
      dtype: float8_e4m3fn
      dtype_bytes: 1
    chunking:
      tokens_per_chunk: 256
    page:
      page_size_bytes: 147456
    burst:
      layers_per_burst: 61
      burst_bytes: 8994816
    """
)


def _write(tmp_path: Path, text: str, name: str = "profile.yaml") -> str:
    path = tmp_path / name
    path.write_text(text)
    return str(path)


def _mutate(text: str, old: str, new: str) -> str:
    if old not in text:
        raise AssertionError(f"fixture does not contain {old!r}")
    return text.replace(old, new, 1)


# ---------------------------------------------------------------------------
# Object-group resolution
# ---------------------------------------------------------------------------


def test_object_group_profile_resolves_descriptor_vector(tmp_path):
    """The descriptor vector is ordered chunk -> object group -> kv rank."""
    geometry = resolve_submit_geometry(_write(tmp_path, _HYBRID_PROFILE))

    assert geometry.profile_mode == PROFILE_MODE_OBJECT_GROUP
    assert geometry.model_name == "example/hybrid"
    assert geometry.task_archetype == "lookup_load"
    assert geometry.chunks_per_submit == 2
    assert geometry.kv_ranks_per_chunk == 2
    assert geometry.separate_object_groups is True
    assert geometry.full_sw_kv is False
    assert geometry.tokens_per_chunk == 256

    # 2 chunks * 2 object groups * 2 KV ranks.
    assert geometry.objects_per_submit == 8
    assert [
        (d.chunk_ordinal, d.object_group_id, d.kv_rank_ordinal, d.size_bytes)
        for d in geometry.objects
    ] == [
        (0, 0, 0, 1048576),
        (0, 0, 1, 1048576),
        (0, 1, 0, 262144),
        (0, 1, 1, 262144),
        (1, 0, 0, 1048576),
        (1, 0, 1, 1048576),
        (1, 1, 0, 262144),
        (1, 1, 1, 262144),
    ]
    # 2 chunks * 2 ranks * (1048576 + 262144).
    assert geometry.task_size_bytes == 5242880
    assert geometry.object_sizes_bytes == (
        1048576,
        1048576,
        262144,
        262144,
        1048576,
        1048576,
        262144,
        262144,
    )
    assert geometry.is_uniform is False


def test_object_group_component_provenance_is_recorded(tmp_path):
    """Component fan-out and block geometry survive resolution."""
    geometry = resolve_submit_geometry(_write(tmp_path, _HYBRID_PROFILE))

    full, sliding = geometry.object_groups
    assert full.object_group_id == 0
    assert full.name == "full_attention"
    assert full.sw_size_chunks == -1
    assert sliding.sw_size_chunks == 1

    component = full.components[0]
    assert component.name == "main_kv"
    assert component.role == "key_value"
    assert component.attention == "gqa"
    assert component.cache_owning_layers == 4
    assert component.dtype == "bfloat16"
    assert component.dtype_bytes == 2
    assert component.tokens_per_block == 128
    assert component.slots_per_block == 128
    assert component.transfer_tokens_per_chunk == 256
    assert component.slots_per_object == 256
    assert component.component_size_bytes == 1048576


def test_kv_rank_fan_out_is_not_inferred_from_model_tp(tmp_path):
    """A task carrying one rank stays one rank regardless of model TP."""
    single_rank = _mutate(
        _HYBRID_PROFILE, "kv_ranks_per_chunk: 2", "kv_ranks_per_chunk: 1"
    )
    single_rank = _mutate(
        single_rank, "task_archetype: lookup_load", "task_archetype: store"
    )
    single_rank = _mutate(single_rank, "objects_per_submit: 8", "objects_per_submit: 4")
    single_rank = _mutate(single_rank, "burst_bytes: 5242880", "burst_bytes: 2621440")

    geometry = resolve_submit_geometry(_write(tmp_path, single_rank))

    assert geometry.task_archetype == "store"
    assert geometry.kv_ranks_per_chunk == 1
    assert geometry.objects_per_submit == 4
    assert {d.kv_rank_ordinal for d in geometry.objects} == {0}


def test_opaque_component_supplies_its_own_size(tmp_path):
    """An opaque component is accepted on its declared bytes alone."""
    opaque = textwrap.dedent(
        """
        model:
          name: example/opaque

        runtime:
          lmcache_tokens_per_chunk: 256
          task_archetype: shared_envelope
          chunks_per_submit: 1
          kv_ranks_per_chunk: 1
          separate_object_groups: true
          full_sw_kv: false

        object_groups:
          - object_group_id: 0
            name: recurrent_state
            sw_size_chunks: 0
            components:
              - name: gdn_state
                role: recurrent_state
                cache_owning_layers: 3
                architecture:
                  attention: opaque
                quantization:
                  dtype: float32
                  dtype_bytes: 4
                component_size_bytes: 98304
            object_size_bytes: 98304
        """
    )
    geometry = resolve_submit_geometry(_write(tmp_path, opaque))

    component = geometry.object_groups[0].components[0]
    assert component.attention == "opaque"
    assert component.component_size_bytes == 98304
    # Block geometry is not fabricated for a byte-opaque component.
    assert component.slots_per_object == 0
    assert geometry.task_size_bytes == 98304
    assert geometry.is_uniform is True


def test_packed_components_share_one_object(tmp_path):
    """Two components in one group pack into a single object, not two.

    This is the MiniMax-M3 shape: main K/V and a key-only indexer are
    separate kernel groups but one object group, so they land in one L2
    object per ``(chunk, kv rank)``.
    """
    text = textwrap.dedent(
        """
        model:
          name: example/packed

        runtime:
          lmcache_tokens_per_chunk: 256
          task_archetype: store
          chunks_per_submit: 2
          kv_ranks_per_chunk: 1
          separate_object_groups: false
          full_sw_kv: false

        object_groups:
          - object_group_id: 0
            name: sparse_attention
            sw_size_chunks: -1
            components:
              - name: main_kv
                role: key_value
                cache_owning_layers: 4
                architecture:
                  attention: gqa
                  kv_size: 2
                  num_kv_heads: 1
                  head_size: 128
                quantization:
                  dtype: bfloat16
                  dtype_bytes: 2
                block_geometry:
                  tokens_per_block: 128
                  slots_per_block: 128
                  transfer_tokens_per_chunk: 256
                component_size_bytes: 524288
              - name: index_key
                role: index_key
                cache_owning_layers: 3
                architecture:
                  attention: opaque
                quantization:
                  dtype: float8_e4m3fn
                  dtype_bytes: 1
                component_size_bytes: 101376
            object_size_bytes: 625664
        """
    )
    geometry = resolve_submit_geometry(_write(tmp_path, text))

    assert geometry.objects_per_submit == 2
    assert [c.name for c in geometry.object_groups[0].components] == [
        "main_kv",
        "index_key",
    ]
    assert geometry.object_sizes_bytes == (625664, 625664)
    assert geometry.task_size_bytes == 1251328
    assert geometry.is_uniform is True


# ---------------------------------------------------------------------------
# Rejections
# ---------------------------------------------------------------------------


def test_mixed_legacy_and_object_group_form_is_rejected(tmp_path):
    mixed = _HYBRID_PROFILE + textwrap.dedent(
        """
        chunking:
          tokens_per_chunk: 256
        page:
          page_size_bytes: 147456
        """
    )
    with pytest.raises(GeometryProfileError, match="exactly one geometry form"):
        resolve_submit_geometry(_write(tmp_path, mixed))


@pytest.mark.parametrize(
    "old,new,message",
    [
        ("task_archetype: lookup_load", "task_archetype: prefill", "task_archetype"),
        ("object_group_id: 1", "object_group_id: 2", "dense"),
        ("name: sliding_window", "name: full_attention", "duplicate"),
        ("chunks_per_submit: 2", "chunks_per_submit: 0", "chunks_per_submit"),
        ("kv_ranks_per_chunk: 2", "kv_ranks_per_chunk: 0", "kv_ranks_per_chunk"),
        ("tokens_per_block: 128", "tokens_per_block: 96", "integral"),
        ("component_size_bytes: 1048576", "component_size_bytes: 1048577", "declared"),
        ("object_size_bytes: 262144", "object_size_bytes: 262145", "object_size_bytes"),
        ("objects_per_submit: 8", "objects_per_submit: 4", "objects_per_submit"),
        ("burst_bytes: 5242880", "burst_bytes: 99", "burst_bytes"),
        ("attention: gqa", "attention: sliding", "attention"),
        ("sw_size_chunks: -1", "sw_size_chunks: full", "sw_size_chunks"),
    ],
)
def test_malformed_object_group_profiles_are_rejected(tmp_path, old, new, message):
    text = _mutate(_HYBRID_PROFILE, old, new)
    with pytest.raises(GeometryProfileError, match=message):
        resolve_submit_geometry(_write(tmp_path, text))


def test_duplicate_component_names_in_one_group_are_rejected(tmp_path):
    """Component names identify a component within its own group."""
    text = textwrap.dedent(
        """
        model:
          name: example/dup

        runtime:
          lmcache_tokens_per_chunk: 256
          task_archetype: store
          chunks_per_submit: 1
          kv_ranks_per_chunk: 1
          separate_object_groups: false
          full_sw_kv: false

        object_groups:
          - object_group_id: 0
            name: packed
            sw_size_chunks: -1
            components:
              - name: main_kv
                role: key_value
                cache_owning_layers: 2
                architecture:
                  attention: opaque
                quantization:
                  dtype: bfloat16
                  dtype_bytes: 2
                component_size_bytes: 4096
              - name: main_kv
                role: index_key
                cache_owning_layers: 2
                architecture:
                  attention: opaque
                quantization:
                  dtype: bfloat16
                  dtype_bytes: 2
                component_size_bytes: 2048
            object_size_bytes: 6144
        """
    )
    with pytest.raises(GeometryProfileError, match="duplicate component name"):
        resolve_submit_geometry(_write(tmp_path, text))


def test_opaque_component_must_not_declare_block_geometry(tmp_path):
    """A byte-opaque component has no per-token layout to declare."""
    text = _mutate(_HYBRID_PROFILE, "attention: gqa", "attention: opaque")
    with pytest.raises(GeometryProfileError, match="block_geometry"):
        resolve_submit_geometry(_write(tmp_path, text))


def test_missing_object_groups_section_is_rejected(tmp_path):
    text = textwrap.dedent(
        """
        model:
          name: example/empty
        runtime:
          lmcache_tokens_per_chunk: 256
          task_archetype: store
          chunks_per_submit: 1
          kv_ranks_per_chunk: 1
          separate_object_groups: false
          full_sw_kv: false
        object_groups: []
        """
    )
    with pytest.raises(GeometryProfileError, match="object_groups"):
        resolve_submit_geometry(_write(tmp_path, text))


def test_transfer_tokens_beyond_chunk_is_rejected(tmp_path):
    text = _mutate(
        _HYBRID_PROFILE,
        "lmcache_tokens_per_chunk: 256",
        "lmcache_tokens_per_chunk: 128",
    )
    with pytest.raises(GeometryProfileError, match="transfer_tokens_per_chunk"):
        resolve_submit_geometry(_write(tmp_path, text))


def test_non_boolean_object_group_separation_is_rejected(tmp_path):
    text = _mutate(
        _HYBRID_PROFILE, "separate_object_groups: true", "separate_object_groups: 1"
    )
    with pytest.raises(GeometryProfileError, match="separate_object_groups"):
        resolve_submit_geometry(_write(tmp_path, text))


# ---------------------------------------------------------------------------
# Legacy and raw compatibility
# ---------------------------------------------------------------------------


def test_legacy_profile_resolves_through_the_dispatcher(tmp_path):
    """A legacy page-burst profile becomes one synthetic object group."""
    path = _write(tmp_path, _LEGACY_PROFILE)
    geometry = resolve_submit_geometry(path)

    assert geometry.profile_mode == PROFILE_MODE_LEGACY
    assert geometry.model_name == "deepseek-ai/DeepSeek-V3"
    assert geometry.objects_per_submit == 61
    assert geometry.page_size_bytes == 147456
    assert geometry.task_size_bytes == 8994816
    assert geometry.tokens_per_chunk == 256
    assert geometry.is_uniform is True
    assert len(geometry.object_groups) == 1
    assert geometry.chunks_per_submit == 61
    assert geometry.kv_ranks_per_chunk == 1
    assert geometry.sha256 == resolve_geometry_profile(path).sha256
    # Legacy objects keep their flat page-burst identity: one synthetic
    # group, one rank, one object per chunk ordinal.
    assert [d.chunk_ordinal for d in geometry.objects] == list(range(61))
    assert {d.object_group_id for d in geometry.objects} == {0}


def test_uniform_profile_wrapper_matches_the_dispatcher(tmp_path):
    path = _write(tmp_path, _LEGACY_PROFILE)
    wrapped = submit_geometry_from_uniform(resolve_geometry_profile(path))

    assert wrapped == resolve_submit_geometry(path)


def test_raw_geometry_becomes_one_synthetic_group():
    geometry = raw_submit_geometry(num_keys=4, page_size_bytes=1024)

    assert geometry.profile_mode == PROFILE_MODE_LEGACY
    assert geometry.source_path == ""
    assert geometry.shape_spec == ""
    assert geometry.objects_per_submit == 4
    assert geometry.object_sizes_bytes == (1024, 1024, 1024, 1024)
    assert geometry.task_size_bytes == 4096
    assert geometry.is_uniform is True
