# SPDX-License-Identifier: Apache-2.0
"""Tests for ``bench l2`` argument validation and warmup stripping."""

# Standard
from hashlib import sha256
from pathlib import Path
from unittest.mock import MagicMock
import argparse
import json

# Third Party
import pytest

# First Party
from lmcache.cli.metrics import Metrics
from lmcache.cli.commands.bench.l2_adapter_bench.command import (
    _parse_read_write_ratio,
    _strip_warmup,
    add_l2_arguments,
    run_l2_adapter_bench,
)
from lmcache.cli.commands.bench.l2_adapter_bench.geometry import (
    GeometryProfileError,
    resolve_geometry_profile,
    resolve_inline_shape_spec,
)
from lmcache.cli.commands.bench.l2_adapter_bench.result import BenchResult

_MB = 1024 * 1024
# Points at a path that is never created; validation must reject the
# arguments before any adapter touches the filesystem.
_ADAPTER_JSON = '{"type":"fs","base_path":"/nonexistent/bench-l2-validation"}'


def _parse(*argv: str, adapter_json: str = _ADAPTER_JSON) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    add_l2_arguments(parser)
    return parser.parse_args(["--l2-adapter", adapter_json, *argv])


def _fs_adapter_json(tmp_path: Path) -> str:
    return json.dumps({"type": "fs", "base_path": str(tmp_path / "l2")})


def _write_deepseek_profile(tmp_path: Path) -> Path:
    """Write the uniform 256-token DeepSeek-V3 MLA profile used by the PoC."""
    profile = tmp_path / "deepseek_v3_fp8.yaml"
    profile.write_text(
        "\n".join(
            [
                'model: {name: "deepseek-ai/DeepSeek-V3"}',
                (
                    "architecture: {num_layers: 61, attention: mla, "
                    "cached_elems_per_token: 576}"
                ),
                "quantization: {dtype_bytes: 1}",
                "chunking: {tokens_per_chunk: 256}",
                "page: {page_size_bytes: 147456}",
                "burst: {layers_per_burst: 61, burst_bytes: 8994816}",
            ]
        )
    )
    return profile


def _write_gqa_profile(tmp_path: Path) -> Path:
    """Write a uniform GQA profile with 256 KiB pages."""
    profile = tmp_path / "gqa.yaml"
    profile.write_text(
        "\n".join(
            [
                "model: {name: test-gqa}",
                (
                    "architecture: {num_layers: 2, kv_size: 2, "
                    "num_kv_heads: 8, head_size: 128}"
                ),
                "quantization: {dtype_bytes: 1}",
                "chunking: {tokens_per_chunk: 128}",
                "page: {page_size_bytes: 262144}",
                "burst: {layers_per_burst: 2, burst_bytes: 524288}",
            ]
        )
    )
    return profile


class _MetricsCommand:
    """Minimal command surface that retains the emitted metrics."""

    def __init__(self) -> None:
        self.metrics: Metrics | None = None

    def create_metrics(
        self, title: str, _args: argparse.Namespace, width: int
    ) -> Metrics:
        self.metrics = Metrics(title)
        return self.metrics


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_rounds_mode_is_the_default() -> None:
    args = _parse()

    assert args.duration_sec == 0.0
    assert args.warmup_sec == 0.0
    # Empty prefix keeps the historical key universe addressable, so
    # existing rounds-mode corpora survive this flag being added.
    assert args.key_prefix == ""
    assert args.write_key_prefix == ""
    assert args.read_write_ratio is None


def test_duration_sec_and_warmup_sec_parse() -> None:
    args = _parse("--duration-sec", "30", "--warmup-sec", "5")

    assert args.duration_sec == 30.0
    assert args.warmup_sec == 5.0


def test_kvcache_shape_profile_resolves_deepseek_page_burst(
    tmp_path: Path,
) -> None:
    """A profile must set the submitted object count and page size."""
    profile = _write_deepseek_profile(tmp_path)
    command = _MetricsCommand()
    args = _parse(
        "--only",
        "store",
        "--key-prefix",
        "deepseek-profile",
        "--kvcache-shape-profile",
        str(profile),
        "--in-flight",
        "1",
        "--rounds",
        "1",
        "--warmup-rounds",
        "0",
        adapter_json=_fs_adapter_json(tmp_path),
    )

    run_l2_adapter_bench(command, args)

    assert args.num_keys == 61
    assert args.data_size_kb == 144
    assert command.metrics is not None
    geometry = command.metrics.to_dict()["metrics"]["geometry"]
    assert geometry == {
        "profile_path": str(profile.resolve()),
        "profile_sha256": sha256(profile.read_bytes()).hexdigest(),
        "model_name": "deepseek-ai/DeepSeek-V3",
        "tokens_per_chunk": 256,
        "objects_per_submit": 61,
        "page_size_bytes": 147456,
        "task_size_bytes": 8994816,
    }
    # A file-backed profile records no inline spec.
    assert "shape_spec" not in geometry


