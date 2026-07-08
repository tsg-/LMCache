# SPDX-License-Identifier: Apache-2.0
"""``lmcache rdma`` — standalone RDMA thin-client operations.

Usage::

    lmcache rdma store  --url tcp://server:5601 --tokens 1,2,3,4 --size 64
    lmcache rdma retrieve --url tcp://server:5601 --tokens 1,2,3,4 --size 64
    lmcache rdma roundtrip --url tcp://server:5601 --tokens 1,2,3,4 --size 64
"""

from __future__ import annotations

import argparse
import sys
import time

import torch

from lmcache.cli.commands.base import BaseCommand
from lmcache.v1.platform.rdma.thin_client import RdmaThinClient


class RdmaCommand(BaseCommand):
    """Drive RDMA store/retrieve against an LMCache server (no vLLM)."""

    def name(self) -> str:
        return "rdma"

    def help(self) -> str:
        return "Standalone RDMA thin-client store/retrieve operations."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "action",
            choices=["store", "retrieve", "roundtrip"],
            help="Operation: store, retrieve, or roundtrip (store then retrieve).",
        )
        parser.add_argument(
            "--url",
            default="tcp://localhost:5601",
            help="ZMQ server URL (default: tcp://localhost:5601).",
        )
        parser.add_argument(
            "--tokens",
            required=True,
            help="Comma-separated token IDs for the cache key.",
        )
        parser.add_argument(
            "--size",
            type=int,
            default=16,
            help="Number of float32 elements in the tensor (default: 16).",
        )
        parser.add_argument(
            "--model",
            default="thin-client",
            help="Model name for cache key (default: thin-client).",
        )
        parser.add_argument(
            "--timeout",
            type=float,
            default=30.0,
            help="Timeout in seconds (default: 30).",
        )

    def execute(self, args: argparse.Namespace) -> None:
        token_ids = [int(t.strip()) for t in args.tokens.split(",")]

        with RdmaThinClient(
            server_url=args.url,
            model_name=args.model,
            timeout=args.timeout,
        ) as client:
            if args.action == "store":
                self._do_store(client, token_ids, args)
            elif args.action == "retrieve":
                self._do_retrieve(client, token_ids, args)
            elif args.action == "roundtrip":
                self._do_roundtrip(client, token_ids, args)

    def _do_store(
        self,
        client: RdmaThinClient,
        token_ids: list[int],
        args: argparse.Namespace,
    ) -> None:
        data = torch.arange(args.size, dtype=torch.float32)
        t0 = time.monotonic()
        ok = client.store("cli-store-0", token_ids=token_ids, data=data)
        elapsed_ms = (time.monotonic() - t0) * 1000

        metrics = self.create_metrics("RDMA Store", args, width=40)
        metrics.add("status", "Status", "OK" if ok else "FAIL")
        metrics.add("elapsed_ms", "Elapsed (ms)", round(elapsed_ms, 2))
        metrics.add("size", "Elements", args.size)
        metrics.add("bytes", "Bytes", args.size * 4)
        metrics.emit()

        if not ok:
            sys.exit(1)

    def _do_retrieve(
        self,
        client: RdmaThinClient,
        token_ids: list[int],
        args: argparse.Namespace,
    ) -> None:
        t0 = time.monotonic()
        result = client.retrieve(
            "cli-retrieve-0",
            token_ids=token_ids,
            numel=args.size,
            dtype=torch.float32,
        )
        elapsed_ms = (time.monotonic() - t0) * 1000

        metrics = self.create_metrics("RDMA Retrieve", args, width=40)
        metrics.add("status", "Status", "OK" if result is not None else "MISS")
        metrics.add("elapsed_ms", "Elapsed (ms)", round(elapsed_ms, 2))
        metrics.add("size", "Elements", args.size)
        metrics.emit()

        if result is None:
            sys.exit(1)

    def _do_roundtrip(
        self,
        client: RdmaThinClient,
        token_ids: list[int],
        args: argparse.Namespace,
    ) -> None:
        data = torch.arange(args.size, dtype=torch.float32)

        t0 = time.monotonic()
        store_ok = client.store("cli-rt-store", token_ids=token_ids, data=data)
        store_ms = (time.monotonic() - t0) * 1000

        if not store_ok:
            print("STORE failed", file=sys.stderr)
            sys.exit(1)

        t1 = time.monotonic()
        result = client.retrieve(
            "cli-rt-retrieve",
            token_ids=token_ids,
            numel=args.size,
            dtype=torch.float32,
        )
        retrieve_ms = (time.monotonic() - t1) * 1000

        data_match = result is not None and torch.equal(result, data)

        metrics = self.create_metrics("RDMA Roundtrip", args, width=40)
        metrics.add("store_status", "Store", "OK" if store_ok else "FAIL")
        metrics.add("store_ms", "Store (ms)", round(store_ms, 2))
        metrics.add(
            "retrieve_status",
            "Retrieve",
            "OK" if result is not None else "MISS",
        )
        metrics.add("retrieve_ms", "Retrieve (ms)", round(retrieve_ms, 2))
        metrics.add("data_match", "Data match", "YES" if data_match else "NO")
        metrics.add("size", "Elements", args.size)
        metrics.add("bytes", "Bytes", args.size * 4)
        metrics.emit()

        if not data_match:
            sys.exit(1)
