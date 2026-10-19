# SPDX-License-Identifier: Apache-2.0
"""Tests for the N-initiator model-page-geometry aggregate reporter."""

from __future__ import annotations

# Standard
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

# Third Party
import pytest

SCRIPT = Path(__file__).parents[2] / "scripts/ipu-poc/geom_multi_report.py"

# DeepSeek-V3 model page: 61 objects x 144 KiB.
PAGE_KB = 144
PAGE_BYTES = PAGE_KB * 1024
OBJECTS_PER_SUBMIT = 61
# Measured at this geometry; the 28 MiB constant (52428) is 13.5% wrong here.
READ_SEG = 45369.8
WRITE_SEG = 4096.0
# Stand-in for the resolved profile digest; the gate compares it verbatim, so
# only its length (64 hex digits) has to be realistic.
PROFILE_SHA = "a" * 64


def _geometry_section() -> dict[str, object]:
    """Return the geometry provenance the bench emits under a profile."""
    return {
        "profile_path": "scripts/ipu-poc/models/deepseek_v3_fp8.yaml",
        "profile_sha256": PROFILE_SHA,
        "model_name": "deepseek-v3",
        "objects_per_submit": OBJECTS_PER_SUBMIT,
        "page_size_bytes": PAGE_BYTES,
    }


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("geom_multi_report", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reporter = _load_module()


def _zero_error_counters() -> dict[str, int]:
    """Return the explicit zero-valued fabric error deltas a run requires."""
    return {counter: 0 for counter in reporter._ERROR_COUNTERS}


def _mib_s(byte_rate: float) -> float:
    """Convert a byte rate to the MiB/s the bench reports."""
    return byte_rate / 2**20


def _write_read_initiator(
    run_dir: Path,
    worker_id: int,
    start: float,
    window: float = 30.0,
    pages: int = 6100,
    reported_mib_s: float | None = None,
) -> None:
    """Write one read-only initiator's artifacts."""
    if reported_mib_s is None:
        reported_mib_s = _mib_s(pages * PAGE_BYTES / window)
    metrics = {
        "metrics": {
            "config": {"mode": "sustained", "num_keys": OBJECTS_PER_SUBMIT},
            "geometry": _geometry_section(),
            "op_0": {
                "operation": "Load",
                "window_sec": window,
                "total_keys": pages,
                "total_success": pages,
                "throughput_aggregate_mbps": reported_mib_s,
                "submit_latency_avg_ms": 6.0,
            },
        }
    }
    (run_dir / f"initiator-{worker_id}.json").write_text(json.dumps(metrics))
    (run_dir / f"initiator-{worker_id}.status").write_text("0\n")
    (run_dir / f"initiator-{worker_id}.log").write_text(
        f"{start:.3f} Sustained window for {window}s at 8 total in flight\n"
    )


def _write_mixed_initiator(
    run_dir: Path,
    worker_id: int,
    start: float,
    window: float = 30.0,
    read_pages: int = 6100,
    write_pages: int = 1220,
) -> None:
    """Write one mixed initiator's artifacts at a 5:1 byte ratio."""
    read_bytes = read_pages * PAGE_BYTES
    write_bytes = write_pages * PAGE_BYTES
    metrics = {
        "metrics": {
            "config": {
                "mode": "sustained",
                "num_keys": OBJECTS_PER_SUBMIT,
                "read_write_ratio_requested": "5:1",
                "write_key_prefix": f"write-{worker_id}",
            },
            "geometry": _geometry_section(),
            "op_0": {
                "operation": "Load",
                "window_sec": window,
                "total_keys": read_pages,
                "total_success": read_pages,
                "throughput_aggregate_mbps": _mib_s(read_bytes / window),
            },
            "op_1": {
                "operation": "Store",
                "window_sec": window,
                "total_keys": write_pages,
                "total_success": write_pages,
            },
            "mixed": {
                "read_success_bytes": read_bytes,
                "write_success_bytes": write_bytes,
                "read_write_ratio_achieved": read_bytes / write_bytes,
            },
        }
    }
    (run_dir / f"initiator-{worker_id}.json").write_text(json.dumps(metrics))
    (run_dir / f"initiator-{worker_id}.status").write_text("0\n")
    (run_dir / f"initiator-{worker_id}.log").write_text(
        f"{start:.3f} [Mixed] Sustained window for {window}s at 8 total in flight\n"
    )


def _write_poll(
    path: Path, read_ops_per_sec: float, write_ops_per_sec: float, seconds: int = 40
) -> None:
    """Write a counter poll whose slopes are exactly the given rates."""
    lines = [
        f"{100 + t:.3f} {int(t * read_ops_per_sec)} {int(t * write_ops_per_sec)}\n"
        for t in range(seconds)
    ]
    path.write_text("".join(lines))


def _build(run_dir: Path, poll: Path, initiators: int, mode: str, **kwargs):
    """Call build_report with this geometry's defaults."""
    params: dict[str, object] = dict(
        run_dir=run_dir,
        expected_initiators=initiators,
        mode=mode,
        page_kb=PAGE_KB,
        poll_path=poll,
        read_seg_bytes=READ_SEG,
        write_seg_bytes=WRITE_SEG,
        expected_ratio=5.0,
        trim_sec=3.0,
        max_start_skew_sec=2.0,
        ratio_tolerance=0.01,
        counter_tolerance=0.05,
        counters=_zero_error_counters(),
        corpus_before=292800,
        corpus_after=292800,
        expected_profile_sha256=PROFILE_SHA,
        expected_page_size_bytes=PAGE_BYTES,
        expected_objects_per_submit=OBJECTS_PER_SUBMIT,
    )
    params.update(kwargs)
    return reporter.build_report(**params)


def test_accepts_four_read_initiators(tmp_path: Path) -> None:
    for worker_id in range(4):
        _write_read_initiator(tmp_path, worker_id, start=100.0)
    poll = tmp_path / "poll.txt"
    # Four initiators each moving 6100 pages in 10 s.
    read_rate = 4 * 6100 * PAGE_BYTES / 30.0
    _write_poll(poll, read_rate / READ_SEG, 0.0)

    report = _build(tmp_path, poll, 4, "read")

    assert report["accepted"], report["acceptance_failures"]
    assert report["aggregate_read_gbps"] == pytest.approx(read_rate * 8 / 1e9)
    # Read-only cells must not claim a write direction at all.
    assert "write" not in report["counter_correlation"]["directions"]
    assert report["counter_correlation"]["directions"]["read"][
        "ratio"
    ] == pytest.approx(1.0, abs=0.01)


def test_read_aggregate_scales_with_initiator_count(tmp_path: Path) -> None:
    """Aggregate read rate is the sum of per-initiator rates, not an average."""
    one = tmp_path / "one"
    two = tmp_path / "two"
    for path, count in ((one, 1), (two, 2)):
        path.mkdir()
        for worker_id in range(count):
            _write_read_initiator(path, worker_id, start=100.0)
        _write_poll(path / "poll.txt", count * 6100 * PAGE_BYTES / 30.0 / READ_SEG, 0.0)

    r_one = _build(one, one / "poll.txt", 1, "read")
    r_two = _build(two, two / "poll.txt", 2, "read")

    assert r_one["accepted"] and r_two["accepted"]
    assert r_two["aggregate_read_gbps"] == pytest.approx(
        2 * r_one["aggregate_read_gbps"]
    )


def test_accepts_mixed_at_five_to_one(tmp_path: Path) -> None:
    for worker_id in range(2):
        _write_mixed_initiator(tmp_path, worker_id, start=100.0)
    poll = tmp_path / "poll.txt"
    read_rate = 2 * 6100 * PAGE_BYTES / 30.0
    write_rate = 2 * 1220 * PAGE_BYTES / 30.0
    _write_poll(poll, read_rate / READ_SEG, write_rate / WRITE_SEG)

    report = _build(tmp_path, poll, 2, "mixed")

    assert report["accepted"], report["acceptance_failures"]
    assert report["achieved_read_write_ratio"] == pytest.approx(5.0)
    assert report["aggregate_write_gbps"] == pytest.approx(write_rate * 8 / 1e9)
    # Both directions get their own segment constant, and they differ.
    directions = report["counter_correlation"]["directions"]
    assert directions["read"]["segment_bytes"] == READ_SEG
    assert directions["write"]["segment_bytes"] == WRITE_SEG


def test_rejects_ratio_drift(tmp_path: Path) -> None:
    for worker_id in range(2):
        _write_mixed_initiator(tmp_path, worker_id, start=100.0, write_pages=2000)
    poll = tmp_path / "poll.txt"
    _write_poll(poll, 1000.0, 100.0)

    report = _build(tmp_path, poll, 2, "mixed")

    assert not report["accepted"]
    assert "achieved ratio" in " ".join(report["acceptance_failures"])


def test_rejects_start_skew_beyond_limit(tmp_path: Path) -> None:
    """Initiators that did not enter their windows together are not a cell."""
    _write_read_initiator(tmp_path, 0, start=100.0)
    _write_read_initiator(tmp_path, 1, start=105.0)
    poll = tmp_path / "poll.txt"
    _write_poll(poll, 1000.0, 0.0)

    report = _build(tmp_path, poll, 2, "read")

    assert not report["accepted"]
    assert "skew" in " ".join(report["acceptance_failures"])


def test_rejects_reported_versus_derived_goodput_gap(tmp_path: Path) -> None:
    """A units or window change shows up as the two derivations diverging."""
    _write_read_initiator(tmp_path, 0, start=100.0)
    # Overstate the reported figure by 10% while bytes and window stay honest.
    _write_read_initiator(
        tmp_path,
        1,
        start=100.0,
        reported_mib_s=_mib_s(6100 * PAGE_BYTES / 30.0) * 1.10,
    )
    poll = tmp_path / "poll.txt"
    _write_poll(poll, 1000.0, 0.0)

    report = _build(tmp_path, poll, 2, "read")

    assert not report["accepted"]
    assert "derived" in " ".join(report["acceptance_failures"])


def test_rejects_missing_initiator(tmp_path: Path) -> None:
    _write_read_initiator(tmp_path, 0, start=100.0)
    poll = tmp_path / "poll.txt"
    _write_poll(poll, 1000.0, 0.0)

    report = _build(tmp_path, poll, 4, "read")

    assert not report["accepted"]
    assert any("missing result" in f for f in report["acceptance_failures"])


def test_rejects_changed_read_corpus(tmp_path: Path) -> None:
    """A read cell that added objects wrote where it must not have."""
    _write_read_initiator(tmp_path, 0, start=100.0)
    poll = tmp_path / "poll.txt"
    _write_poll(poll, 1000.0, 0.0)

    report = _build(tmp_path, poll, 1, "read", corpus_after=292801)

    assert not report["accepted"]
    assert any("corpus changed" in f for f in report["acceptance_failures"])


def test_rejects_nonzero_fabric_error(tmp_path: Path) -> None:
    _write_read_initiator(tmp_path, 0, start=100.0)
    poll = tmp_path / "poll.txt"
    _write_poll(poll, 1000.0, 0.0)
    counters = _zero_error_counters()
    counters["RetransSegs"] = 4

    report = _build(tmp_path, poll, 1, "read", counters=counters)

    assert not report["accepted"]
    assert any("RetransSegs advanced" in f for f in report["acceptance_failures"])


def test_rejects_missing_error_counter_delta(tmp_path: Path) -> None:
    """An absent counter is a gap in evidence, not an implicit zero."""
    _write_read_initiator(tmp_path, 0, start=100.0)
    poll = tmp_path / "poll.txt"
    _write_poll(poll, 1000.0, 0.0)
    counters = _zero_error_counters()
    del counters["RTO"]

    report = _build(tmp_path, poll, 1, "read", counters=counters)

    assert not report["accepted"]
    assert any(
        "missing error-counter delta" in f for f in report["acceptance_failures"]
    )


def test_rejects_absent_geometry_provenance(tmp_path: Path) -> None:
    """Without num_keys the run cannot be shown to have used the profile."""
    _write_read_initiator(tmp_path, 0, start=100.0)
    path = tmp_path / "initiator-0.json"
    payload = json.loads(path.read_text())
    del payload["metrics"]["config"]["num_keys"]
    path.write_text(json.dumps(payload))
    poll = tmp_path / "poll.txt"
    _write_poll(poll, 1000.0, 0.0)

    report = _build(tmp_path, poll, 1, "read")

    assert not report["accepted"]
    assert any("config.num_keys" in f for f in report["acceptance_failures"])


def test_rejects_absent_geometry_section(tmp_path: Path) -> None:
    """No geometry section means the flat shape ran, whatever num_keys says."""
    _write_read_initiator(tmp_path, 0, start=100.0)
    path = tmp_path / "initiator-0.json"
    payload = json.loads(path.read_text())
    del payload["metrics"]["geometry"]
    path.write_text(json.dumps(payload))
    poll = tmp_path / "poll.txt"
    _write_poll(poll, 1000.0, 0.0)

    report = _build(tmp_path, poll, 1, "read")

    assert not report["accepted"]
    assert any("geometry profile sha256" in f for f in report["acceptance_failures"])


def test_rejects_geometry_from_a_different_profile(tmp_path: Path) -> None:
    """A same-shaped different profile must not pass on shape alone."""
    _write_read_initiator(tmp_path, 0, start=100.0)
    path = tmp_path / "initiator-0.json"
    payload = json.loads(path.read_text())
    payload["metrics"]["geometry"]["profile_sha256"] = "b" * 64
    path.write_text(json.dumps(payload))
    poll = tmp_path / "poll.txt"
    _write_poll(poll, 1000.0, 0.0)

    report = _build(tmp_path, poll, 1, "read")

    assert not report["accepted"]
    assert any("geometry profile sha256" in f for f in report["acceptance_failures"])


def test_rejects_geometry_page_size_mismatch(tmp_path: Path) -> None:
    """The corpus was laid out at one page size; a cell at another is not it."""
    _write_read_initiator(tmp_path, 0, start=100.0)
    path = tmp_path / "initiator-0.json"
    payload = json.loads(path.read_text())
    payload["metrics"]["geometry"]["page_size_bytes"] = PAGE_BYTES * 2
    path.write_text(json.dumps(payload))
    poll = tmp_path / "poll.txt"
    _write_poll(poll, 1000.0, 0.0)

    report = _build(tmp_path, poll, 1, "read")

    assert not report["accepted"]
    assert any("geometry page" in f for f in report["acceptance_failures"])


def test_rejects_page_kb_disagreeing_with_expected_page_bytes(tmp_path: Path) -> None:
    """page_kb drives app bytes and page_size_bytes drives the gate; both must
    describe one object, so a caller mismatch is an error, not a warning."""
    poll = tmp_path / "poll.txt"
    _write_poll(poll, 1000.0, 0.0)

    with pytest.raises(ValueError, match="does not match expected page"):
        _build(tmp_path, poll, 1, "read", expected_page_size_bytes=PAGE_BYTES * 2)


def test_counter_mismatch_is_reported_but_does_not_reject(tmp_path: Path) -> None:
    """A slope that disagrees corroborates weakly; it is still not a gate."""
    _write_read_initiator(tmp_path, 0, start=100.0)
    poll = tmp_path / "poll.txt"
    # Half the ops the application bytes imply.
    _write_poll(poll, 6100 * PAGE_BYTES / 30.0 / READ_SEG / 2, 0.0)

    report = _build(tmp_path, poll, 1, "read")

    assert report["accepted"], report["acceptance_failures"]
    read = report["counter_correlation"]["directions"]["read"]
    assert read["status"] == "outside_tolerance"
    assert read["ratio"] == pytest.approx(0.5, abs=0.02)


def test_rejects_unavailable_read_slope(tmp_path: Path) -> None:
    """An unmeasurable slope leaves the cell with no wire-level witness."""
    _write_read_initiator(tmp_path, 0, start=100.0)
    poll = tmp_path / "poll.txt"
    # Only a handful of samples land inside the trimmed window.
    _write_poll(poll, 1000.0, 0.0, seconds=5)

    report = _build(tmp_path, poll, 1, "read")

    read = report["counter_correlation"]["directions"]["read"]
    assert read["status"] == "unavailable"
    assert not report["accepted"]
    assert any(
        "no read-direction RDMA counter slope" in f
        for f in report["acceptance_failures"]
    )


def test_rejects_unavailable_write_slope_in_mixed(tmp_path: Path) -> None:
    """Mixed drove both directions, so the write slope is gated too.

    Both directions are fit from the one poll file and share its time base, so a
    poll too short to fit takes out both. What this pins is that the write
    direction is gated at all under mixed -- a read-only cell never reaches it.
    """
    for worker_id in range(2):
        _write_mixed_initiator(tmp_path, worker_id, start=100.0)
    poll = tmp_path / "poll.txt"
    _write_poll(poll, 1000.0, 200.0, seconds=5)

    report = _build(tmp_path, poll, 2, "mixed")

    assert not report["accepted"]
    failures = " ".join(report["acceptance_failures"])
    assert "no write-direction RDMA counter slope" in failures
    assert "no read-direction RDMA counter slope" in failures


def test_rejects_out_of_scope_segment_size(tmp_path: Path) -> None:
    """A non-positive segment size is a caller error, not a silent default."""
    _write_read_initiator(tmp_path, 0, start=100.0)
    poll = tmp_path / "poll.txt"
    _write_poll(poll, 1000.0, 0.0)

    with pytest.raises(ValueError, match="segment sizes must be positive"):
        _build(tmp_path, poll, 1, "read", read_seg_bytes=0.0)


def test_rejects_mixed_without_a_write_calibration(tmp_path: Path) -> None:
    """4096.0 is a page-sized guess; this geometry calibrates to 4083.9."""
    for worker_id in range(2):
        _write_mixed_initiator(tmp_path, worker_id, start=100.0)
    poll = tmp_path / "poll.txt"
    _write_poll(poll, 1000.0, 200.0)

    with pytest.raises(ValueError, match="mixed mode requires write_seg_bytes"):
        _build(tmp_path, poll, 2, "mixed", write_seg_bytes=None)


def test_read_cell_needs_no_write_calibration(tmp_path: Path) -> None:
    """A read cell drives no writes, so it legitimately has no write constant."""
    _write_read_initiator(tmp_path, 0, start=100.0)
    poll = tmp_path / "poll.txt"
    _write_poll(poll, 6100 * PAGE_BYTES / 30.0 / READ_SEG, 0.0)

    report = _build(tmp_path, poll, 1, "read", write_seg_bytes=None)

    assert report["accepted"], report["acceptance_failures"]
    assert report["write_seg_bytes"] is None
    assert "write" not in report["counter_correlation"]["directions"]


def test_rejects_unknown_mode(tmp_path: Path) -> None:
    poll = tmp_path / "poll.txt"
    _write_poll(poll, 1000.0, 0.0)

    with pytest.raises(ValueError, match="mode must be read or mixed"):
        _build(tmp_path, poll, 1, "write-only")
