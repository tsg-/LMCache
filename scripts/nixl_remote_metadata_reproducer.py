#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Probe NIXL prepared handles after a repeated remote-metadata load.

This standalone harness isolates the sequence implicated by LMCache-szh:

1. Load source metadata into a target NIXL agent.
2. Prepare local and remote transfer descriptor lists.
3. Load either identical metadata or metadata from a second source agent with
   the same agent name.
4. Create a transfer using the prepared descriptor handles.

It runs both the second load on the preparation thread and on a new thread.
The ``mismatched`` metadata case creates two independent UCX agent instances
with the same logical name, matching the competing metadata condition without
forging a NIXL metadata blob.

Run this on a host with a working NIXL UCX runtime, for example::

    env UCX_TLS=rc_mlx5,ud_mlx5,sm UCX_NET_DEVICES=mlx5_1:1 \
        NIXL_NET_BACKEND=UCX python scripts/nixl_remote_metadata_reproducer.py
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal

import torch

Variant = Literal["matched", "mismatched"]

_PAGE_BYTES = 4096


@dataclass
class CallResult:
    """Capture the result or exception from one NIXL API call."""

    value: str | None = None
    exception_type: str | None = None
    exception_message: str | None = None


@dataclass
class ProbeResult:
    """Capture the observable result of one metadata reload variant."""

    variant: Variant
    cross_thread: bool
    metadata_equal: bool
    first_load: CallResult
    second_load: CallResult
    prepared_transfer: CallResult
    elapsed_ms: float


def _load_nixl_api() -> tuple[type[Any], type[Any]]:
    """Load the NIXL Python API from a supported package name.

    Returns:
        The ``nixl_agent`` and ``nixl_agent_config`` classes.

    Raises:
        RuntimeError: If no supported NIXL Python package is installed.
    """
    errors: list[str] = []
    for module_name in ("nixl._api", "nixl_cu12._api", "nixl_cu13._api"):
        try:
            module = importlib.import_module(module_name)
        except ImportError as error:
            errors.append(f"{module_name}: {error}")
            continue
        return module.nixl_agent, module.nixl_agent_config
    raise RuntimeError("NIXL Python API is unavailable: " + "; ".join(errors))


def _call(callable_: Any) -> CallResult:
    """Execute one NIXL operation and return its value or raised exception.

    Args:
        callable_: Zero-argument callable invoking a NIXL API method.

    Returns:
        A serializable result for the call.
    """
    try:
        value = callable_()
        if isinstance(value, bytes):
            return CallResult(value=value.decode())
        return CallResult(value=str(value))
    except Exception as error:  # noqa: BLE001
        return CallResult(
            exception_type=type(error).__name__,
            exception_message=str(error),
        )


def _register_page(agent: Any, page: torch.Tensor) -> Any:
    """Register one CPU page and return its NIXL transfer descriptors.

    Args:
        agent: NIXL agent that owns ``page``.
        page: Contiguous CPU page to register.

    Returns:
        NIXL transfer descriptors for the registered page.
    """
    reg_descs = agent.get_reg_descs(
        [(page.data_ptr(), page.numel(), 0, "")],
        "cpu",
    )
    agent.register_memory(reg_descs)
    return agent.get_xfer_descs(
        [(page.data_ptr(), page.numel(), 0)],
        "cpu",
    )


