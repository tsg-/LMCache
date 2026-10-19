# SPDX-License-Identifier: Apache-2.0
"""Snapshot ACC, target NVMe, and app counters over one fleet measured window."""

from __future__ import annotations

# Standard
import argparse
import json
import re
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any


HOST_PORTS = {"mmgi0": 9101, "mmgi1": 9102, "mmgi2": 9103, "mmgi3": 9104}
TARGET_PORTS = {"mmgi0": 1, "mmgi1": 2, "mmgi2": 3, "mmgi3": 4}
TARGET_ACCS = {"mmgi0": "acc2", "mmgi1": "acc1", "mmgi2": "acc3", "mmgi3": "acc4"}
FIELD = re.compile(
    r'^acc_tele_field\{acc="(acc[1-4])",section="global",'
    r'field="(bytes_from_ulp_rc|bytes_to_ulp)"\} (\d+)$'
)
READ_TIME = re.compile(
    r'^acc_telemetry_read_timestamp_seconds\{acc="(acc[1-4])",'
    r'source="grpc_exporter"\} ([0-9.]+)$'
)


def _ssh(host: str, command: str) -> str:
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "ConnectTimeout=3",
            host,
            command,
        ],
        capture_output=True,
        text=True,
        timeout=8,
        check=True,
    )
    return result.stdout


def _sample_time_and_body(host: str, command: str) -> tuple[float, str]:
    output = _ssh(
        host,
        f'printf "START %s\\n" "$(date +%s.%N)"; '
        f'{command}; printf "END %s\\n" "$(date +%s.%N)"',
    )
    lines = output.splitlines()
    if not lines[0].startswith("START ") or not lines[-1].startswith("END "):
        raise ValueError(f"{host}: missing sample timestamps")
    started = float(lines[0].split()[1])
    ended = float(lines[-1].split()[1])
    if ended - started > 1:
        raise ValueError(f"{host}: sample span {ended - started:.3f}s exceeds 1s")
    return (started + ended) / 2, "\n".join(lines[1:-1])


def _metric_value(
    text: str, name: str, phase: str = "measured", operation: str = "Load"
) -> float:
    values = [
        float(line.rsplit(" ", 1)[1])
        for line in text.splitlines()
        if line.startswith(name + "{")
        and f'phase="{phase}"' in line
        and f'operation="{operation}"' in line
    ]
    if len(values) != 1:
        raise ValueError(f"expected one {operation}/{phase} {name}, got {len(values)}")
    return values[0]


def read_app(host: str, port: int, mode: str = "load") -> dict[str, float]:
    """Read one initiator's measured goodput and phase start directly."""
    timestamp, text = _sample_time_and_body(
        host, f"curl -fsS --max-time 2 http://127.0.0.1:{port}/metrics"
    )
    if mode == "mixed":
        starts = {
            operation: _metric_value(
                text,
                "lmcache_bench_l2_phase_start_time_seconds",
                operation=operation,
            )
            for operation in ("Load", "Store")
        }
        if starts["Load"] != starts["Store"]:
            raise ValueError(f"{host}: mixed phase starts differ")
        return {
            "timestamp": timestamp,
            "load_success_bytes": _metric_value(
                text,
                "lmcache_bench_l2_success_bytes_total",
                operation="Load",
            ),
            "store_success_bytes": _metric_value(
                text,
                "lmcache_bench_l2_success_bytes_total",
                operation="Store",
            ),
            "phase_started_at": starts["Load"],
        }
    return {
        "timestamp": timestamp,
        "success_bytes": _metric_value(text, "lmcache_bench_l2_success_bytes_total"),
        "phase_started_at": _metric_value(
            text, "lmcache_bench_l2_phase_start_time_seconds"
        ),
    }


def read_acc(
    hosts: tuple[str, ...] = tuple(HOST_PORTS),
) -> dict[str, dict[str, int | float]]:
    """Read selected target ACCs through the scrape-time exporter."""
    text = _ssh("mmgt", "curl -fsS --max-time 2 http://127.0.0.1:9188/metrics")
    if "acc_telemetry_exporter_complete 1\n" not in text:
        raise ValueError("ACC exporter returned an incomplete fleet")
    values: dict[str, dict[str, int | float]] = {
        TARGET_ACCS[host]: {} for host in hosts
    }
    for line in text.splitlines():
        match = FIELD.match(line)
        if match:
            name = "write" if match.group(2) == "bytes_from_ulp_rc" else "read"
            if match.group(1) in values:
                values[match.group(1)][name] = int(match.group(3))
        match = READ_TIME.match(line)
        if match:
            if match.group(1) in values:
                values[match.group(1)]["timestamp"] = float(match.group(2))
    if any(set(value) != {"write", "read", "timestamp"} for value in values.values()):
        raise ValueError("ACC exporter omitted a payload counter or read timestamp")
    return values


