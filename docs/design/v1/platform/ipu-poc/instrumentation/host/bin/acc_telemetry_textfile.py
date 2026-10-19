# SPDX-License-Identifier: Apache-2.0
"""Export Falcon ACC RC byte counters for node_exporter's textfile collector."""

from __future__ import annotations

# Standard
import argparse
import math
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Mapping, Protocol


OUTPUT_FILENAME = "acc_telemetry.prom"
REQUIRED_COUNTERS = ("bytes_from_ulp_rc", "bytes_to_ulp")
TELEMETRY_SECTIONS = (
    ("global", "global_counters"),
    ("rx", "rx_counters"),
    ("tx", "tx_counters"),
    ("rue", "rue_counters"),
)


class TelemetryReader(Protocol):
    """Read one complete ACC Falcon telemetry sample."""

    def collect(self) -> tuple[dict[str, int], dict[str, dict[str, int | float]]]:
        """Return the RC byte counters and all dashboard-compatible fields."""

    def reconnect(self) -> None:
        """Discard any failed connection before the next collection attempt."""


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


def extract_telemetry_fields(
    global_all: object,
) -> dict[str, dict[str, int | float]]:
    """Read every numeric Falcon counter from the four gRPC sections.

    Args:
        global_all: The ``global_all`` message returned by
            ``TelemetryStub.GetGlobalCounters``.

    Returns:
        Numeric counter values keyed by dashboard-compatible section and field
        names.

    Raises:
        ValueError: If an expected section is absent or contains a non-finite
            floating-point value.
    """
    extracted: dict[str, dict[str, int | float]] = {}
    for section, message_name in TELEMETRY_SECTIONS:
        message = getattr(global_all, message_name, None)
        if message is None:
            raise ValueError(f"telemetry response lacks {message_name}")
        fields: dict[str, int | float] = {}
        for descriptor in message.DESCRIPTOR.fields:
            value = getattr(message, descriptor.name)
            if type(value) is int:
                fields[descriptor.name] = value
            elif type(value) is float:
                if not math.isfinite(value):
                    raise ValueError(f"{message_name}.{descriptor.name} must be finite")
                fields[descriptor.name] = value
        extracted[section] = fields
    return extracted


def normalize_telemetry_response(
    global_all: object,
) -> tuple[dict[str, int], dict[str, dict[str, int | float]]]:
    """Normalize a gRPC telemetry response for both legacy and new metrics.

    Args:
        global_all: The ``global_all`` message returned by
            ``TelemetryStub.GetGlobalCounters``.

    Returns:
        The legacy RC byte-counter subset and all dashboard-compatible Falcon
        fields.

    Raises:
        ValueError: If required byte counters or telemetry sections are invalid.
    """
    fields = extract_telemetry_fields(global_all)
    return validate_counters(fields["global"]), fields


