# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the NIXL benchmark runner topology constants."""

from __future__ import annotations

# Standard
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


SCRIPTS_DIR = Path(__file__).parents[2] / "scripts"


def _load_module(name: str) -> ModuleType:
    path = SCRIPTS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_load_module("bench_verify")
runner = _load_module("nixl_bench_runner")


def test_bmg1_uses_the_rdma_fabric_port() -> None:
    """bmg1's f1 port is management; NIXL must use 192.168.200 on f0."""
    assert runner.BMG1_UCX_NET_DEV == "rocep153s0f0:1"
    assert runner.BMG1_ETHTOOL_IFACE == "ens1f0np0"
    assert "UCX_NET_DEVICES=rocep153s0f0:1" in runner.ucx_env("bmg1")
    assert "UCX_NET_DEVICES=rocep153s0f0:1" in runner.ucx_env("dev@192.168.200.4")