def exported_devices(hosts: tuple[str, ...] = tuple(HOST_PORTS)) -> list[str]:
    """List selected target ports' backing namespaces from configfs."""
    ports = " ".join(str(TARGET_PORTS[host]) for host in hosts)
    output = _ssh(
        "mmgt",
        f"for port in {ports}; do "
        "for file in /sys/kernel/config/nvmet/ports/$port/subsystems/"
        "*/namespaces/*/device_path; do "
        '[ -r "$file" ] && cat "$file"; '
        "done; done",
    )
    devices = sorted({Path(line).name for line in output.splitlines() if line})
    expected = 4 * len(hosts)
    if len(devices) != expected:
        raise ValueError(
            f"expected {expected} exported target namespaces, got {devices}"
        )
    return devices


def read_nvme(devices: list[str]) -> dict[str, Any]:
    """Read kernel diskstats for the exported target namespaces."""
    timestamp, text = _sample_time_and_body("mmgt", "cat /proc/diskstats")
    rows: dict[str, dict[str, int]] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10 or fields[2] not in devices:
            continue
        rows[fields[2]] = {
            "read": int(fields[5]) * 512,
            "write": int(fields[9]) * 512,
        }
    if set(rows) != set(devices):
        raise ValueError("diskstats lacks an exported namespace")
    return {
        "timestamp": timestamp,
        "read": sum(row["read"] for row in rows.values()),
        "write": sum(row["write"] for row in rows.values()),
        "devices": rows,
    }


def snapshot(
    devices: list[str],
    mode: str = "load",
    hosts: tuple[str, ...] = tuple(HOST_PORTS),
) -> dict[str, Any]:
    """Read selected sources concurrently and preserve their read times."""
    with ThreadPoolExecutor(max_workers=6) as pool:
        apps = {
            host: pool.submit(read_app, host, port, mode)
            for host, port in HOST_PORTS.items()
            if host in hosts
        }
        acc = pool.submit(read_acc, hosts)
        nvme = pool.submit(read_nvme, devices)
        return {
            "app": {host: future.result() for host, future in apps.items()},
            "acc": acc.result(),
            "nvme": nvme.result(),
        }


def _delta(start: dict[str, Any], end: dict[str, Any], label: str) -> tuple[int, float]:
    value = end[label] - start[label]
    span = end["timestamp"] - start["timestamp"]
    if value < 0 or span <= 0:
        raise ValueError(f"{label} reset or nonpositive read span")
    return int(value), span


