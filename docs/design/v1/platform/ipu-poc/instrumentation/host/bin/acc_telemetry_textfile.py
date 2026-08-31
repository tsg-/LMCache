# SPDX-License-Identifier: Apache-2.0
"""Export Falcon ACC RC byte counters for node_exporter's textfile collector."""

from __future__ import annotations

# Standard
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Mapping


OUTPUT_FILENAME = "acc_telemetry.prom"
REQUIRED_COUNTERS = ("bytes_from_ulp_rc", "bytes_to_ulp")


def validate_counters(values: Mapping[str, object]) -> dict[str, int]:
    """Validate and normalize the two ACC RC byte counters.

    Args:
        values: Mapping containing the generated telemetry response values.

    Returns:
        The two counter values as native Python integers.

    Raises:
        ValueError: If either counter is absent, negative, or not an integer.
    """
    normalized: dict[str, int] = {}
    for name in REQUIRED_COUNTERS:
        value = values.get(name)
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        normalized[name] = value
    return normalized


def render_metrics(
    counters: Mapping[str, int],
    success: bool,
    timestamp: int,
    acc: str | None = None,
) -> str:
    """Render one ACC collector sample in Prometheus textfile format.

    Args:
        counters: Validated ACC RC byte counters. Omit on failed collection.
        success: Whether the ACC RPC and counter validation succeeded.
        timestamp: Unix timestamp of this collection attempt.
        acc: Optional ACC label for independently collected textfiles.

    Returns:
        Prometheus exposition text ending in a newline.
    """
    lines: list[str] = []
    acc_label = f'acc="{acc}",' if acc else ""
    if success:
        lines.extend(
            [
                "# HELP acc_telemetry_bytes_total ACC Falcon RC byte counter",
                "# TYPE acc_telemetry_bytes_total counter",
            ]
        )
        for name in REQUIRED_COUNTERS:
            lines.append(
                'acc_telemetry_bytes_total{%scounter="%s",ulp="rdma"} %d'
                % (acc_label, name, counters[name])
            )
    lines.extend(
        [
            "# HELP acc_telemetry_collector_success 1 when the latest ACC "
            "query succeeded",
            "# TYPE acc_telemetry_collector_success gauge",
            f"acc_telemetry_collector_success{{{acc_label.rstrip(',')}}} {int(success)}"
            if acc
            else f"acc_telemetry_collector_success {int(success)}",
            "# HELP acc_telemetry_collector_timestamp_seconds Unix timestamp "
            "of latest ACC attempt",
            "# TYPE acc_telemetry_collector_timestamp_seconds gauge",
            f"acc_telemetry_collector_timestamp_seconds{{{acc_label.rstrip(',')}}} "
            f"{timestamp}"
            if acc
            else f"acc_telemetry_collector_timestamp_seconds {timestamp}",
        ]
    )
    return "\n".join(lines) + "\n"


def collect_counters(endpoint: str, proto_dir: Path) -> dict[str, int]:
    """Query the ACC telemetry service for its global RC byte counters.

    Args:
        endpoint: ACC gRPC endpoint as ``host:port``.
        proto_dir: Directory containing generated telemetry protobuf modules.

    Returns:
        Validated RC byte counters.

    Raises:
        ImportError: If gRPC or the telemetry protobuf modules are unavailable.
        ValueError: If the returned counters are not valid unsigned values.
        grpc.RpcError: If the ACC service does not answer the RPC.
    """
    if not proto_dir.is_dir():
        raise ValueError(f"telemetry protobuf directory does not exist: {proto_dir}")
    sys.path.insert(0, str(proto_dir))

    import grpc
    import telemetry_pb2
    import telemetry_pb2_grpc

    request = telemetry_pb2.StatsRequest(
        type=telemetry_pb2.Global,
        host=telemetry_pb2.HOST0,
        ulp=telemetry_pb2.ULP_RDMA,
    )
    channel = grpc.insecure_channel(endpoint)
    try:
        response = telemetry_pb2_grpc.TelemetryStub(channel).GetGlobalCounters(
            request, timeout=3
        )
    finally:
        channel.close()
    global_counters = response.global_all.global_counters
    return validate_counters(
        {name: getattr(global_counters, name) for name in REQUIRED_COUNTERS}
    )


def write_textfile(
    output_dir: Path,
    contents: str,
    output_filename: str = OUTPUT_FILENAME,
) -> None:
    """Atomically publish Prometheus exposition text in the output directory.

    Args:
        output_dir: node_exporter textfile collector directory.
        contents: Complete Prometheus exposition text.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="ascii",
        dir=output_dir,
        prefix=f"{output_filename}.",
        delete=False,
    ) as temporary:
        temporary.write(contents)
        temporary.flush()
        os.fchmod(temporary.fileno(), 0o644)
        temporary_path = Path(temporary.name)
    temporary_path.replace(output_dir / output_filename)


def _required_env(name: str) -> str:
    """Read a required collector environment variable.

    Args:
        name: Environment variable name.

    Returns:
        The non-empty value.

    Raises:
        ValueError: If the variable is unset or empty.
    """
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} must be set")
    return value


def main() -> int:
    """Collect ACC telemetry and write a success or failure textfile."""
    output_dir = Path(
        os.environ.get("ACC_TELEMETRY_OUTPUT_DIR", "/var/lib/node_exporter/textfile")
    )
    acc = os.environ.get("ACC_TELEMETRY_ACC") or None
    output_filename = os.environ.get("ACC_TELEMETRY_OUTPUT_FILENAME", OUTPUT_FILENAME)
    timestamp = int(time.time())
    try:
        counters = collect_counters(
            endpoint=_required_env("ACC_TELEMETRY_ENDPOINT"),
            proto_dir=Path(_required_env("ACC_TELEMETRY_PROTO_DIR")),
        )
    except Exception as error:
        print(f"ACC telemetry collection failed: {error}", file=sys.stderr)
        write_textfile(
            output_dir,
            render_metrics({}, success=False, timestamp=timestamp, acc=acc),
            output_filename,
        )
        return 1

    write_textfile(
        output_dir,
        render_metrics(counters, success=True, timestamp=timestamp, acc=acc),
        output_filename,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
