# SPDX-License-Identifier: Apache-2.0
"""Run the public CLI with an isolated raw adapter status recorder."""

# Standard
from pathlib import Path
from threading import Event, Thread
from typing import Any
import json
import os
import re
import runpy
import time

# First Party
from lmcache.v1.distributed.l2_adapters.config import register_l2_adapter_type
from lmcache.v1.distributed.l2_adapters.factory import register_l2_adapter_factory
from lmcache.v1.distributed.l2_adapters.raw_block_l2_adapter import (
    RawBlockL2Adapter,
    RawBlockL2AdapterConfig,
)


class RecordingRawConfig(RawBlockL2AdapterConfig):
    """Keep the recorder's type through the public JSON/config factory."""

    def __init__(self, **kwargs: Any) -> None:
        """Validate normal raw parameters and select the instrumented factory."""
        super().__init__(**kwargs)
        self.type = "goal5_raw_recording"


class RecordingRawAdapter(RawBlockL2Adapter):
    """Sample the adapter's public status; retain terminal checkpoint/drain state."""

    def __init__(self, config: Any, l1_memory_desc: Any = None) -> None:
        """Construct the ordinary raw adapter and exclusively own its counter file."""
        artifact = os.environ["GOAL5_ARTIFACT"]
        run_id = os.environ["GOAL5_RAW_RUN_ID"]
        if not all(
            re.fullmatch(r"[A-Za-z0-9_-]+", text) for text in (artifact, run_id)
        ):
            raise ValueError("Unsafe raw recorder identity")
        path = (
            Path("/home/goal5-overnight/host-results")
            / run_id
            / f"{artifact}-native.jsonl"
        )
        self._stream = path.open("x")
        try:
            super().__init__(config, l1_memory_desc)
        except BaseException:
            self._stream.close()
            raise
        self._recording_closed = False
        self._stop = Event()
        self._recorder = Thread(target=self._sample, daemon=True)
        self._recorder.start()

    def close(self) -> None:
        """Stop sampling, drain the adapter and retain final checkpoint status."""
        if self._recording_closed:
            return
        self._stop.set()
        self._recorder.join()
        try:
            super().close()
            self._write(True)
        finally:
            self._stream.close()
            self._recording_closed = True

    def _write(self, terminal: bool) -> None:
        self._stream.write(
            json.dumps(
                {
                    "time": time.time(),
                    "status": self.report_status(),
                    "terminal": terminal,
                }
            )
            + "\n"
        )
        self._stream.flush()

    def _sample(self) -> None:
        while not self._stop.is_set():
            self._write(False)
            self._stop.wait(0.1)


if __name__ == "__main__":
    register_l2_adapter_type("goal5_raw_recording", RecordingRawConfig)
    register_l2_adapter_factory("goal5_raw_recording", RecordingRawAdapter)
    runpy.run_module("lmcache.cli.main", run_name="__main__")