def counter_deltas(start: dict[str, Any], end: dict[str, Any]) -> dict[str, Any]:
    """Compute bytes and rates using each source's own snapshot span."""
    totals: dict[str, Any] = {}
    if set(start["acc"]) != set(end["acc"]):
        raise ValueError("ACC set changed between snapshots")
    if set(start["app"]) != set(end["app"]):
        raise ValueError("initiator set changed between snapshots")
    per_acc: dict[str, dict[str, int | float]] = {acc: {} for acc in start["acc"]}
    for direction in ("write", "read"):
        bytes_total = 0
        gbps_total = 0.0
        for acc in start["acc"]:
            before, after = start["acc"][acc], end["acc"][acc]
            try:
                count, span = _delta(before, after, direction)
            except ValueError as exc:
                raise ValueError(f"{acc}.{direction}: {exc}") from exc
            bytes_total += count
            gbps_total += count * 8 / span / 1e9
            per_acc[acc][f"{direction}_bytes"] = count
            per_acc[acc][f"{direction}_gbps_decimal"] = count * 8 / span / 1e9
            per_acc[acc]["read_span_seconds"] = span
        totals[f"rdma_{direction}_bytes"] = bytes_total
        totals[f"rdma_{direction}_gbps_decimal"] = gbps_total
    totals["acc_deltas"] = per_acc
    for direction in ("read", "write"):
        count, span = _delta(start["nvme"], end["nvme"], direction)
        totals[f"nvme_{direction}_bytes"] = count
        totals[f"nvme_{direction}_gbps_decimal"] = count * 8 / span / 1e9
    if "devices" in start["nvme"]:
        before_devices = start["nvme"]["devices"]
        after_devices = end["nvme"]["devices"]
        if set(before_devices) != set(after_devices):
            raise ValueError("target NVMe device set changed between snapshots")
        per_device = {}
        for device, before in before_devices.items():
            after = after_devices[device]
            delta = {
                direction: after[direction] - before[direction]
                for direction in ("read", "write")
            }
            if any(count < 0 for count in delta.values()):
                raise ValueError(f"{device}: target NVMe counter reset")
            per_device[device] = delta
        totals["nvme_device_deltas_bytes"] = per_device
    directions = (
        ("load", "store")
        if "load_success_bytes" in next(iter(start["app"].values()))
        else ("",)
    )
    for direction in directions:
        name = f"{direction}_success_bytes" if direction else "success_bytes"
        app_bytes = 0
        app_gbps = 0.0
        for host in start["app"]:
            count, span = _delta(start["app"][host], end["app"][host], name)
            app_bytes += count
            app_gbps += count * 8 / span / 1e9
        prefix = f"app_{direction}" if direction else "app"
        totals[f"{prefix}_bytes" if direction else "app_success_bytes"] = app_bytes
        totals[
            f"{prefix}_gbps_decimal" if direction else "app_goodput_gbps_decimal"
        ] = app_gbps
    if len(directions) == 2:
        if totals["app_store_bytes"] <= 0:
            raise ValueError("mixed app store bytes did not increase")
        totals["app_read_write_ratio"] = (
            totals["app_load_bytes"] / totals["app_store_bytes"]
        )
    return totals


def validate_load_counter_agreement(deltas: dict[str, Any]) -> None:
    """Reject a load result whose selected target counters lack its traffic.

    Args:
        deltas: Phase-aligned counter deltas returned by counter_deltas.

    Returns:
        None when the counters are plausibly attributed to the active lanes.

    Raises:
        ValueError: If target NVMe or RDMA disagrees with the app.
    """
    app = deltas["app_goodput_gbps_decimal"]
    nvme = deltas["nvme_read_gbps_decimal"]
    rdma = deltas["rdma_write_gbps_decimal"]
    if app <= 0 or nvme <= 0:
        raise ValueError("Load goodput or target NVMe read rate is zero")
    if abs(nvme / app - 1) > 0.03:
        raise ValueError("target NVMe reads differ from app Load by over 3%")
    if abs(rdma / nvme - 1) > 0.05:
        raise ValueError("selected ACC RDMA WR differs from NVMe reads by over 5%")


def validate_mixed_counter_agreement(deltas: dict[str, Any]) -> None:
    """Require both mixed directions on the selected target ports.

    Args:
        deltas: Phase-aligned counter deltas returned by counter_deltas.

    Returns:
        None when both target directions agree with application bytes.

    Raises:
        ValueError: If a target direction is absent or disagrees with the app.
    """
    for operation, nvme_direction, rdma_direction in (
        ("load", "read", "write"),
        ("store", "write", "read"),
    ):
        app = deltas[f"app_{operation}_gbps_decimal"]
        nvme = deltas[f"nvme_{nvme_direction}_gbps_decimal"]
        rdma = deltas[f"rdma_{rdma_direction}_gbps_decimal"]
        if app <= 0 or nvme <= 0:
            raise ValueError(
                f"{operation}: app bytes or NVMe {nvme_direction}s are zero"
            )
        if abs(nvme / app - 1) > 0.03:
            raise ValueError(
                f"{operation}: target NVMe {nvme_direction}s differ"
                " from app bytes by over 3%"
            )
        if abs(rdma / nvme - 1) > 0.05:
            raise ValueError(
                f"{operation}: selected ACC RDMA {rdma_direction.upper()}"
                f" differs from NVMe {nvme_direction}s by over 5%"
            )


