# SPDX-License-Identifier: Apache-2.0
"""Integration test for ``lmcache bench server --mode rdma``.

Spins up a real MPCacheServer in RDMA mode (StubRdmaTransport) and
runs the bench command against it, verifying store/retrieve roundtrip.
"""

from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import time
from typing import Generator

import pytest

from lmcache.v1.distributed.config import (
    EvictionConfig,
    L1ManagerConfig,
    L1MemoryManagerConfig,
    StorageManagerConfig,
)
from lmcache.v1.mp_observability.config import DEFAULT_OBSERVABILITY_CONFIG
from lmcache.v1.multiprocess.config import MPServerConfig
from lmcache.v1.multiprocess.server import run_cache_server


SERVER_HOST = "localhost"
SERVER_PORT = 5602
SERVER_URL = f"tcp://{SERVER_HOST}:{SERVER_PORT}"


def _server_process_runner(host: str, port: int) -> None:
    """Entry point for the RDMA-mode server subprocess."""
    os.environ["LMCACHE_RDMA_TRANSPORT"] = "stub"
    mp_config = MPServerConfig(
        host=host,
        port=port,
        chunk_size=4,
        supported_transfer_mode="rdma",
    )
    storage_manager_config = StorageManagerConfig(
        l1_manager_config=L1ManagerConfig(
            memory_config=L1MemoryManagerConfig(
                size_in_bytes=64 * 1024 * 1024,
                use_lazy=False,
            ),
        ),
        eviction_config=EvictionConfig(eviction_policy="LRU"),
    )
    run_cache_server(
        mp_config=mp_config,
        storage_manager_config=storage_manager_config,
        obs_config=DEFAULT_OBSERVABILITY_CONFIG,
        start_prometheus_http_server=False,
    )


@pytest.fixture(scope="module")
def rdma_server() -> Generator[mp.Process, None, None]:
    """Start a real MPCacheServer subprocess in RDMA mode."""
    mp.set_start_method("spawn", force=True)
    process = mp.Process(
        target=_server_process_runner,
        args=(SERVER_HOST, SERVER_PORT),
        daemon=True,
    )
    process.start()
    time.sleep(2)
    yield process
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        if process.is_alive():
            process.kill()
            process.join()


class TestRdmaBenchMode:
    """Exercises the --mode rdma bench path end-to-end."""

    def test_rdma_bench_roundtrip(self, rdma_server: mp.Process) -> None:
        """Run 3 iterations and verify all pass."""
        os.environ["LMCACHE_RDMA_TRANSPORT"] = "stub"

        from lmcache.cli.commands.bench.server_bench.command import run_server_bench
        from lmcache.cli.commands.base import BaseCommand

        class _FakeCommand(BaseCommand):
            def name(self) -> str:
                return "test"

            def help(self) -> str:
                return ""

            def add_arguments(self, parser: argparse.ArgumentParser) -> None:
                pass

            def execute(self, args: argparse.Namespace) -> None:
                pass

        args = argparse.Namespace(
            rpc_url=SERVER_URL,
            mode="rdma",
            num_tokens=4,
            start=0,
            end=3,
            interval=0.0,
            timeout=10.0,
            format=None,
            output=None,
            quiet=True,
        )

        # Should not raise; internally verifies data match
        run_server_bench(_FakeCommand(), args)
