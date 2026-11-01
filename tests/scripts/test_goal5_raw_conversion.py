# SPDX-License-Identifier: Apache-2.0
"""Raw conversion must change only the exact private mount entry."""

# Standard
from pathlib import Path
import sys
import tarfile

# Third Party
import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts/ipu-poc"))


def test_private_fstab_preserves_os_entries_and_rejects_foreign_sources() -> None:
    """A matching mountpoint alone cannot authorize changing an unrelated device."""
    # Third Party
    from goal5_raw_conversion import private_fstab

    original = (
        "# existing\n"
        "UUID=os-root / xfs defaults 0 0\n"
        "UUID=private-array /mnt/lmcache xfs defaults,noauto,nofail 0 0\n"
    )
    result = private_fstab(original, "private-array")
    assert result.startswith("# existing\nUUID=os-root / xfs defaults 0 0\n")
    assert "# Goal5 raw owner: UUID=private-array /mnt/lmcache" in result
    with pytest.raises(ValueError, match="private mount"):
        private_fstab(original, "another-array")
    with pytest.raises(ValueError, match="duplicate"):
        private_fstab(
            original + original.splitlines(keepends=True)[-1], "private-array"
        )


def test_inventory_retains_failed_store_and_excludes_only_fio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Historical rejected Store payloads must survive the raw conversion archive."""
    # Third Party
    import goal5_raw_conversion as conversion

    monkeypatch.setattr(conversion, "MOUNT", tmp_path)
    failed = tmp_path / "mmgi1/goal5/rejected"
    failed.mkdir(parents=True)
    (failed / "retained.data").write_bytes(b"preserved")
    scratch = tmp_path / "mmgi1/fio"
    scratch.mkdir()
    (scratch / "scratch.bin").write_bytes(b"scratch")
    entries = conversion.inventory("mmgi1")
    assert list(entries) == ["mmgi1/goal5/rejected/retained.data"]
    assert entries["mmgi1/goal5/rejected/retained.data"][0] == 9
    (failed / "foreign").symlink_to(tmp_path / "mmgi1/fio/scratch.bin")
    with pytest.raises(ValueError, match="Unexpected historical"):
        conversion.inventory("mmgi1")


@pytest.mark.skipif(sys.platform != "linux", reason="GNU tar compressor invocation")
def test_preservation_keeps_pytest_links_without_following_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pytest current link must survive alongside its payload, without duplication."""
    # Third Party
    import goal5_raw_conversion as conversion

    mount = tmp_path / "mount"
    target = mount / "mmgi1/goal5/pytest/case0"
    target.mkdir(parents=True)
    (target / "retained.data").write_bytes(b"preserved")
    link = target.with_name("casecurrent")
    link.symlink_to(target, target_is_directory=True)
    archive_root = tmp_path / "archive"
    archive_root.mkdir()
    monkeypatch.setattr(conversion, "MOUNT", mount)
    receipt = conversion.preserve("mmgi1", archive_root)
    assert receipt["files"] == 1
    assert receipt["symlinks"] == 1
    assert receipt["payload_bytes"] == 9
    with tarfile.open(receipt["archive"], "r:gz") as archive:
        member = archive.getmember("./mmgi1/goal5/pytest/casecurrent")
        assert member.issym()
        assert member.linkname == str(target)
        assert sum(item.size for item in archive if item.isfile()) == 9