def test_kvcache_shape_profile_resolves_gqa_page_formula(tmp_path: Path) -> None:
    """GQA profiles resolve the per-layer page from KV heads and head size."""
    geometry = resolve_geometry_profile(str(_write_gqa_profile(tmp_path)))

    assert geometry.model_name == "test-gqa"
    assert geometry.objects_per_submit == 2
    assert geometry.page_size_bytes == 262144
    assert geometry.data_size_kb == 256
    assert geometry.task_size_bytes == 524288


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        pytest.param(
            "--num-keys",
            "61",
            id="explicit-num-keys",
        ),
        pytest.param(
            "--data-size-kb",
            "144",
            id="explicit-data-size",
        ),
    ],
)
def test_kvcache_shape_profile_rejects_manual_raw_geometry(
    tmp_path: Path, flag: str, value: str
) -> None:
    """Profile mode must have one unambiguous source for object geometry."""
    args = _parse(
        "--kvcache-shape-profile",
        str(_write_deepseek_profile(tmp_path)),
        flag,
        value,
    )

    with pytest.raises(SystemExit) as exc:
        run_l2_adapter_bench(MagicMock(), args)

    assert exc.value.code == 2


def test_default_raw_geometry_does_not_conflict_with_a_profile(
    tmp_path: Path,
) -> None:
    """Only a user-supplied --num-keys conflicts; the default must not."""
    args = _parse("--kvcache-shape-profile", str(_write_deepseek_profile(tmp_path)))

    assert args.num_keys == 32
    assert not getattr(args, "_explicit_raw_geometry", set())


def test_shape_spec_and_shape_profile_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    """The two flags are alternative spellings, so argparse rejects both."""
    with pytest.raises(SystemExit) as exc:
        _parse(
            "--kvcache-shape-spec",
            "(1,1024,256,1,576):uint8:61",
            "--kvcache-shape-profile",
            str(_write_deepseek_profile(tmp_path)),
        )

    assert exc.value.code == 2


def test_inline_shape_spec_matches_the_deepseek_profile_geometry() -> None:
    """The inline server grammar must resolve the same page as the YAML."""
    geometry = resolve_inline_shape_spec("(1,1024,256,1,576):uint8:61")

    # NB (1024) is the paged-KV pool's block count, not part of one page,
    # so it must not appear in the page size.
    assert geometry.page_size_bytes == 147456
    assert geometry.data_size_kb == 144
    assert geometry.objects_per_submit == 61
    assert geometry.tokens_per_chunk == 256
    assert geometry.task_size_bytes == 8994816
    # An inline spec has no file, so it records the spec instead.
    assert geometry.source_path == ""
    assert geometry.sha256 == ""
    assert geometry.shape_spec == "(1,1024,256,1,576):uint8:61"


def test_inline_shape_spec_sums_layers_across_uniform_groups() -> None:
    """Groups may differ in layer count as long as the page size matches."""
    geometry = resolve_inline_shape_spec(
        "(1,1024,256,1,576):uint8:30;(1,512,256,1,576):uint8:31"
    )

    assert geometry.objects_per_submit == 61
    assert geometry.page_size_bytes == 147456
    assert geometry.shape_spec == (
        "(1,1024,256,1,576):uint8:30;(1,512,256,1,576):uint8:31"
    )