def render_metrics(
    counters: Mapping[str, int],
    success: bool,
    timestamp: float,
    acc: str | None = None,
    tele_fields: Mapping[str, Mapping[str, int | float]] | None = None,
    source: str | None = None,
    collection_duration_seconds: float | None = None,
    schedule_lag_seconds: float | None = None,
    reconnects: int | None = None,
) -> str:
    """Render one ACC collector sample in Prometheus textfile format.

    Args:
        counters: Validated ACC RC byte counters. Omit on failed collection.
        success: Whether the ACC RPC and counter validation succeeded.
        timestamp: Unix timestamp of this collection attempt.
        acc: Optional ACC label for independently collected textfiles.
        tele_fields: Numeric Falcon counters grouped by telemetry section.
        source: Optional collection-path label for the read timestamp.
        collection_duration_seconds: Wall-clock duration of the latest RPC.
        schedule_lag_seconds: Delay from the latest fixed-rate deadline.
        reconnects: Number of reconnects requested since process start.

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
        if tele_fields:
            lines.extend(
                [
                    "# HELP acc_tele_field Falcon transport-engine counter "
                    "from gRPC telemetry",
                    "# TYPE acc_tele_field counter",
                ]
            )
            for section, fields in tele_fields.items():
                for field, value in fields.items():
                    lines.append(
                        f'acc_tele_field{{{acc_label}section="{section}",'
                        f'field="{field}"}} {value}'
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
    if acc and source:
        lines.extend(
            [
                "# HELP acc_telemetry_read_timestamp_seconds Unix timestamp "
                "when the ACC counter read completed",
                "# TYPE acc_telemetry_read_timestamp_seconds gauge",
                f'acc_telemetry_read_timestamp_seconds{{acc="{acc}",'
                f'source="{source}"}} {timestamp}',
            ]
        )
    if collection_duration_seconds is not None:
        lines.extend(
            [
                "# HELP acc_telemetry_collection_duration_seconds Duration of "
                "the latest ACC telemetry RPC",
                "# TYPE acc_telemetry_collection_duration_seconds gauge",
                f"acc_telemetry_collection_duration_seconds{{{acc_label.rstrip(',')}}} "
                f"{collection_duration_seconds}",
            ]
        )
    if schedule_lag_seconds is not None:
        lines.extend(
            [
                "# HELP acc_telemetry_schedule_lag_seconds Delay from the "
                "fixed collection deadline",
                "# TYPE acc_telemetry_schedule_lag_seconds gauge",
                f"acc_telemetry_schedule_lag_seconds{{{acc_label.rstrip(',')}}} "
                f"{schedule_lag_seconds}",
            ]
        )
    if reconnects is not None:
        lines.extend(
            [
                "# HELP acc_telemetry_reconnects_total Reconnect requests "
                "after failed ACC telemetry RPCs",
                "# TYPE acc_telemetry_reconnects_total counter",
                f"acc_telemetry_reconnects_total{{{acc_label.rstrip(',')}}} "
                f"{reconnects}",
            ]
        )
    return "\n".join(lines) + "\n"


class GrpcTelemetryReader:
    """Reuse one gRPC channel for repeated telemetry reads from one ACC."""

    def __init__(
        self,
        endpoint: str,
        proto_dir: Path,
        rpc_timeout_seconds: float = 1.0,
    ) -> None:
        """Prepare the reusable reader without opening a gRPC connection.

        Args:
            endpoint: ACC gRPC endpoint as ``host:port``.
            proto_dir: Directory containing generated telemetry protobuf modules.
            rpc_timeout_seconds: Deadline applied to each telemetry RPC.
        """
        if not proto_dir.is_dir():
            raise ValueError(
                f"telemetry protobuf directory does not exist: {proto_dir}"
            )
        if rpc_timeout_seconds <= 0:
            raise ValueError("rpc_timeout_seconds must be positive")

        proto_path = str(proto_dir)
        if proto_path not in sys.path:
            sys.path.insert(0, proto_path)

        import grpc
        import telemetry_pb2
        import telemetry_pb2_grpc

        self._endpoint = endpoint
        self._rpc_timeout_seconds = rpc_timeout_seconds
        self._grpc = grpc
        self._telemetry_pb2 = telemetry_pb2
        self._telemetry_pb2_grpc = telemetry_pb2_grpc
        self._channel = None
        self._stub = None
        self._request = telemetry_pb2.StatsRequest(
            type=telemetry_pb2.Global,
            host=telemetry_pb2.HOST0,
            ulp=telemetry_pb2.ULP_RDMA,
        )

    def collect(self) -> tuple[dict[str, int], dict[str, dict[str, int | float]]]:
        """Read and normalize all four Falcon telemetry sections.

        Returns:
            The legacy byte-counter subset and all dashboard-compatible fields.

        Raises:
            grpc.RpcError: If the ACC service does not answer before its
                configured deadline.
            ValueError: If an expected counter section is invalid.
        """
        if self._stub is None:
            self._connect()
        response = self._stub.GetGlobalCounters(
            self._request,
            timeout=self._rpc_timeout_seconds,
        )
        return normalize_telemetry_response(response.global_all)

    def reconnect(self) -> None:
        """Close the current gRPC channel before the next collection attempt."""
        self.close()

    def close(self) -> None:
        """Close the reusable channel when the collector is stopping."""
        if self._channel is not None:
            self._channel.close()
        self._channel = None
        self._stub = None

    def _connect(self) -> None:
        """Open the reusable channel and initialize its telemetry stub."""
        self._channel = self._grpc.insecure_channel(self._endpoint)
        self._stub = self._telemetry_pb2_grpc.TelemetryStub(self._channel)


def collect_counters(endpoint: str, proto_dir: Path) -> dict[str, int]:
    """Query one ACC once and return the legacy RC byte counters.

    Args:
        endpoint: ACC gRPC endpoint as ``host:port``.
        proto_dir: Directory containing generated telemetry protobuf modules.

    Returns:
        Validated RC byte counters.
    """
    reader = GrpcTelemetryReader(endpoint, proto_dir)
    try:
        counters, _fields = reader.collect()
        return counters
    finally:
        reader.close()


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


def run_collection_loop(
    reader: TelemetryReader,
    *,
    output_dir: Path,
    output_filename: str,
    acc: str,
    interval_seconds: float,
    now: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    wait: Callable[[float], bool],
) -> None:
    """Collect one ACC on a fixed schedule until the wait function stops it.

    Args:
        reader: Reusable gRPC reader for exactly one ACC.
        output_dir: Directory receiving the ACC-specific textfile.
        output_filename: Distinct textfile name for this ACC.
        acc: ACC label written to every metric.
        interval_seconds: Fixed interval between collection deadlines.
        now: Wall-clock source used at RPC return.
        monotonic: Monotonic clock used for fixed-rate scheduling.
        wait: Sleeps until the next deadline and returns true when stopping.

    Raises:
        ValueError: If the collection interval is not positive.
    """
    if interval_seconds <= 0:
        raise ValueError("interval_seconds must be positive")

    next_deadline = monotonic()
    reconnects = 0
    while True:
        started = monotonic()
        schedule_lag_seconds = max(0.0, started - next_deadline)
        try:
            counters, tele_fields = reader.collect()
            success = True
        except Exception as error:
            print(f"ACC telemetry collection failed: {error}", file=sys.stderr)
            counters = {}
            tele_fields = {}
            success = False
            reconnects += 1
            try:
                reader.reconnect()
            except Exception as reconnect_error:
                print(
                    f"ACC telemetry reconnect failed: {reconnect_error}",
                    file=sys.stderr,
                )

        timestamp = now()
        collection_duration_seconds = max(0.0, monotonic() - started)
        write_textfile(
            output_dir,
            render_metrics(
                counters,
                success=success,
                timestamp=timestamp,
                acc=acc,
                tele_fields=tele_fields,
                source="grpc",
                collection_duration_seconds=collection_duration_seconds,
                schedule_lag_seconds=schedule_lag_seconds,
                reconnects=reconnects,
            ),
            output_filename,
        )

        next_deadline += interval_seconds
        remaining = next_deadline - monotonic()
        if remaining <= 0:
            missed_deadlines = int((-remaining) // interval_seconds) + 1
            next_deadline += missed_deadlines * interval_seconds
            remaining = next_deadline - monotonic()
        if wait(remaining):
            return


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


def _positive_float_env(name: str, default: float) -> float:
    """Read a positive floating-point collector setting from the environment.

    Args:
        name: Environment variable name.
        default: Value used when the setting is unset.

    Returns:
        A positive floating-point setting.

    Raises:
        ValueError: If the value cannot be parsed or is not positive.
    """
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = float(raw_value)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive number") from error
    if value <= 0:
        raise ValueError(f"{name} must be a positive number")
    return value


def main() -> int:
    """Collect ACC telemetry once or run a persistent per-ACC loop."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--loop",
        action="store_true",
        help="run the reusable gRPC collector until systemd stops it",
    )
    args = parser.parse_args()
    output_dir = Path(
        os.environ.get("ACC_TELEMETRY_OUTPUT_DIR", "/var/lib/node_exporter/textfile")
    )
    acc = os.environ.get("ACC_TELEMETRY_ACC") or None
    output_filename = os.environ.get("ACC_TELEMETRY_OUTPUT_FILENAME", OUTPUT_FILENAME)
    try:
        reader = GrpcTelemetryReader(
            endpoint=_required_env("ACC_TELEMETRY_ENDPOINT"),
            proto_dir=Path(_required_env("ACC_TELEMETRY_PROTO_DIR")),
            rpc_timeout_seconds=_positive_float_env(
                "ACC_TELEMETRY_RPC_TIMEOUT_SECONDS",
                1.0,
            ),
        )
    except Exception as error:
        print(f"ACC telemetry collection failed: {error}", file=sys.stderr)
        write_textfile(
            output_dir,
            render_metrics({}, success=False, timestamp=time.time(), acc=acc),
            output_filename,
        )
        return 1

    try:
        if args.loop:
            if acc is None:
                raise ValueError("ACC_TELEMETRY_ACC must be set for --loop")
            stop_event = threading.Event()

            def _stop_loop(_signum: int, _frame: object) -> None:
                """Request a graceful stop after systemd sends a termination signal."""
                stop_event.set()

            signal.signal(signal.SIGINT, _stop_loop)
            signal.signal(signal.SIGTERM, _stop_loop)
            run_collection_loop(
                reader,
                output_dir=output_dir,
                output_filename=output_filename,
                acc=acc,
                interval_seconds=_positive_float_env(
                    "ACC_TELEMETRY_INTERVAL_SECONDS",
                    2.0,
                ),
                wait=stop_event.wait,
            )
            return 0

        counters, _fields = reader.collect()
        write_textfile(
            output_dir,
            render_metrics(
                counters,
                success=True,
                timestamp=time.time(),
                acc=acc,
            ),
            output_filename,
        )
        return 0
    except Exception as error:
        print(f"ACC telemetry collection failed: {error}", file=sys.stderr)
        write_textfile(
            output_dir,
            render_metrics({}, success=False, timestamp=time.time(), acc=acc),
            output_filename,
        )
        return 1
    finally:
        reader.close()


if __name__ == "__main__":
    raise SystemExit(main())