def wait_for_measured_start(
    mode: str = "load", hosts: tuple[str, ...] = tuple(HOST_PORTS)
) -> dict[str, float]:
    """Wait until selected processes publish a positive measured start."""
    deadline = time.monotonic() + 120
    with ThreadPoolExecutor(max_workers=4) as pool:
        while time.monotonic() < deadline:
            futures = {
                host: pool.submit(read_app, host, port, mode)
                for host, port in HOST_PORTS.items()
                if host in hosts
            }
            try:
                starts = {
                    host: future.result()["phase_started_at"]
                    for host, future in futures.items()
                }
            except (
                OSError,
                subprocess.CalledProcessError,
                subprocess.TimeoutExpired,
                ValueError,
            ):
                time.sleep(0.2)
                continue
            if all(value > 0 for value in starts.values()):
                return starts
            time.sleep(0.2)
    raise TimeoutError("selected measured phases did not start within 120s")


def host_result_summary(
    result: dict[str, Any],
    path: str,
    mode: str = "load",
    duration: float | None = None,
) -> dict[str, Any]:
    """Link one complete host result to a fleet result."""
    if mode == "mixed":
        config = result["metrics"]["config"]
        if duration is None or float(config["duration_sec"]) != duration:
            raise ValueError(f"{path}: unexpected mixed duration")
        if config["read_write_ratio_requested"] != "5:1":
            raise ValueError(f"{path}: unexpected mixed read:write ratio")
        operations = {}
        starts = set()
        ends = set()
        for key, name in (("op_0", "Load"), ("op_1", "Store")):
            row = result["metrics"][key]
            if row["operation"] != name or (
                row["total_success"] != row["total_keys"] or row["total_keys"] <= 0
            ):
                raise ValueError(f"{path}: {name} requests did not all succeed")
            if float(row["window_sec"]) < duration:
                raise ValueError(f"{path}: {name} measured window was short")
            starts.add(float(row["phase_start_time_seconds"]))
            ends.add(float(row["phase_end_time_seconds"]))
            operations[name] = {
                "total_success": row["total_success"],
                "total_keys": row["total_keys"],
                "total_success_bytes": int(row["total_success_bytes"]),
                "window_sec": row["window_sec"],
            }
        if (
            len(starts) != 1
            or len(ends) != 1
            or min(starts) <= 0
            or min(ends) <= min(starts)
        ):
            raise ValueError(f"{path}: mixed phase timestamps invalid")
        if operations["Store"]["total_success_bytes"] <= 0:
            raise ValueError(f"{path}: no successful Store bytes")
        ratio = (
            operations["Load"]["total_success_bytes"]
            / operations["Store"]["total_success_bytes"]
        )
        if not 4.95 <= ratio <= 5.05:
            raise ValueError(f"{path}: mixed read:write ratio {ratio:.3f}")
        return {
            "path": path,
            "operations": operations,
            "read_write_ratio_achieved": ratio,
            "phase_start_time_seconds": min(starts),
            "phase_end_time_seconds": min(ends),
        }
    operation = result["metrics"]["op_0"]
    if (
        operation["operation"] != "Load"
        or operation["total_success"] != operation["total_keys"]
        or operation["total_keys"] <= 0
    ):
        raise ValueError(f"{path}: benchmark requests did not all succeed")
    if duration is not None:
        if float(operation["window_sec"]) < duration:
            raise ValueError(f"{path}: measured window was short")
        started = float(operation["phase_start_time_seconds"])
        ended = float(operation["phase_end_time_seconds"])
        if started <= 0 or ended <= started:
            raise ValueError(f"{path}: invalid measured phase timestamps")
    return {
        "path": path,
        "operation": operation["operation"],
        "phase_start_time_seconds": operation.get("phase_start_time_seconds"),
        "phase_end_time_seconds": operation.get("phase_end_time_seconds"),
        "total_success": operation["total_success"],
        "total_keys": operation["total_keys"],
        "throughput_requested_gbps_decimal": operation.get(
            "throughput_aggregate_gbps_decimal"
        ),
        "throughput_success_gbps_decimal": operation.get(
            "throughput_success_gbps_decimal"
        ),
    }