@pytest.mark.parametrize(
    ("spec", "field"),
    [
        pytest.param("(0,1024,256,1,576):uint8:61", "kv_size", id="zero-kv-size"),
        # NB never reaches the page size, so only the explicit check
        # catches it -- and it is recorded as replayable provenance.
        pytest.param("(1,0,256,1,576):uint8:61", "NB", id="zero-num-blocks"),
        pytest.param("(1,-4,256,1,576):uint8:61", "NB", id="negative-num-blocks"),
        pytest.param("(1,1024,0,1,576):uint8:61", "BS", id="zero-block-size"),
        pytest.param("(1,1024,256,0,576):uint8:61", "NH", id="zero-heads"),
        pytest.param("(1,1024,256,1,0):uint8:61", "HS", id="zero-head-size"),
        pytest.param("(1,1024,256,1,-576):uint8:61", "HS", id="negative-head-size"),
    ],
)
def test_inline_shape_spec_rejects_non_positive_dimensions(
    spec: str, field: str
) -> None:
    """A zero page passes the KiB check, so reject the dimension instead."""
    with pytest.raises(GeometryProfileError, match=f"{field} must be positive"):
        resolve_inline_shape_spec(spec)


def test_inline_shape_spec_rejects_differing_block_sizes() -> None:
    """Equal page bytes do not imply equal BS, and BS sets tokens_per_chunk."""
    # Both groups are 147456 B: 256*1*576 and 128*2*576.
    with pytest.raises(GeometryProfileError, match="uniform block size"):
        resolve_inline_shape_spec(
            "(1,1024,256,1,576):uint8:30;(1,1024,128,2,576):uint8:31"
        )


def test_inline_shape_spec_records_the_spec_as_its_provenance(
    tmp_path: Path,
) -> None:
    """The run must be reproducible from the structured output alone."""
    command = _MetricsCommand()
    args = _parse(
        "--only",
        "store",
        "--key-prefix",
        "inline-spec",
        "--kvcache-shape-spec",
        "(1,1024,256,1,576):uint8:61",
        "--in-flight",
        "1",
        "--rounds",
        "1",
        "--warmup-rounds",
        "0",
        adapter_json=_fs_adapter_json(tmp_path),
    )

    run_l2_adapter_bench(command, args)

    assert command.metrics is not None
    geometry = command.metrics.to_dict()["metrics"]["geometry"]
    assert geometry == {
        "shape_spec": "(1,1024,256,1,576):uint8:61",
        "model_name": "inline-shape-spec",
        "tokens_per_chunk": 256,
        "objects_per_submit": 61,
        "page_size_bytes": 147456,
        "task_size_bytes": 8994816,
    }
    # The file-provenance form is absent, not empty.
    assert "profile_path" not in geometry
    assert "profile_sha256" not in geometry


def test_inline_shape_spec_rejects_heterogeneous_page_sizes() -> None:
    """bench l2 submits flat buffers, so mixed pages cannot be averaged."""
    with pytest.raises(GeometryProfileError, match="uniform page size"):
        resolve_inline_shape_spec(
            "(1,1024,256,1,576):uint8:61;(2,1024,256,8,128):uint8:2"
        )


def test_inline_shape_spec_rejects_non_kib_page() -> None:
    """--data-size-kb can express whole KiB only."""
    with pytest.raises(GeometryProfileError, match="multiple of 1024"):
        resolve_inline_shape_spec("(1,1024,1,1,1):uint8:1")


def test_inline_shape_spec_rejects_malformed_grammar() -> None:
    """A parse failure must surface as a geometry error, not a ValueError."""
    with pytest.raises(GeometryProfileError, match="invalid --kvcache-shape-spec"):
        resolve_inline_shape_spec("not-a-spec")


def test_mla_page_derives_from_lora_rank_and_rope_dim(tmp_path: Path) -> None:
    """MLA caches one shared latent, so kv_lora_rank + qk_rope_head_dim wins."""
    profile = tmp_path / "mla-components.yaml"
    profile.write_text(
        "\n".join(
            [
                "model: {name: test-mla}",
                (
                    "architecture: {num_layers: 61, attention: mla, "
                    "kv_lora_rank: 512, qk_rope_head_dim: 64}"
                ),
                "quantization: {dtype_bytes: 1}",
                "chunking: {tokens_per_chunk: 256}",
                "page: {page_size_bytes: 147456}",
                "burst: {layers_per_burst: 61}",
            ]
        )
    )

    geometry = resolve_geometry_profile(str(profile))

    assert geometry.page_size_bytes == 147456


