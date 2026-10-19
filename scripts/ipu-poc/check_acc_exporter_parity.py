# SPDX-License-Identifier: Apache-2.0
"""Compare scrape-time ACC reads with the old textfile loop under load."""

from __future__ import annotations

# Standard
import argparse
import json
import re
import time
from pathlib import Path
from urllib.request import urlopen


FIELD = re.compile(
    r'^acc_tele_field\{acc="(acc[1-4])",section="([^"]+)",'
    r'field="([^"]+)"\} (\S+)$'
)


def parse_fields(text: str) -> dict[str, int | float]:
    """Parse every field using its ACC, section, and field labels."""
    fields: dict[str, int | float] = {}
    for line in text.splitlines():
        match = FIELD.match(line)
        if match:
            key = "/".join(match.group(1, 2, 3))
            raw = match.group(4)
            fields[key] = float(raw) if "." in raw or "e" in raw.lower() else int(raw)
    return fields


def loop_fields(directory: Path) -> dict[str, int | float]:
    """Read the four legacy loop snapshots from node_exporter's directory."""
    text = "".join(
        (directory / f"acc_grpc_acc{i}.prom").read_text(encoding="ascii")
        for i in range(1, 5)
    )
    return parse_fields(text)


def check_parity(directory: Path, duration: float) -> dict[str, object]:
    """Measure per-field disagreement and loop increments for one loaded window."""
    started = time.time()
    deadline = time.monotonic() + duration
    differences: dict[str, int | float] = {}
    increments: dict[str, int | float] = {}
    decreases: dict[str, int] = {}
    previous: dict[str, int | float] | None = None
    attempts = 0
    errors: list[str] = []
    while time.monotonic() < deadline:
        attempts += 1
        try:
            with urlopen("http://127.0.0.1:9188/metrics", timeout=2) as response:
                live = parse_fields(response.read().decode("ascii"))
            old = loop_fields(directory)
            if len(live) != 300 or set(live) != set(old):
                raise ValueError(
                    f"field set mismatch: live={len(live)} loop={len(old)}"
                )
            for key, value in live.items():
                differences[key] = max(differences.get(key, 0), abs(value - old[key]))
            if previous is not None:
                for key, value in old.items():
                    step = value - previous[key]
                    increments[key] = max(increments.get(key, 0), abs(step))
                    if step < 0 and key.endswith(
                        ("/bytes_from_ulp_rc", "/bytes_to_ulp")
                    ):
                        decreases[key] = decreases.get(key, 0) + 1
            previous = old
        except Exception as exc:
            errors.append(f"sample {attempts}: {exc}")
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(1.0, remaining))
    failing = {
        key: {"max_difference": diff, "max_loop_increment": increments.get(key, 0)}
        for key, diff in differences.items()
        if diff > increments.get(key, 0)
    }
    return {
        "start_timestamp_seconds": started,
        "end_timestamp_seconds": time.time(),
        "attempts": attempts,
        "fields_compared": len(differences),
        "max_difference_by_field": differences,
        "max_loop_increment_by_field": increments,
        "byte_counter_decreases": decreases,
        "failures": failing,
        "sample_errors": errors,
        "passed": len(differences) == 300
        and not failing
        and not decreases
        and not errors
        and time.time() - started >= 600,
    }


def main() -> None:
    """Run the parity gate and write its full evidence as JSON."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--textfile-dir", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=600)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = check_parity(args.textfile_dir, args.duration)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(
        f"passed={result['passed']} fields={result['fields_compared']} "
        f"attempts={result['attempts']} failures={len(result['failures'])} "
        f"errors={len(result['sample_errors'])}"
    )
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
