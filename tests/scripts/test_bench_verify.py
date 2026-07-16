# SPDX-License-Identifier: Apache-2.0
"""Tests for transport-neutral benchmark verification."""

# Standard
import importlib.util
from pathlib import Path
import sys
from types import ModuleType


SCRIPT = Path(__file__).parents[2] / "scripts" / "bench_verify.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("bench_verify", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load bench_verify.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_wire_counter_delta_discovers_driver_names() -> None:
    """Wire evidence uses available byte/packet counters without fixed names."""
    module = _load_module()
    before = module.parse_ethtool_statistics(
        "Statistics:\n  port_xmit_data: 10\n  custom_rx_octets: 20\n"
    )
    after = module.parse_ethtool_statistics(
        "Statistics:\n  port_xmit_data: 18\n  custom_rx_octets: 25\n"
    )

    assert module.wire_counter_delta(before, after) == {
        "port_xmit_data": 8,
        "custom_rx_octets": 5,
    }
    assert module.estimate_wire_bytes(module.wire_counter_delta(before, after)) == {
        "port_xmit_data": 32,
        "custom_rx_octets": 5,
    }


def test_parse_dma_samples_ignores_unrelated_server_output() -> None:
    """Legacy text records are confined to diagnostic parsing."""
    module = _load_module()

    samples = module.parse_dma_samples_text(
        "server started\n"
        "BENCH_RDMA_DMA operation=read request_id=bench-store-100 "
        "bytes=2048 dma_ms=0.125\n"
        "unrelated dma_ms=99\n"
    )

    assert samples == [
        module.DmaSample(
            operation="read",
            request_id="bench-store-100",
            bytes_transferred=2048,
            dma_ms=0.125,
        )
    ]


def test_parse_dma_samples_rejects_legacy_text_records() -> None:
    """Publishable parsing rejects the legacy LMCache server log format."""
    module = _load_module()

    assert (
        module.parse_dma_samples(
            "BENCH_RDMA_DMA operation=read request_id=bench-store-100 "
            "bytes=2048 dma_ms=0.125\n"
        )
        == []
    )


def test_parse_dma_samples_accepts_structured_records() -> None:
    """Structured raw-verbs records derive DMA duration from nanoseconds."""
    module = _load_module()

    samples = module.parse_dma_samples(
        'BENCH_RDMA_DMA {"operation":"read","page_idx":3,"start_ns":100,'
        '"end_ns":250100,"bytes":65536}\n'
    )

    assert samples == [
        module.DmaSample(
            operation="read",
            request_id="3",
            bytes_transferred=65536,
            dma_ms=0.25,
        )
    ]


def test_build_result_marks_control_dominated_and_requires_verbs_evidence() -> None:
    """Verification keeps data integrity and transport evidence independent."""
    module = _load_module()
    before = module.parse_ethtool_statistics("tx_bytes: 10\n")
    after = module.parse_ethtool_statistics("tx_bytes: 18\n")

    result = module.build_result(
        b"producer",
        b"producer",
        before,
        after,
        module.Transport.VERBS,
        ["TRANSPORT verbs rc_mlx5 device=mlx5_1 qp_num=7 gid_index=3"],
        [3.0, 5.0],
        [1.0, 2.0],
    )

    assert result.digest_matches is True
    assert result.wire_bytes_observed is True
    assert result.wire_bytes_total == 8
    assert result.wire_bytes_within_pct is True
    assert result.transport_asserted is True
    assert result.control_plane_dominated is True


def test_verbs_mr_evidence_is_diagnostic_not_transport_gate() -> None:
    """MR-flag evidence is a diagnostic surface for M1, not an assert gate."""
    module = _load_module()
    transport = "TRANSPORT verbs rc_mlx5 device=mlx5_1 qp_num=7 gid_index=3"
    source_minimum = (
        "TRANSPORT_MR flags=IBV_ACCESS_LOCAL_WRITE|IBV_ACCESS_REMOTE_READ "
        "direction=read role=source"
    )
    storage_minimum = (
        "TRANSPORT_MR flags=IBV_ACCESS_LOCAL_WRITE "
        "direction=read role=storage"
    )
    over_permissive_source = (
        "TRANSPORT_MR flags=IBV_ACCESS_LOCAL_WRITE|"
        "IBV_ACCESS_REMOTE_READ|IBV_ACCESS_REMOTE_WRITE "
        "direction=read role=source"
    )

    # assert_transport passes on the RC-QP line alone; MR flags do not gate.
    assert module.assert_transport(module.Transport.VERBS, [transport])
    assert module.assert_transport(
        module.Transport.VERBS, [transport, over_permissive_source, storage_minimum]
    )

    # But the diagnostic helper surfaces the mismatch.
    minimum_observed = module.parse_verbs_mr_evidence(
        [transport, source_minimum, storage_minimum]
    )
    over_observed = module.parse_verbs_mr_evidence(
        [transport, over_permissive_source, storage_minimum]
    )
    assert module.verbs_mr_evidence_matches_minimum(minimum_observed) is True
    assert module.verbs_mr_evidence_matches_minimum(over_observed) is False


def test_build_result_from_digests_matches_bytes_path() -> None:
    """Endpoint digest records produce the same record as raw payload bytes."""
    module = _load_module()
    payload = b"producer"
    before = module.parse_ethtool_statistics("tx_bytes: 10\n")
    after = module.parse_ethtool_statistics("tx_bytes: 18\n")
    evidence = ["TRANSPORT verbs rc_mlx5 device=mlx5_1 qp_num=7 gid_index=3"]

    from_bytes = module.build_result(
        payload,
        payload,
        before,
        after,
        module.Transport.VERBS,
        evidence,
        [3.0],
        [1.0],
    )
    from_records = module.build_result_from_digests(
        from_bytes.digest_algorithm,
        from_bytes.producer_digest,
        from_bytes.consumer_digest,
        len(payload),
        before,
        after,
        module.Transport.VERBS,
        evidence,
        [3.0],
        [1.0],
    )

    assert from_records == from_bytes


def test_diagnostic_result_requires_unavailable_evidence_details() -> None:
    """Diagnostic records preserve unknown values rather than guessing them."""
    module = _load_module()

    result = module.build_diagnostic_result(
        module.Transport.NIXL,
        ["NIXL_NET_BACKEND=UCX", "nixl://192.168.200.3:5605"],
        [2.5],
        ["producer_digest", "dma_ms_median"],
        "no structured benchmark record",
    )

    assert result["data_quality"] == "diagnostic"
    assert result["producer_digest"] is None
    assert result["dma_ms_median"] is None
    assert result["transport_asserted"] is True


def test_wire_bytes_within_tolerance_rejects_wrong_magnitude() -> None:
    """A moving wire counter alone is insufficient evidence of a transfer."""
    module = _load_module()

    assert module.wire_bytes_within_tolerance({"tx_bytes": 70}, 64, 10) is True
    assert module.wire_bytes_within_tolerance({"tx_bytes": 7}, 64, 10) is False


def test_wire_bytes_within_tolerance_uses_largest_estimate() -> None:
    """Overlapping counter views cannot hide an inflated wire-byte total."""
    module = _load_module()

    assert (
        module.wire_bytes_within_tolerance(
            {"tx_bytes": 64, "port_xmit_data": 640}, 64, 10
        )
        is False
    )