def test_mla_page_cross_checks_a_redundant_declared_total(tmp_path: Path) -> None:
    """A declared cached_elems_per_token must agree with the components."""
    profile = tmp_path / "mla-mismatch.yaml"
    profile.write_text(
        "\n".join(
            [
                "model: {name: test-mla}",
                (
                    "architecture: {num_layers: 61, attention: mla, "
                    "kv_lora_rank: 512, qk_rope_head_dim: 64, "
                    "cached_elems_per_token: 640}"
                ),
                "quantization: {dtype_bytes: 1}",
                "chunking: {tokens_per_chunk: 256}",
                "page: {page_size_bytes: 147456}",
                "burst: {layers_per_burst: 61}",
            ]
        )
    )

    with pytest.raises(GeometryProfileError, match="cached_elems_per_token"):
        resolve_geometry_profile(str(profile))


def test_mla_page_requires_components_or_a_total(tmp_path: Path) -> None:
    """An MLA profile with no element source cannot be validated."""
    profile = tmp_path / "mla-empty.yaml"
    profile.write_text(
        "\n".join(
            [
                "model: {name: test-mla}",
                "architecture: {num_layers: 1, attention: mla}",
                "quantization: {dtype_bytes: 1}",
                "chunking: {tokens_per_chunk: 256}",
                "page: {page_size_bytes: 147456}",
                "burst: {layers_per_burst: 1}",
            ]
        )
    )

    with pytest.raises(GeometryProfileError, match="must declare kv_lora_rank"):
        resolve_geometry_profile(str(profile))


def test_kvcache_shape_profile_rejects_non_kib_page_before_adapter(
    tmp_path: Path,
) -> None:
    """The L2 CLI can express whole KiB only, so reject partial pages."""
    profile = tmp_path / "non-kib.yaml"
    profile.write_text(
        "\n".join(
            [
                "model: {name: test}",
                (
                    "architecture: {num_layers: 1, kv_size: 1, "
                    "num_kv_heads: 1, head_size: 1}"
                ),
                "quantization: {dtype_bytes: 1}",
                "chunking: {tokens_per_chunk: 1}",
                "page: {page_size_bytes: 1}",
                "burst: {layers_per_burst: 1, burst_bytes: 1}",
            ]
        )
    )
    args = _parse("--kvcache-shape-profile", str(profile))

    with pytest.raises(SystemExit) as exc:
        run_l2_adapter_bench(MagicMock(), args)

    assert exc.value.code == 2


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param("5:1", (5, 1), id="five-to-one"),
        pytest.param("1:1", (1, 1), id="one-to-one"),
        pytest.param("3:2", (3, 2), id="non-integer-ratio"),
    ],
)
def test_read_write_ratio_parser(value: str, expected: tuple[int, int]) -> None:
    assert _parse_read_write_ratio(value) == expected


@pytest.mark.parametrize("value", ["", "1", "1:", ":1", "1:2:3", "0:1", "1:0"])
def test_read_write_ratio_parser_rejects_invalid_values(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_read_write_ratio(value)


# ---------------------------------------------------------------------------
# Validation — all must fail before an adapter is constructed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--duration-sec", "-1"], id="negative-duration"),
        pytest.param(
            ["--duration-sec", "5", "--warmup-sec", "-1"], id="negative-warmup"
        ),
        pytest.param(
            ["--duration-sec", "5", "--only", "lookup"], id="sustained-lookup"
        ),
        pytest.param(
            ["--duration-sec", "5", "--no-skip-verify"], id="sustained-verify"
        ),
        pytest.param(
            ["--no-skip-verify", "--only", "load"], id="verify-with-only-load"
        ),
        pytest.param(
            ["--no-skip-verify", "--only", "store"], id="verify-with-only-store"
        ),
        pytest.param(["--warmup-sec", "5"], id="warmup-sec-without-duration"),
    ],
)
def test_invalid_combinations_exit_2(argv: list[str]) -> None:
    args = _parse(*argv)

    with pytest.raises(SystemExit) as exc:
        run_l2_adapter_bench(MagicMock(), args)

    assert exc.value.code == 2


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["--read-write-ratio", "5:1"], id="no-duration"),
        pytest.param(
            [
                "--duration-sec",
                "5",
                "--read-write-ratio",
                "5:1",
                "--only",
                "load",
            ],
            id="with-only",
        ),
        pytest.param(
            ["--duration-sec", "5", "--read-write-ratio", "5:1"],
            id="no-read-prefix",
        ),
        pytest.param(
            [
                "--duration-sec",
                "5",
                "--read-write-ratio",
                "5:1",
                "--key-prefix",
                "reads",
            ],
            id="no-write-prefix",
        ),
        pytest.param(
            [
                "--duration-sec",
                "5",
                "--read-write-ratio",
                "5:1",
                "--key-prefix",
                "shared",
                "--write-key-prefix",
                "shared",
            ],
            id="matching-prefixes",
        ),
        pytest.param(
            [
                "--duration-sec",
                "5",
                "--warmup-sec",
                "1",
                "--read-write-ratio",
                "5:1",
                "--key-prefix",
                "reads",
                "--write-key-prefix",
                "writes",
            ],
            id="warmup-writes",
        ),
    ],
)
def test_mixed_mode_invalid_combinations_exit_2(argv: list[str]) -> None:
    args = _parse(*argv)

    with pytest.raises(SystemExit) as exc:
        run_l2_adapter_bench(MagicMock(), args)

    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# End-to-end against the local fs adapter
