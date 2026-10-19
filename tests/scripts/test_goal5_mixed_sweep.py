# SPDX-License-Identifier: Apache-2.0
"""Tests for the capacity-gated 5:1 load-curve matrix."""

from __future__ import annotations

# Standard
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

# Third Party
import pytest


SCRIPT = Path(__file__).parents[2] / "scripts/ipu-poc/goal5_mixed_sweep.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("goal5_mixed_sweep", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_matrix_covers_both_models_and_all_in_flight_settings() -> None:
    """Each model has three independent cells at each intended offered load."""
    sweep = _load_module()
    cells = sweep.cells()

    assert len(cells) == 24
    assert len(set(cells)) == 24
    for model in sweep.MODELS:
        assert {
            (in_flight, repetition)
            for cell_model, in_flight, repetition in cells
            if cell_model == model
        } == {
            (in_flight, repetition)
            for in_flight in (1, 2, 4, 8)
            for repetition in (1, 2, 3)
        }


def test_middle_repetition_reverses_in_flight_order() -> None:
    """Balance a time trend against the in-flight sweep order."""
    sweep = _load_module()
    plan = sweep.cells((1, 2, 4, 8, 16, 32))

    for model in sweep.MODELS:
        for repetition, expected in (
            (1, (1, 2, 4, 8, 16, 32)),
            (2, (32, 16, 8, 4, 2, 1)),
            (3, (1, 2, 4, 8, 16, 32)),
        ):
            actual = tuple(
                in_flight
                for cell_model, in_flight, cell_repetition in plan
                if cell_model == model and cell_repetition == repetition
            )
            assert actual == expected


