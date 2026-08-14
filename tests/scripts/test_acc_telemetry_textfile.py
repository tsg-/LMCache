# SPDX-License-Identifier: Apache-2.0
"""Tests for the ACC telemetry node_exporter textfile collector."""

from __future__ import annotations

# Standard
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

# Third Party
import pytest


SCRIPT = (
    Path(__file__).parents[2]
    / "docs/design/v1/platform/ipu-poc/instrumentation/host/bin"
    / "acc_telemetry_textfile.py"
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("acc_telemetry_textfile", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


collector = _load_module()


def test_render_metrics_exports_both_rc_byte_counters() -> None:
    """Render both ACC RC byte counters as Prometheus counters."""
    rendered = collector.render_metrics(
        {"bytes_from_ulp_rc": 123, "bytes_to_ulp": 456},
        success=True,
        timestamp=100,
    )

    assert "# TYPE acc_telemetry_bytes_total counter" in rendered
    assert (
        'acc_telemetry_bytes_total{counter="bytes_from_ulp_rc",ulp="rdma"} 123'
        in rendered
    )
    assert (
        'acc_telemetry_bytes_total{counter="bytes_to_ulp",ulp="rdma"} 456' in rendered
    )
    assert "acc_telemetry_collector_success 1" in rendered
    assert "acc_telemetry_collector_timestamp_seconds 100" in rendered


def test_render_failure_has_no_stale_counters() -> None:
    """A failed ACC query publishes only freshness and failure signals."""
    rendered = collector.render_metrics({}, success=False, timestamp=100)

    assert "acc_telemetry_collector_success 0" in rendered
    assert "acc_telemetry_bytes_total" not in rendered


def test_write_textfile_replaces_old_counters_after_failure(tmp_path: Path) -> None:
    """A failed collection must replace, rather than preserve, prior counters."""
    collector.write_textfile(
        tmp_path,
        collector.render_metrics(
            {"bytes_from_ulp_rc": 123, "bytes_to_ulp": 456},
            success=True,
            timestamp=100,
        ),
    )
    collector.write_textfile(
        tmp_path,
        collector.render_metrics({}, success=False, timestamp=101),
    )

    rendered = (tmp_path / collector.OUTPUT_FILENAME).read_text(encoding="ascii")

    assert "acc_telemetry_collector_success 0" in rendered
    assert "acc_telemetry_bytes_total" not in rendered


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"bytes_from_ulp_rc": 1},
        {"bytes_from_ulp_rc": -1, "bytes_to_ulp": 2},
        {"bytes_from_ulp_rc": "1", "bytes_to_ulp": 2},
    ],
)
def test_validate_counters_rejects_incomplete_or_invalid_values(
    values: dict[str, object],
) -> None:
    """Both ACC counters must be non-negative integers."""
    with pytest.raises(ValueError):
        collector.validate_counters(values)


def test_validate_counters_accepts_zero_values() -> None:
    """An idle link may legitimately return zero for either counter."""
    values = collector.validate_counters({"bytes_from_ulp_rc": 0, "bytes_to_ulp": 0})

    assert values == {"bytes_from_ulp_rc": 0, "bytes_to_ulp": 0}