# ---------------------------------------------------------------------------


def test_verify_runs_when_both_directions_are_present(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--no-skip-verify without --only must actually reach the gate.

    The complement of the rejection cases above: the gate is reachable,
    and its success line appears, so a passing run is distinguishable
    from one that silently skipped verification.
    """
    args = _parse(
        "--no-skip-verify",
        "--key-prefix",
        "verify",
        "--num-keys",
        "4",
        "--in-flight",
        "2",
        "--data-size-kb",
        "4",
        "--rounds",
        "1",
        "--warmup-rounds",
        "1",
        adapter_json=_fs_adapter_json(tmp_path),
    )

    run_l2_adapter_bench(MagicMock(), args)

    assert "All 8 keys data verified OK." in capsys.readouterr().out


def test_sustained_store_writes_a_distinct_file_per_submit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Sustained store keys must not wrap onto already-stored keys.

    ``fs_native`` short-circuits a store whose key already exists and
    reports success without writing, so a wrapped store window would
    measure the existence check while still counting payload bytes. The
    key space here (rounds=2 x in_flight=2 x num_keys=2 = 8 keys, i.e. 4
    submit slots) is far smaller than the number of submits a 0.3 s window
    completes, so a wrapping implementation would plateau at 8 files.
    """
    base = tmp_path / "l2"
    args = _parse(
        "--duration-sec",
        "0.3",
        "--key-prefix",
        "distinct-files",
        "--only",
        "store",
        "--num-keys",
        "2",
        "--in-flight",
        "2",
        "--data-size-kb",
        "4",
        "--rounds",
        "2",
        adapter_json=json.dumps({"type": "fs", "base_path": str(base)}),
    )

    run_l2_adapter_bench(MagicMock(), args)

    out = capsys.readouterr().out
    # Summary line: "  [Store] <n> submits in <t>s (drain tail ...)"
    summary = next(line for line in out.splitlines() if "submits in" in line)
    submits = int(summary.split("submits in")[0].split()[-1])
    files = [p for p in base.rglob("*") if p.is_file()]

    assert submits > 4, "window too short to exercise wrap-around"
    # One file per key, every key distinct.
    assert len(files) == submits * 2


def test_sustained_mode_records_per_submit_latencies(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A sustained window must complete submits and report a window."""
    args = _parse(
        "--duration-sec",
        "0.3",
        "--key-prefix",
        "latencies",
        "--only",
        "store",
        "--num-keys",
        "2",
        "--in-flight",
        "2",
        "--data-size-kb",
        "4",
        "--rounds",
        "8",
        adapter_json=_fs_adapter_json(tmp_path),
    )

    run_l2_adapter_bench(MagicMock(), args)

    out = capsys.readouterr().out
    assert "Sustained window for 0.3s" in out
    assert "submits in" in out


# ---------------------------------------------------------------------------
# Cross-invocation store keyspace
# ---------------------------------------------------------------------------


def _store_args(tmp_path: Path, *extra: str, prefix: str | None = None) -> object:
    argv = [
        "--only",
        "store",
        "--num-keys",
        "2",
        "--in-flight",
        "2",
        "--data-size-kb",
        "4",
        "--rounds",
        "1",
        "--warmup-rounds",
        "0",
        *extra,
    ]
    if prefix is not None:
        argv += ["--key-prefix", prefix]
    return _parse(*argv, adapter_json=_fs_adapter_json(tmp_path))


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param((), id="rounds"),
        pytest.param(("--duration-sec", "0.2"), id="sustained"),
    ],
)
def test_a_store_requires_a_key_prefix(tmp_path: Path, extra: tuple[str, ...]) -> None:
    """Every store run must be named explicitly, in EITHER mode.

    Keys restart at index 0 each invocation, so an unprefixed store shares
    a key universe with every other unprefixed store. The pre-flight probe
    catches the sequential case but is not a reservation -- two concurrent
    producers can both find the default namespace empty and then write the
    same keys. Requiring the prefix is what actually keeps them disjoint.
    """
    args = _store_args(tmp_path, *extra)

    with pytest.raises(SystemExit) as exc:
        run_l2_adapter_bench(MagicMock(), args)

    assert exc.value.code == 2


def test_unsafe_shared_key_prefix_overrides_the_requirement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The escape hatch must actually reach the benchmark.

    Reproducing a historical unprefixed corpus is a legitimate need, so
    the guard is overridable -- but only by naming the risk.
    """
    args = _store_args(tmp_path, "--unsafe-shared-key-prefix")

    run_l2_adapter_bench(MagicMock(), args)

    assert "Store" in capsys.readouterr().out


def test_load_does_not_require_a_key_prefix(tmp_path: Path) -> None:
    """The requirement is store-only: a load consumes no new capacity.

    Guards the validation against over-reach -- a sustained load pass over
    an already-prepopulated corpus must still be runnable without one.
    """
    args = _parse(
        "--only",
        "load",
        "--duration-sec",
        "0.2",
        "--num-keys",
        "2",
        "--in-flight",
        "2",
        "--data-size-kb",
        "4",
        "--rounds",
        "1",
        "--warmup-rounds",
        "0",
        adapter_json=_fs_adapter_json(tmp_path),
    )

    # Reaches the run rather than exiting 2 during validation.
    run_l2_adapter_bench(MagicMock(), args)


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param((), id="rounds"),
        pytest.param(("--duration-sec", "0.2"), id="sustained"),
    ],
)
def test_repeated_store_run_is_rejected(tmp_path: Path, extra: tuple[str, ...]) -> None:
    """A second store run into the same keyspace must not report success.

    ``fs_native`` short-circuits a store whose file exists and reports
    success without writing, and the harness counts that as all keys
    transferred -- so the second run would advertise a full write rate
    having written nothing. Keys restart at index 0 every invocation, so
    this is the default outcome of reusing a prefix, in both modes.
    """
    run_l2_adapter_bench(MagicMock(), _store_args(tmp_path, *extra, prefix="run-a"))

    with pytest.raises(SystemExit) as exc:
        run_l2_adapter_bench(MagicMock(), _store_args(tmp_path, *extra, prefix="run-a"))

    assert exc.value.code == 2


