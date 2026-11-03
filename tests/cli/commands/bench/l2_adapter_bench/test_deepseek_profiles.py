# SPDX-License-Identifier: Apache-2.0
"""Check packed storage geometry against pinned DeepSeek config snapshots."""

# Standard
from pathlib import Path
import argparse
import json

# Third Party
import pytest
import yaml

# First Party
from lmcache.cli.commands.bench.l2_adapter_bench.command import (
    add_l2_arguments,
    run_l2_adapter_bench,
)
from lmcache.cli.commands.bench.l2_adapter_bench.geometry import (
    GeometryProfileError,
    resolve_submit_geometry,
)

_MODELS = Path(__file__).resolve().parents[5] / "scripts/ipu-poc/models"
# A submit is a 16K-token prefix retrieve: 64 chunks of 256 tokens, 1 object per chunk.
_CHUNKS_PER_SUBMIT = 64
_CONFIGS = Path(__file__).parent / "fixtures/deepseek"


@pytest.mark.parametrize(
    ("profile", "model", "payload_256", "stored_256", "payload_1024", "stored_1024"),
    [
        ("deepseek_v3_2", "DeepSeek-V3.2", 12305408, 12308480, 49221632, 49221632),
        ("deepseek_v4_flash", "DeepSeek-V4-Flash", 899648, 901120, 3598592, 3600384),
        ("deepseek_v4_pro", "DeepSeek-V4-Pro", 1288048, 1290240, 5152192, 5152768),
        ("deepseek_v4_1_flash", "DeepSeek-V4.1-Flash", 227840, 229376, 911360, 913408),
    ],
)
def test_persisted_chunk_bytes(
    profile: str,
    model: str,
    payload_256: int,
    stored_256: int,
    payload_1024: int,
    stored_1024: int,
) -> None:
    """Catch missing scales, MTP inclusion, duplicate KV owners, or layer objects."""
    config = json.loads((_CONFIGS / f"{model}.json").read_text())
    if "text_config" in config:
        config = config["text_config"]
    for tokens, payload, stored in [
        (256, payload_256, stored_256),
        (1024, payload_1024, stored_1024),
    ]:
        if model == "DeepSeek-V3.2":
            main = config["kv_lora_rank"] + 16 + 2 * config["qk_rope_head_dim"]
            index = config["index_head_dim"] + 4
            expected = config["num_hidden_layers"] * tokens * (main + index)
        elif model == "DeepSeek-V4.1-Flash":
            main = config["head_dim"] // 2 + config["head_dim"] // 16
            index = config["index_head_dim"] // 2 + config["index_head_dim"] // 32
            expected = sum(
                tokens // config["compress_ratios"][layer] * (main + index)
                for layer in config["kv_source_layer_ids"]
            )
        else:
            main = config["head_dim"] - config["qk_rope_head_dim"]
            main += 2 * config["qk_rope_head_dim"] + 8
            index = config["index_head_dim"] // 2 + config["index_head_dim"] // 32
            # compress_ratios also contains the MTP layer; it owns no persisted KV.
            ratios = config["compress_ratios"][: config["num_hidden_layers"]]
            expected = sum(
                tokens // ratio * (main + (index if ratio == 4 else 0))
                for ratio in ratios
                if ratio
            )
        assert expected == payload
        geometry = resolve_submit_geometry(
            str(_MODELS / f"{profile}.yaml"), tokens_per_chunk=tokens
        )
        assert geometry.tokens_per_chunk == tokens
        assert geometry.objects_per_submit == _CHUNKS_PER_SUBMIT
        assert len(geometry.object_groups) == 1
        assert geometry.payload_size_bytes == _CHUNKS_PER_SUBMIT * payload
        assert geometry.padding_size_bytes == _CHUNKS_PER_SUBMIT * (stored - payload)
        assert geometry.object_sizes_bytes == (stored,) * _CHUNKS_PER_SUBMIT
        assert geometry.task_size_bytes == _CHUNKS_PER_SUBMIT * stored


def test_default_chunk_size_is_256() -> None:
    """The profile's default must not drift to the conditional 1024-token run."""
    geometry = resolve_submit_geometry(str(_MODELS / "deepseek_v4_1_flash.yaml"))
    assert geometry.tokens_per_chunk == 256
    assert geometry.object_sizes_bytes == (229376,) * _CHUNKS_PER_SUBMIT


@pytest.mark.parametrize("tokens", [0, -1, True, 257])
def test_invalid_or_partial_compression_chunk_is_rejected(tokens: int) -> None:
    """Do not truncate compressed entries or accept non-positive chunk sizes."""
    with pytest.raises(GeometryProfileError):
        resolve_submit_geometry(
            str(_MODELS / "deepseek_v4_flash.yaml"), tokens_per_chunk=tokens
        )


@pytest.mark.parametrize("size", [0, -1, True])
def test_corrupt_packed_record_size_is_rejected(tmp_path: Path, size: int) -> None:
    """A packed record still needs a positive byte count."""
    profile = yaml.safe_load((_MODELS / "deepseek_v4_flash.yaml").read_text())
    profile["object_groups"][0]["components"][0]["architecture"]["bytes_per_slot"] = (
        size
    )
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(profile))
    with pytest.raises(GeometryProfileError, match="bytes_per_slot"):
        resolve_submit_geometry(str(path))


def test_chunk_override_rejects_fixed_geometry() -> None:
    """Legacy fixed-byte profiles must not silently ignore the chunk override."""
    with pytest.raises(GeometryProfileError, match="packed"):
        resolve_submit_geometry(
            str(_MODELS / "minimax_m3_bf16_tp8.yaml"), tokens_per_chunk=1024
        )


def test_geometry_only_prints_sizes_without_adapter_setup(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No adapter spec is needed to inspect geometry, including its tail padding."""
    parser = argparse.ArgumentParser()
    add_l2_arguments(parser)
    args = parser.parse_args(
        [
            "--kvcache-shape-profile",
            str(_MODELS / "deepseek_v4_1_flash.yaml"),
            "--tokens-per-chunk",
            "1024",
            "--geometry-only",
        ]
    )
    # No command surface: reaching adapter setup or metrics construction must fail.
    run_l2_adapter_bench(None, args)  # type: ignore[arg-type]
    output = capsys.readouterr().out
    assert "1024 tokens/chunk" in output
    assert "911360 B payload" in output
    assert "2048 B O_DIRECT padding" in output
    assert "913408 B/object" in output
    assert f"{_CHUNKS_PER_SUBMIT} objects/submit" in output
    assert f"{_CHUNKS_PER_SUBMIT * 913408} B/submit" in output


def test_chunk_selector_requires_a_profile(capsys: pytest.CaptureFixture[str]) -> None:
    """A chunk override must never be ignored for a raw byte-size run."""
    parser = argparse.ArgumentParser()
    add_l2_arguments(parser)
    args = parser.parse_args(["--tokens-per-chunk", "1024", "--geometry-only"])
    with pytest.raises(SystemExit) as error:
        run_l2_adapter_bench(None, args)  # type: ignore[arg-type]
    assert error.value.code == 2
    assert "--kvcache-shape-profile" in capsys.readouterr().err
