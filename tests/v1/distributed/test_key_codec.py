# SPDX-License-Identifier: Apache-2.0
"""Shared codecs preserve established backend names and escaping."""

# Third Party
import pytest

# First Party
from lmcache.v1.distributed.api import ObjectKey
from lmcache.v1.distributed.key_codec import (
    decode_fs_filename,
    decode_storage_key,
    encode_fs_filename,
    encode_storage_key,
    format_storage_key,
)
from lmcache.v1.distributed.l2_adapters.bigtable_key_encoder import BigtableL2KeyEncoder
from lmcache.v1.distributed.l2_adapters.raw_block.key_codec import (
    decode_object_key,
    object_key_to_string,
)


@pytest.mark.parametrize("salt", ["", "tenant"])
def test_legacy_backend_bytes_and_recovery(salt: str) -> None:
    key = ObjectKey(b"\xde\xad\xbe\xef", "org/model", 42, 3, salt)
    suffix = f"@{salt}" if salt else ""
    assert encode_storage_key(key) == f"org/model@0000002a@3@deadbeef{suffix}"
    assert decode_storage_key(encode_storage_key(key)) == key
    assert (
        encode_fs_filename(key) == f"org-SEP-model@0x0000002a@3@deadbeef{suffix}.data"
    )
    assert decode_fs_filename(encode_fs_filename(key)) == key
    assert object_key_to_string(key) == f"org%2Fmodel@0x0000002a@3@deadbeef{suffix}"
    assert decode_object_key(object_key_to_string(key)) == key
    assert (
        format_storage_key(
            key, "{model}_{rank}_{group}_{hash}{suffix}.bin", model_separator="--"
        )
        == f"org--model_0000002a_3_deadbeef{suffix}.bin"
    )
    assert BigtableL2KeyEncoder().encode_row_key(key) == (
        f"dead@org/model@0000002a@3@deadbeef{suffix}".encode()
    )
    assert BigtableL2KeyEncoder(layer_group_size=4).encode_row_key(key) == (
        f"dead@org/model@lg4@0000002a@3@deadbeef{suffix}".encode()
    )


@pytest.mark.parametrize("tags", [{}, {"lora": "a1", "dtype": "fp8"}])
@pytest.mark.parametrize("salt", ["", "tenant"])
@pytest.mark.parametrize("rank, group", [(0, 0), (0xFFFFFFFF, 255), (7, 0x1F)])
def test_storage_key_matches_the_generic_template(
    salt: str, rank: int, group: int, tags: dict[str, str]
) -> None:
    key = ObjectKey(bytes(range(16)), "a/b/c", rank, group, salt, tags=tags)
    template = "{model}@{rank}@{group}@{hash}{suffix}"
    assert encode_storage_key(key) == format_storage_key(key, template)


def test_storage_keys_sharing_model_rank_group_stay_distinct() -> None:
    """Keys differing only in hash, salt or tags encode to their own names."""
    template = "{model}@{rank}@{group}@{hash}{suffix}"
    keys = [
        ObjectKey(chunk_hash.to_bytes(16, "big"), "org/model", 5, 2, salt, tags=tags)
        for chunk_hash in range(3)
        for salt in ("", "tenant")
        for tags in ({}, {"lora": "a1"})
    ]
    names = [encode_storage_key(key) for key in keys]
    assert len(set(names)) == len(keys)
    for key, name in zip(keys, names, strict=True):
        assert name == format_storage_key(key, template)
        assert decode_storage_key(name) == key