def test_a_fresh_key_prefix_allows_a_second_store_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--key-prefix gives an independent key universe on the same store.

    The escape hatch for the rejection above: same backing path, disjoint
    keys, so every submit is a physical write again.
    """
    run_l2_adapter_bench(MagicMock(), _store_args(tmp_path, prefix="run-a"))
    capsys.readouterr()

    run_l2_adapter_bench(MagicMock(), _store_args(tmp_path, prefix="run-b"))

    # Reached the report rather than exiting: the run actually measured.
    assert "Store" in capsys.readouterr().out


def test_store_then_load_still_shares_the_keyspace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The prefix guard must not break prepopulate-then-load.

    ``--only store`` followed by ``--only load`` at the same geometry is a
    supported workflow and depends on both runs deriving identical keys,
    which means passing the SAME --key-prefix to both. The guard fires on
    repeated *stores* only, so the load must still hit.
    """
    run_l2_adapter_bench(MagicMock(), _store_args(tmp_path, prefix="pair"))
    capsys.readouterr()

    load_args = _parse(
        "--only",
        "load",
        "--key-prefix",
        "pair",
        "--num-keys",
        "2",
        "--in-flight",
        "2",
        "--data-size-kb",
        "4",
        "--rounds",
        "1",
        "--warmup-rounds",
        "0",
        adapter_json=_fs_adapter_json(tmp_path),
    )
    run_l2_adapter_bench(MagicMock(), load_args)

    out = capsys.readouterr().out
    # All 4 keys hit: the load found what the store wrote.
    assert "Load" in out
    assert "0/4" not in out