def _run_variant(
    variant: Variant,
    cross_thread: bool,
    nixl_agent: type[Any],
    nixl_agent_config: type[Any],
) -> ProbeResult:
    """Run one repeated-metadata-load experiment.

    Args:
        variant: Whether the second metadata payload is identical or originates
            from a distinct agent instance with the same logical agent name.
        cross_thread: Whether the second metadata load runs on a new thread.
        nixl_agent: NIXL agent class.
        nixl_agent_config: NIXL agent configuration class.

    Returns:
        The observed results for both loads and prepared transfer creation.
    """
    started_at = time.monotonic()
    suffix = f"{os.getpid()}-{threading.get_ident()}-{variant}"
    config = nixl_agent_config(backends=["UCX"])
    target = nixl_agent(f"target-{suffix}", config)
    source_name = f"source-{suffix}"
    source_first = nixl_agent(source_name, config)
    source_second = nixl_agent(source_name, config)

    source_page = torch.arange(256, dtype=torch.uint8).repeat(_PAGE_BYTES // 256)
    target_page = torch.zeros(_PAGE_BYTES, dtype=torch.uint8)
    source_xfer_descs = _register_page(source_first, source_page)
    target_xfer_descs = _register_page(target, target_page)

    first_metadata = source_first.get_agent_metadata()
    second_metadata = (
        first_metadata
        if variant == "matched"
        else source_second.get_agent_metadata()
    )
    first_remote_name: bytes | str | None = None
    try:
        first_remote_name = target.add_remote_agent(first_metadata)
        first_load = CallResult(
            value=(
                first_remote_name.decode()
                if isinstance(first_remote_name, bytes)
                else first_remote_name
            )
        )
    except Exception as error:  # noqa: BLE001
        first_load = CallResult(
            exception_type=type(error).__name__,
            exception_message=str(error),
        )

    if first_remote_name is None:
        return ProbeResult(
            variant=variant,
            cross_thread=cross_thread,
            metadata_equal=first_metadata == second_metadata,
            first_load=first_load,
            second_load=CallResult(),
            prepared_transfer=CallResult(),
            elapsed_ms=(time.monotonic() - started_at) * 1000,
        )

    remote_descs = target.deserialize_descs(
        source_first.get_serialized_descs(source_xfer_descs)
    )
    remote_handle = target.prep_xfer_dlist(first_remote_name, remote_descs)
    local_handle = target.prep_xfer_dlist("", target_xfer_descs)

    if cross_thread:
        second_load_holder: list[CallResult] = []

        def _reload_metadata() -> None:
            second_load_holder.append(
                _call(lambda: target.add_remote_agent(second_metadata))
            )

        thread = threading.Thread(target=_reload_metadata, name="metadata-reload")
        thread.start()
        thread.join()
        second_load = second_load_holder[0]
    else:
        second_load = _call(lambda: target.add_remote_agent(second_metadata))

    prepared_transfer = _call(
        lambda: target.make_prepped_xfer(
            "READ",
            local_handle,
            [0],
            remote_handle,
            [0],
        )
    )
    return ProbeResult(
        variant=variant,
        cross_thread=cross_thread,
        metadata_equal=first_metadata == second_metadata,
        first_load=first_load,
        second_load=second_load,
        prepared_transfer=prepared_transfer,
        elapsed_ms=(time.monotonic() - started_at) * 1000,
    )


def _parse_args() -> argparse.Namespace:
    """Parse command-line options for the reproducer.

    Returns:
        Parsed options.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--variant",
        choices=("matched", "mismatched", "all"),
        default="all",
        help="Metadata payloads to test (default: all).",
    )
    parser.add_argument(
        "--cross-thread",
        action="store_true",
        help="Only run the second metadata load on a new thread.",
    )
    return parser.parse_args()


def main() -> int:
    """Run the selected NIXL remote-metadata experiments.

    Returns:
        Zero after emitting experiment records. Individual NIXL failures are
        recorded in the JSON output, not treated as harness failures.
    """
    args = _parse_args()
    nixl_agent, nixl_agent_config = _load_nixl_api()
    variants: tuple[Variant, ...] = (
        ("matched", "mismatched") if args.variant == "all" else (args.variant,)
    )
    thread_modes = (True,) if args.cross_thread else (False, True)

    for variant in variants:
        for cross_thread in thread_modes:
            result = _run_variant(
                variant,
                cross_thread,
                nixl_agent,
                nixl_agent_config,
            )
            print(json.dumps(asdict(result), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
