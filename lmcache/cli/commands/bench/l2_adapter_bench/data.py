# SPDX-License-Identifier: Apache-2.0
"""Test data construction helpers for L2 adapter benchmarks."""

# Future
from __future__ import annotations

# Standard
from collections.abc import Mapping, Sequence
import select

# Third Party
import torch

# First Party
from lmcache.v1.distributed.api import ObjectKey
from lmcache.v1.distributed.internal_api import L1MemoryDesc
from lmcache.v1.memory_management import (
    MemoryFormat,
    MemoryObj,
    MemoryObjMetadata,
    TensorMemoryObj,
)
from lmcache.v1.platform import consume_fd

_KB = 1024


def make_aligned_tensor(num_bytes: int, align_bytes: int = 1) -> torch.Tensor:
    """Create a 1-D uint8 tensor whose data pointer is aligned.

    Args:
        num_bytes: Number of bytes in the returned tensor.
        align_bytes: Required data pointer alignment in bytes.

    Returns:
        A 1-D ``torch.uint8`` tensor with ``num_bytes`` elements.

    Raises:
        ValueError: If ``num_bytes`` is negative or ``align_bytes`` is
            not positive.
        RuntimeError: If the allocated tensor cannot be aligned.
    """
    if num_bytes < 0:
        raise ValueError("num_bytes must be non-negative")
    if align_bytes <= 0:
        raise ValueError("align_bytes must be positive")
    if align_bytes == 1:
        return torch.empty(num_bytes, dtype=torch.uint8)

    backing = torch.empty(num_bytes + align_bytes - 1, dtype=torch.uint8)
    offset = (-backing.data_ptr()) % align_bytes
    aligned = backing[offset : offset + num_bytes]
    if aligned.data_ptr() % align_bytes != 0:
        raise RuntimeError(
            f"failed to allocate {align_bytes}-byte aligned benchmark buffer"
        )
    return aligned


def make_object_keys(
    num_keys: int, model_name: str = "bench-model", key_offset: int = 0
) -> list[ObjectKey]:
    """Generate *num_keys* unique ``ObjectKey`` instances for benchmarking.

    ``ObjectKey`` is a frozen dataclass with field order:
    (chunk_hash, model_name, kv_rank).

    Keys are a pure function of ``(model_name, key_offset)``, so two
    invocations with the same arguments produce the same keys and address
    the same backing objects. That is deliberate -- it is what lets
    ``--only store`` be followed by ``--only load`` -- but it means a
    *store* run repeated with the same ``model_name`` re-targets objects
    that already exist. Pass a distinct ``model_name`` to get a fresh key
    universe; see ``--key-prefix``.

    Args:
        num_keys: Number of keys to generate.
        model_name: Model name embedded in each key. Acts as the key
            namespace: distinct values yield disjoint key universes.
        key_offset: Starting index offset to ensure uniqueness across threads.
    """
    keys: list[ObjectKey] = []
    for i in range(num_keys):
        idx = key_offset + i
        # chunk_hash: 16 bytes derived from index to guarantee uniqueness
        chunk_hash = idx.to_bytes(16, "big")
        keys.append(
            ObjectKey(
                chunk_hash=chunk_hash,
                model_name=model_name,
                kv_rank=idx,
            )
        )
    return keys


def make_object_group_keys(
    first_chunk_index: int,
    chunks_per_submit: int,
    object_group_ids: Sequence[int],
    kv_ranks_per_chunk: int,
    model_name: str = "bench-model",
) -> list[ObjectKey]:
    """Generate the keys one production-shaped L2 submit addresses.

    Keys are emitted in ``chunk -> object group -> kv rank`` order, which is
    the order the engine issues them in and the order the resolved object
    descriptors use, so a key's position in the returned list is also its
    object's position in the L1 buffer.

    All objects belonging to one chunk share a single ``chunk_hash`` derived
    from the global chunk index. That mirrors production, where the hash
    identifies the token range and the object group and KV rank select a
    slice of it -- and it is what makes a chunk-atomic hit or miss possible.
    ``kv_rank`` is built through :meth:`ObjectKey.ComputeKVRank` with the
    single-node pattern (``world_size`` equal to ``local_world_size``), so
    the encoded rank matches what a real worker would publish.

    Args:
        first_chunk_index: Global index of this submit's first chunk. Keys
            are a pure function of this and ``model_name``, so the same
            arguments re-address the same objects.
        chunks_per_submit: Number of consecutive chunks in the submit.
        object_group_ids: Object group IDs in submit order.
        kv_ranks_per_chunk: Number of KV ranks each chunk fans out to.
        model_name: Model name embedded in each key, acting as the key
            namespace.

    Returns:
        The submit's keys in descriptor order.
    """
    keys: list[ObjectKey] = []
    for chunk_offset in range(chunks_per_submit):
        chunk_hash = (first_chunk_index + chunk_offset).to_bytes(16, "big")
        for object_group_id in object_group_ids:
            for rank in range(kv_ranks_per_chunk):
                keys.append(
                    ObjectKey(
                        chunk_hash=chunk_hash,
                        model_name=model_name,
                        kv_rank=ObjectKey.ComputeKVRank(
                            world_size=kv_ranks_per_chunk,
                            global_rank=rank,
                            local_world_size=kv_ranks_per_chunk,
                            local_rank=rank,
                        ),
                        object_group_id=object_group_id,
                    )
                )
    return keys


