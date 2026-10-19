# SPDX-License-Identifier: Apache-2.0
"""Read the configured Falcon ACCs in parallel when Prometheus requests /metrics."""

# Future
from __future__ import annotations

# Standard
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Mapping
import argparse
import sys
import time

sys.path.insert(0, str(Path(__file__).parent))
# Third Party
from acc_telemetry_textfile import GrpcTelemetryReader, TelemetryReader, render_metrics


def _read_acc(acc: str, reader: TelemetryReader) -> tuple[str, str, bool, float]:
    started = time.monotonic()
    try:
        counters, fields = reader.collect()
        read_time = time.time()
        success = True
    except Exception as exc:
        print(f"{acc}: telemetry read failed: {exc}", file=sys.stderr, flush=True)
        try:
            reader.reconnect()
        except Exception as reconnect_exc:
            print(
                f"{acc}: telemetry reconnect failed: {reconnect_exc}",
                file=sys.stderr,
                flush=True,
            )
        counters, fields = {}, None
        read_time = time.time()
        success = False
    duration = time.monotonic() - started
    output = render_metrics(
        counters,
        success,
        read_time,
        acc=acc,
        tele_fields=fields,
        source="grpc_exporter" if success else None,
    )
    return acc, output, success, duration


def collect_metrics(
    readers: Mapping[str, TelemetryReader],
    executor: ThreadPoolExecutor | None = None,
) -> str:
    """Collect and render one scrape, omitting fields for any failed ACC.

    Args:
        readers: One independent gRPC reader for each target ACC.
        executor: Persistent worker pool when serving HTTP.

    Returns:
        Prometheus exposition with complete set of unique family declarations.
    """
    if executor is None:
        with ThreadPoolExecutor(max_workers=len(readers)) as temporary:
            return collect_metrics(readers, temporary)
    started = time.monotonic()
    futures = [
        executor.submit(_read_acc, acc, reader) for acc, reader in readers.items()
    ]
    parts = [future.result() for future in futures]
    families: dict[str, list[str]] = {}
    for acc, output, _success, duration in parts:
        for line in output.splitlines():
            if line.startswith(("# HELP ", "# TYPE ")):
                name = line.split()[2]
                family = families.setdefault(name, [])
                if line not in family:
                    family.append(line)
            else:
                name = line.split("{", 1)[0].split(" ", 1)[0]
                families.setdefault(name, []).append(line)
        families.setdefault("acc_telemetry_rpc_duration_seconds", []).append(
            f'acc_telemetry_rpc_duration_seconds{{acc="{acc}"}} {duration}'
        )
    complete = int(all(part[2] for part in parts) and len(parts) == len(readers))
    families["acc_telemetry_rpc_duration_seconds"][:0] = [
        "# HELP acc_telemetry_rpc_duration_seconds Duration of each ACC RPC",
        "# TYPE acc_telemetry_rpc_duration_seconds gauge",
    ]
    families["acc_telemetry_exporter_complete"] = [
        "# HELP acc_telemetry_exporter_complete 1 when all ACC reads succeed",
        "# TYPE acc_telemetry_exporter_complete gauge",
        f"acc_telemetry_exporter_complete {complete}",
    ]
    families["acc_telemetry_scrape_duration_seconds"] = [
        "# HELP acc_telemetry_scrape_duration_seconds Duration of all ACC reads",
        "# TYPE acc_telemetry_scrape_duration_seconds gauge",
        f"acc_telemetry_scrape_duration_seconds {time.monotonic() - started}",
    ]
    return "\n".join(line for family in families.values() for line in family) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    """Serve a fresh counter set on each Prometheus scrape."""

    readers: Mapping[str, TelemetryReader]
    executor: ThreadPoolExecutor

    def do_GET(self) -> None:
        """Serve the Prometheus endpoint."""
        if self.path != "/metrics":
            self.send_error(404)
            return
        payload = collect_metrics(self.readers, self.executor).encode("ascii")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def main() -> None:
    """Run the loopback-only target ACC metrics endpoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9188)
    parser.add_argument("--proto-dir", type=Path, required=True)
    parser.add_argument("--rpc-timeout", type=float, default=1.0)
    parser.add_argument(
        "--accs", default="1,2,3,4", help="ACC indices; accN reads 127.0.0.1:15000+N"
    )
    args = parser.parse_args()
    readers = {
        f"acc{i}": GrpcTelemetryReader(
            f"127.0.0.1:{15000 + i}", args.proto_dir, args.rpc_timeout
        )
        for i in (int(x) for x in args.accs.split(","))
    }
    with ThreadPoolExecutor(max_workers=len(readers)) as executor:
        MetricsHandler.readers = readers
        MetricsHandler.executor = executor
        server = HTTPServer((args.listen, args.port), MetricsHandler)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            for reader in readers.values():
                reader.close()


if __name__ == "__main__":
    main()
