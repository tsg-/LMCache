# SPDX-License-Identifier: Apache-2.0

# Third Party
import pytest
import torch

# First Party
from lmcache.cli.commands.bench.l2_adapter_bench.data import (
    create_l1_memory_desc,
    make_aligned_tensor,
    make_memory_objects_from_sizes,
    make_object_group_keys,
)
from lmcache.v1.distributed.api import ObjectKey


def test_make_aligned_tensor_returns_aligned_buffer() -> None:
    tensor = make_aligned_tensor(4096 * 3, align_bytes=4096)

    assert tensor.numel() == 4096 * 3
    assert tensor.dtype == torch.uint8
    assert tensor.data_ptr() % 4096 == 0


def test_create_l1_memory_desc_uses_requested_alignment() -> None:
    tensor = make_aligned_tensor(8192, align_bytes=4096)

    desc = create_l1_memory_desc(tensor, align_bytes=4096)

    assert desc.ptr == tensor.data_ptr()
    assert desc.size == 8192
    assert desc.align_bytes == 4096


def test_make_memory_objects_uses_shared_l1_range() -> None:
    buffer = make_aligned_tensor(4096, align_bytes=1024)

    objects = make_memory_objects_from_sizes(
        buffer,
        object_sizes=(1024, 1024),
        base_offset=1024,
    )

    assert len(objects) == 2
    assert objects[0].raw_data.data_ptr() == buffer.data_ptr() + 1024
    assert objects[1].raw_data.data_ptr() == buffer.data_ptr() + 2048
    assert torch.all(objects[0].raw_data == 0)
    assert torch.all(objects[1].raw_data == 1)


def test_make_memory_objects_can_use_different_fill_pattern() -> None:
    buffer = make_aligned_tensor(2048, align_bytes=1024)

    objects = make_memory_objects_from_sizes(
        buffer,
        object_sizes=(1024, 1024),
        base_offset=0,
        fill_offset=1,
    )

    assert torch.all(objects[0].raw_data == 1)
    assert torch.all(objects[1].raw_data == 2)


def test_heterogeneous_objects_are_packed_by_prefix_sum() -> None:
    buffer = make_aligned_tensor(4096, align_bytes=1024)

    objects = make_memory_objects_from_sizes(
        buffer,
        object_sizes=(1024, 256, 512),
        base_offset=512,
    )

    assert [obj.raw_data.numel() for obj in objects] == [1024, 256, 512]
    assert [obj.raw_data.data_ptr() - buffer.data_ptr() for obj in objects] == [
        512,
        1536,
        1792,
    ]
    assert [obj.metadata.phy_size for obj in objects] == [1024, 256, 512]
    # Fill still keys off flattened object position, not object size, so a
    # short object cannot alias a neighbour's pattern.
    assert torch.all(objects[0].raw_data == 0)
    assert torch.all(objects[1].raw_data == 1)
    assert torch.all(objects[2].raw_data == 2)


def test_heterogeneous_objects_reject_an_undersized_buffer() -> None:
    buffer = make_aligned_tensor(1024, align_bytes=1024)

    with pytest.raises(ValueError, match="buffer too small"):
        make_memory_objects_from_sizes(buffer, object_sizes=(512, 1024), base_offset=0)


def test_object_group_keys_are_ordered_chunk_group_rank() -> None:
    keys = make_object_group_keys(
        first_chunk_index=7,
        chunks_per_submit=2,
        object_group_ids=(0, 1),
        kv_ranks_per_chunk=2,
        model_name="ns-bench-model",
    )

    rank_0 = ObjectKey.ComputeKVRank(2, 0, 2, 0)
    rank_1 = ObjectKey.ComputeKVRank(2, 1, 2, 1)
    assert [(k.chunk_hash, k.object_group_id, k.kv_rank) for k in keys] == [
        ((7).to_bytes(16, "big"), 0, rank_0),
        ((7).to_bytes(16, "big"), 0, rank_1),
        ((7).to_bytes(16, "big"), 1, rank_0),
        ((7).to_bytes(16, "big"), 1, rank_1),
        ((8).to_bytes(16, "big"), 0, rank_0),
        ((8).to_bytes(16, "big"), 0, rank_1),
        ((8).to_bytes(16, "big"), 1, rank_0),
        ((8).to_bytes(16, "big"), 1, rank_1),
    ]
    assert {k.model_name for k in keys} == {"ns-bench-model"}


def test_object_group_keys_share_one_chunk_hash_across_groups() -> None:
    """All objects of a chunk address the same chunk, as in production."""
    keys = make_object_group_keys(
        first_chunk_index=0,
        chunks_per_submit=1,
        object_group_ids=(0, 1, 2),
        kv_ranks_per_chunk=1,
    )

    assert len({key.chunk_hash for key in keys}) == 1
    assert [key.object_group_id for key in keys] == [0, 1, 2]


def test_object_group_keys_are_deterministic() -> None:
    first = make_object_group_keys(3, 2, (0,), 1)
    second = make_object_group_keys(3, 2, (0,), 1)

    assert first == second