def make_memory_objects_from_sizes(
    buffer: torch.Tensor,
    object_sizes: Sequence[int],
    base_offset: int,
    fill_offset: int = 0,
) -> list[MemoryObj]:
    """Create MemoryObj views for objects whose sizes need not agree.

    Objects are laid out back to back from ``base_offset`` by prefix sum, so
    a heterogeneous submit occupies one contiguous range with no padding
    between objects.

    Each object is pre-filled with ``(position + fill_offset) mod 256``,
    keyed off the object's flattened position rather than its size, so
    ``verify_round_trip`` can still detect a swap between two objects that
    happen to be the same size.

    Args:
        buffer: Contiguous benchmark L1 buffer that backs all objects.
        object_sizes: Size of each memory object in bytes, in submit order.
        base_offset: Byte offset of the first object within ``buffer``.
        fill_offset: Offset added to each object position before generating
            the byte fill pattern.

    Returns:
        ``TensorMemoryObj`` instances whose ``raw_data`` tensors are views
        into ``buffer``.

    Raises:
        ValueError: If the requested object range falls outside ``buffer``.
    """
    flat_buffer = buffer.view(-1)
    objects: list[MemoryObj] = []
    start = base_offset
    for i, data_size in enumerate(object_sizes):
        end = start + data_size
        if start < 0 or end > flat_buffer.numel():
            raise ValueError(
                f"L1 benchmark buffer too small for object {i}: "
                f"[{start}, {end}) > {flat_buffer.numel()}"
            )
        raw_tensor = flat_buffer[start:end]
        raw_tensor.fill_((i + fill_offset) & 0xFF)
        metadata = MemoryObjMetadata(
            shape=torch.Size([data_size]),
            dtype=torch.uint8,
            address=raw_tensor.data_ptr(),
            phy_size=data_size * raw_tensor.element_size(),
            fmt=MemoryFormat.KV_2LTD,
            ref_count=1,
        )
        objects.append(
            TensorMemoryObj(
                raw_data=raw_tensor,
                metadata=metadata,
                parent_allocator=None,
            )
        )
        start = end
    return objects


def create_l1_memory_desc(
    buffer: torch.Tensor,
    align_bytes: int = 1,
) -> L1MemoryDesc:
    """Create an L1 memory descriptor for a contiguous test buffer."""
    flat_buffer = buffer.view(-1)
    return L1MemoryDesc(
        ptr=flat_buffer.data_ptr(),
        size=flat_buffer.numel() * flat_buffer.element_size(),
        align_bytes=align_bytes,
    )


def wait_eventfd(efd: int, timeout: float = 60.0) -> bool:
    """Block until the eventfd is signalled or *timeout* seconds elapse.

    Uses ``select.poll`` + ``consume_fd`` for cross-platform compatibility.

    Returns True if the fd was signalled, False on timeout.
    """
    poller = select.poll()
    poller.register(efd, select.POLLIN)
    # poll() expects timeout in milliseconds
    events = poller.poll(timeout * 1000)
    if events:
        consume_fd(efd)
        return True
    return False


def wait_eventfds(event_fds: Mapping[str, int], timeout: float = 60.0) -> set[str]:
    """Wait for one or more named eventfds and return the ready names.

    Each eventfd is consumed once before returning, matching
    :func:`wait_eventfd`. Adapters require their store and load completion
    fds to be distinct, so a mixed benchmark can use the returned names to
    harvest only the direction that actually completed.

    Args:
        event_fds: Mapping from an operation name to its completion eventfd.
        timeout: Maximum time to wait in seconds.

    Returns:
        Names whose eventfds were signalled, or an empty set on timeout.

    Raises:
        ValueError: If two operation names share one eventfd. A notification
            is consumed once, so the caller could not tell which operation
            completed.
    """
    if len(set(event_fds.values())) != len(event_fds):
        raise ValueError("each operation must have a distinct completion eventfd")

    poller = select.poll()
    names_by_fd: dict[int, set[str]] = {}
    for name, efd in event_fds.items():
        poller.register(efd, select.POLLIN)
        names_by_fd.setdefault(efd, set()).add(name)

    ready_names: set[str] = set()
    for efd, _event in poller.poll(timeout * 1000):
        consume_fd(efd)
        ready_names.update(names_by_fd.get(efd, set()))
    return ready_names


def verify_round_trip(keys, store_objects, load_objects, log) -> bool:
    """Verify that loaded data matches what was stored.

    Compares the underlying ``raw_data`` tensors directly (more efficient
    than converting via ``byte_array``).
    """
    mismatches = 0
    for i, (s_obj, l_obj) in enumerate(zip(store_objects, load_objects, strict=True)):
        if not torch.equal(s_obj.raw_data, l_obj.raw_data):
            mismatches += 1
            log(
                f"  [Verify] Key {i}: MISMATCH "
                f"(store {s_obj.get_physical_size()} bytes "
                f"vs load {l_obj.get_physical_size()} bytes)"
            )
    if mismatches == 0:
        log(f"  [Verify] All {len(keys)} keys data verified OK.")
        return True
    log(f"  [Verify] {mismatches}/{len(keys)} keys have data mismatches!")
    return False