def test_mixed_sustained_run_uses_distinct_read_and_write_prefixes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mixed mode loads a prepopulated corpus and writes another namespace."""
    prepopulate = _parse(
        "--only",
        "store",
        "--key-prefix",
        "mixed-read",
        "--num-keys",
        "1",
        "--in-flight",
        "6",
        "--data-size-kb",
        "4",
        "--rounds",
        "2",
        adapter_json=_fs_adapter_json(tmp_path),
    )
    run_l2_adapter_bench(MagicMock(), prepopulate)
    capsys.readouterr()

    args = _parse(
        "--duration-sec",
        "0.3",
        "--read-write-ratio",
        "5:1",
        "--no-skip-verify",
        "--key-prefix",
        "mixed-read",
        "--write-key-prefix",
        "mixed-write",
        "--num-keys",
        "1",
        "--in-flight",
        "6",
        "--data-size-kb",
        "4",
        "--rounds",
        "2",
        "--warmup-rounds",
        "0",
        adapter_json=_fs_adapter_json(tmp_path),
    )

    run_l2_adapter_bench(MagicMock(), args)

    out = capsys.readouterr().out
    ratio_line = next(
        line
        for line in out.splitlines()
        if "successful read:write payload ratio" in line
    )
    achieved_ratio = float(ratio_line.split("ratio ")[1].split()[0])
    assert achieved_ratio == pytest.approx(5.0, rel=0.01)


def test_mixed_sustained_rejects_an_existing_write_prefix(tmp_path: Path) -> None:
    """The mixed freshness probe must check stores, not the read corpus."""
    run_l2_adapter_bench(MagicMock(), _store_args(tmp_path, prefix="mixed-write"))

    args = _parse(
        "--duration-sec",
        "0.2",
        "--read-write-ratio",
        "5:1",
        "--key-prefix",
        "mixed-read",
        "--write-key-prefix",
        "mixed-write",
        "--num-keys",
        "1",
        "--in-flight",
        "2",
        "--data-size-kb",
        "4",
        "--rounds",
        "1",
        "--warmup-rounds",
        "0",
        adapter_json=_fs_adapter_json(tmp_path),
    )

    with pytest.raises(SystemExit) as exc:
        run_l2_adapter_bench(MagicMock(), args)

    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# Warmup stripping
# ---------------------------------------------------------------------------


def _rounds_result(rounds: int, in_flight: int = 2) -> BenchResult:
    result = BenchResult(
        operation="Load",
        in_flight=in_flight,
        num_keys=4,
        data_size_bytes=_MB,
    )
    for r in range(rounds):
        result.round_starts.append(float(r))
        result.round_durations.append(0.1)
        result.success_counts.append(in_flight * 4)
        for i in range(in_flight):
            # Encode round and submit so the survivors are identifiable.
            result.submit_latencies.append(r + i / 100.0)
        result.round_latency_counts.append(in_flight)
    return result


def test_strip_warmup_drops_matching_latencies() -> None:
    result = _rounds_result(rounds=5, in_flight=2)

    kept = _strip_warmup(result, warmup=2)

    assert len(kept.round_durations) == 3
    assert len(kept.round_starts) == 3
    assert len(kept.submit_latencies) == 6
    # First surviving latency belongs to round 2, submit 0.
    assert kept.submit_latencies[0] == pytest.approx(2.0)
    assert kept.round_latency_counts == [2, 2, 2]


def test_strip_warmup_handles_a_short_warmup_round() -> None:
    """A timed-out warmup round contributes fewer than in_flight entries."""
    result = _rounds_result(rounds=3, in_flight=4)
    # Simulate round 0 completing only 1 of 4 submits.
    result.submit_latencies = [0.0] + result.submit_latencies[4:]
    result.round_latency_counts[0] = 1

    kept = _strip_warmup(result, warmup=1)

    # 8 entries survive (rounds 1 and 2), and the single warmup entry is
    # dropped -- not four, which would have eaten real data.
    assert len(kept.submit_latencies) == 8
    assert kept.submit_latencies[0] == pytest.approx(1.0)


def test_strip_warmup_preserves_mode_and_timeout_flag() -> None:
    result = _rounds_result(rounds=2)
    result.timed_out = True

    kept = _strip_warmup(result, warmup=1)

    assert kept.mode is result.mode
    assert kept.timed_out is True
