#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Small helpers for the NVMe-oF lifecycle shell scripts (alt-track LMCache-msm.1).

Currently exposes one subcommand:

    find-controller --nqn <nqn> [--json-file PATH]

Reads a `nvme list-subsys -o json` blob (from --json-file or stdin) and
prints the first controller device name for the requested subsystem NQN.
Handles both output shapes:

- nvme-cli 1.x: subsystem entries contain a ``Controllers`` list with
  ``Controller`` fields (e.g. ``"nvme0"``).
- nvme-cli 2.x: subsystem entries contain a ``Paths`` list with ``Name``
  fields (e.g. ``"nvme0"``). Some versions still emit ``Controllers`` as
  an alias; both are checked.

Exits 0 with the device name on stdout on success. Exits 1 with no output
if the NQN is not present. Exits 2 with a diagnostic on stderr for
malformed JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Iterable


def _iter_subsystems(payload: object) -> Iterable[dict]:
    """Yield subsystem dicts from any of the shapes nvme-cli returns.

    nvme-cli 1.x wraps output as ``{"Subsystems": [...]}``. nvme-cli 2.x
    typically returns a list at the top level, and each entry may itself
    contain a nested ``Subsystems`` list (per-host grouping). We flatten
    both layouts.
    """
    if isinstance(payload, dict):
        entries = payload.get("Subsystems") or []
    elif isinstance(payload, list):
        entries = payload
    else:
        return
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        nested = entry.get("Subsystems")
        if isinstance(nested, list):
            for sub in nested:
                if isinstance(sub, dict):
                    yield sub
        else:
            yield entry


def _controller_names(subsystem: dict) -> Iterable[str]:
    """Yield candidate controller device names in a subsystem entry.

    Handles both the v1 ``Controllers[].Controller`` shape and the v2
    ``Paths[].Name`` shape.
    """
    for key in ("Controllers", "Paths"):
        entries = subsystem.get(key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            name = entry.get("Controller") or entry.get("Name")
            if isinstance(name, str) and name:
                yield name


def find_controller(payload: object, target_nqn: str) -> str | None:
    """Return the first controller name for the subsystem matching target_nqn."""
    for sub in _iter_subsystems(payload):
        nqn = sub.get("NQN") or sub.get("Subsystem NQN") or sub.get("SubsystemNQN")
        if nqn != target_nqn:
            continue
        for name in _controller_names(sub):
            return name
    return None


def _load_json(path: str | None) -> object:
    if path in (None, "-"):
        return json.load(sys.stdin)
    with open(path) as fh:
        return json.load(fh)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    fc = sub.add_parser(
        "find-controller",
        help="Print the first controller device name for the requested NQN.",
    )
    fc.add_argument("--nqn", required=True)
    fc.add_argument(
        "--json-file",
        default=None,
        help="Path to a `nvme list-subsys -o json` dump; '-' or omitted reads stdin.",
    )

    args = parser.parse_args(argv)

    if args.cmd == "find-controller":
        try:
            payload = _load_json(args.json_file)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"nvmeof_util: failed to read JSON: {exc}", file=sys.stderr)
            return 2
        controller = find_controller(payload, args.nqn)
        if controller is None:
            return 1
        print(controller)
        return 0

    parser.error(f"unknown command: {args.cmd}")
    return 2  # unreachable


if __name__ == "__main__":
    sys.exit(main())