def result_links(
    run_id: str,
    in_flight: int,
    host_result_template: str | None = None,
    mode: str = "load",
    duration: float | None = None,
    hosts: tuple[str, ...] = tuple(HOST_PORTS),
) -> dict[str, dict[str, Any]]:
    """Link each initiator's result without duplicating a fleet total in it."""
    deadline = time.monotonic() + 30
    links: dict[str, dict[str, Any]] = {}
    for host in hosts:
        path = (
            host_result_template.format(host=host)
            if host_result_template
            else f"/root/{run_id}-if{in_flight}-{host}.json"
        )
        while time.monotonic() < deadline:
            try:
                result = json.loads(_ssh(host, f"cat {shlex.quote(path)}"))
                links[host] = host_result_summary(result, path, mode, duration)
                break
            except (OSError, subprocess.CalledProcessError, json.JSONDecodeError):
                time.sleep(0.5)
        if host not in links:
            raise TimeoutError(f"{host}: no result at {path}")
    return links


def measure(
    run_id: str,
    in_flight: int,
    duration: float,
    host_result_template: str | None = None,
    mode: str = "load",
    hosts: tuple[str, ...] = tuple(HOST_PORTS),
) -> dict[str, Any]:
    """Measure the common sustained interval and its counter deltas."""
    devices = exported_devices(hosts)
    starts = wait_for_measured_start(mode, hosts)
    overlap_start = max(starts.values())
    overlap_end = min(starts.values()) + duration
    start = snapshot(devices, mode, hosts)
    start_read_times = [
        *(v["timestamp"] for v in start["acc"].values()),
        start["nvme"]["timestamp"],
        *(v["timestamp"] for v in start["app"].values()),
    ]
    if min(start_read_times) < overlap_start:
        raise ValueError("start snapshot preceded the shared measured phase")
    if max(start_read_times) - overlap_start > 1:
        raise ValueError("start snapshot missed more than 1s of the overlap window")
    if time.time() >= overlap_end - 2:
        raise ValueError("coordinator attached too late for the overlap window")
    time.sleep(max(0, overlap_end - 1 - time.time()))
    end = snapshot(devices, mode, hosts)
    end_read_times = [
        *(v["timestamp"] for v in end["acc"].values()),
        end["nvme"]["timestamp"],
        *(v["timestamp"] for v in end["app"].values()),
    ]
    if max(end_read_times) > overlap_end:
        raise ValueError("end snapshot exceeded the shared measured phase")
    links = result_links(run_id, in_flight, host_result_template, mode, duration, hosts)
    phase_ends = [
        row["phase_end_time_seconds"]
        for row in links.values()
        if row["phase_end_time_seconds"] is not None
    ]
    deltas = counter_deltas(start, end)
    if mode == "load":
        validate_load_counter_agreement(deltas)
    else:
        validate_mixed_counter_agreement(deltas)
    return {
        "run_id": run_id,
        "hosts": hosts,
        "target_accs": {host: TARGET_ACCS[host] for host in hosts},
        "target_devices": devices,
        "in_flight": in_flight,
        "mode": mode,
        "phase_starts": starts,
        "phase_start_skew_seconds": max(starts.values()) - min(starts.values()),
        "overlap_start_timestamp_seconds": overlap_start,
        "overlap_end_timestamp_seconds": overlap_end,
        "snapshot_start": start,
        "snapshot_end": end,
        "counter_deltas": deltas,
        "host_results": links,
        "phase_end_skew_seconds": (
            max(phase_ends) - min(phase_ends) if len(phase_ends) == len(hosts) else None
        ),
        "counter_scope": "ACC and NVMe counters cover selected target ports",
        "attribution_condition": "exclusive target I/O during both snapshots",
        "valid": True,
    }


def main() -> None:
    """Capture one fleet result alongside selected host result JSONs."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--in-flight", type=int, required=True)
    parser.add_argument("--duration-sec", type=float, required=True)
    parser.add_argument("--mode", choices=("load", "mixed"), default="load")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--host-result-template")
    parser.add_argument("--hosts", nargs="+", choices=tuple(HOST_PORTS))
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9-]+", args.run_id):
        parser.error("--run-id must contain only letters, digits, and hyphens")
    if args.host_result_template and args.host_result_template.count("{host}") != 1:
        parser.error("--host-result-template must contain one {host} placeholder")
    hosts = tuple(args.hosts) if args.hosts else tuple(HOST_PORTS)
    if len(hosts) != len(set(hosts)):
        parser.error("--hosts must be unique")
    try:
        result = measure(
            args.run_id,
            args.in_flight,
            args.duration_sec,
            args.host_result_template,
            args.mode,
            hosts,
        )
    except Exception as exc:
        result = {"run_id": args.run_id, "valid": False, "error": str(exc)}
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        raise
    args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