def test_failed_short_window_canary_stops_before_matrix(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A materially biased 30s mixed result must not spend the write budget."""
    sweep = _load_module()
    calls: list[tuple[str, int, int]] = []
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "valid": True,
                "counter_deltas": {
                    "app_load_gbps_decimal": 1000,
                    "app_store_gbps_decimal": 200,
                },
            }
        )
    )

    def fake_run_cell(
        model: str,
        in_flight: int,
        repetition: int,
        run_id: str,
        directory: Path,
        fleet_script: Path,
    ) -> dict[str, object]:
        calls.append((model, in_flight, repetition))
        return {
            "counter_deltas": {
                "app_load_gbps_decimal": 900,
                "app_store_gbps_decimal": 180,
            },
            "storage_free_bytes_after_mixed": {
                host: 2_000_000_000_000 for host in sweep.HOSTS
            },
        }

    monkeypatch.setattr(sweep, "run_cell", fake_run_cell)
    monkeypatch.setattr(
        sweep,
        "_reclaim_cell",
        lambda *args: pytest.fail("failed canary must retain its Store files"),
        raising=False,
    )
    root = tmp_path / "goal5-mixed-curves-20261001-000000"

    with pytest.raises(RuntimeError, match="30s mixed window differs"):
        sweep.execute(
            root.name, root, tmp_path / "fleet.py", baseline, reclaim_writes=True
        )

    assert calls == [("minimax", 8, 0)]
    status = json.loads((root / "status.json").read_text())
    assert status["state"] == "failed"
    assert status["current"] == "minimax-if8-rep0"


def test_extension_matrix_covers_if16_and_if32() -> None:
    """The extension runs both models three times at each higher IF."""
    sweep = _load_module()

    cells = sweep.cells((16, 32))

    assert len(cells) == 12
    assert len(set(cells)) == 12
    assert {(model, in_flight) for model, in_flight, _ in cells} == {
        ("minimax", 16),
        ("minimax", 32),
        ("mixtral", 16),
        ("mixtral", 32),
    }


def test_reclaim_requires_exact_count_and_bytes_before_unlink(tmp_path: Path) -> None:
    """Only exact fresh Store files are removed after both totals match."""
    sweep = _load_module()
    prefix = "goal5-mixed-curves-20261001-111000-minimax-if16-rep1-mmgi0-138e44c4c604"
    written = [
        tmp_path / f"{prefix}-bench-model@a.data",
        tmp_path / f"{prefix}-bench-model@b.data",
    ]
    written[0].write_bytes(b"abc")
    written[1].write_bytes(b"defg")
    corpus = tmp_path / "minimaxqp-20260924-0142-138e44c4c604-bench-model@a.data"
    corpus.write_bytes(b"keep")
    other_cell = tmp_path / (
        "goal5-mixed-curves-20261001-111000-minimax-if32-rep1-"
        "mmgi0-138e44c4c604-bench-model@a.data"
    )
    other_cell.write_bytes(b"keep")

    def reclaim(
        count: int, byte_count: int, mode: str
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                "-c",
                sweep.RECLAIM_PYTHON,
                str(tmp_path),
                prefix,
                str(count),
                str(byte_count),
                mode,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    assert reclaim(2, 7, "verify").returncode == 0
    assert all(path.exists() for path in written)
    assert reclaim(1, 7, "remove").returncode != 0
    assert reclaim(2, 6, "remove").returncode != 0
    assert all(path.exists() for path in written)
    assert reclaim(2, 7, "remove").returncode == 0
    assert not any(path.exists() for path in written)
    assert corpus.exists()
    assert other_cell.exists()


def test_reclaim_refuses_orphan_temp_file(tmp_path: Path) -> None:
    """An unfinished Store keeps the entire cell's files for diagnosis."""
    sweep = _load_module()
    prefix = "goal5-mixed-curves-20261001-111000-mixtral-if16-rep1-mmgi0-406f93535503"
    data = tmp_path / f"{prefix}-bench-model@a.data"
    temp = tmp_path / f"{prefix}-bench-model@b.tmp"
    data.write_bytes(b"abc")
    temp.write_bytes(b"unfinished")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            sweep.RECLAIM_PYTHON,
            str(tmp_path),
            prefix,
            "1",
            "3",
            "remove",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert data.exists()
    assert temp.exists()


def test_reclaim_request_rejects_wrong_store_namespace() -> None:
    """A host result from another cell cannot authorize file deletion."""
    sweep = _load_module()
    prefix = "goal5-mixed-curves-20261001-111000-minimax-if16-rep1-mmgi0-138e44c4c604"
    base = "/mnt/lmcache/bench-l2/minimaxqp-20260924-0142/dell-r760-poc-003"
    fleet = {
        "host_results": {
            "mmgi0": {
                "operations": {
                    "Store": {"total_success": 2, "total_success_bytes": 8192}
                }
            }
        }
    }
    host = {
        "metrics": {
            "config": {
                "write_key_prefix": prefix,
                "l2_adapter_json": json.dumps(
                    {"type": "fs_native", "base_path": base, "use_odirect": True}
                ),
            },
            "geometry": {"profile_sha256": "138e44c4c604" + "0" * 52},
            "op_0": {"operation": "Load"},
            "op_1": {
                "operation": "Store",
                "total_keys": 2,
                "total_success": 2,
                "total_success_bytes": 8192,
            },
        }
    }

    assert sweep._reclaim_request(
        fleet,
        host,
        "goal5-mixed-curves-20261001-111000",
        "minimax-if16-rep1",
        "minimax",
        "mmgi0",
    ) == ["minimaxqp-20260924-0142", base, prefix, "2", "8192"]
    host["metrics"]["config"]["write_key_prefix"] = prefix.replace("if16", "if32")
    with pytest.raises(ValueError, match="write prefix"):
        sweep._reclaim_request(
            fleet,
            host,
            "goal5-mixed-curves-20261001-111000",
            "minimax-if16-rep1",
            "minimax",
            "mmgi0",
        )


def test_canary_only_records_reclaim_without_starting_extension(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The cleanup canary can be inspected before spending the matrix budget."""
    sweep = _load_module()
    calls: list[tuple[str, int, int]] = []
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps(
            {
                "valid": True,
                "counter_deltas": {
                    "app_load_gbps_decimal": 1000,
                    "app_store_gbps_decimal": 200,
                },
            }
        )
    )

    def fake_run_cell(
        model: str,
        in_flight: int,
        repetition: int,
        run_id: str,
        directory: Path,
        fleet_script: Path,
    ) -> dict[str, object]:
        calls.append((model, in_flight, repetition))
        return {
            "counter_deltas": {
                "app_load_gbps_decimal": 1000,
                "app_store_gbps_decimal": 200,
            },
            "storage_free_bytes_after_mixed": {
                host: 1_600_000_000_000 for host in sweep.HOSTS
            },
        }

    def fake_reclaim_cell(*args: object) -> dict[str, object]:
        result = args[-1]
        assert isinstance(result, dict)
        result["storage_free_bytes_after_reclaim"] = {
            host: 1_800_000_000_000 for host in sweep.HOSTS
        }
        return result

    monkeypatch.setattr(sweep, "run_cell", fake_run_cell)
    monkeypatch.setattr(sweep, "_reclaim_cell", fake_reclaim_cell, raising=False)
    root = tmp_path / "goal5-mixed-curves-20261001-111001"

    sweep.execute(
        root.name,
        root,
        tmp_path / "fleet.py",
        baseline,
        in_flights=(16, 32),
        reclaim_writes=True,
        canary_only=True,
    )

    assert calls == [("minimax", 8, 0)]
    status = json.loads((root / "status.json").read_text())
    assert status["state"] == "complete"
    assert status["completed"]["minimax-if8-rep0"]["storage_free_bytes_after"] == {
        host: 1_800_000_000_000 for host in sweep.HOSTS
    }
