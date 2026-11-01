# SPDX-License-Identifier: Apache-2.0
"""Record public native counters without reaching through the L2 adapter."""

# Standard
from pathlib import Path
from threading import Event, Thread
from typing import Any
import json
import time

# Third Party
from lmcache_fs_uring import LMCacheFSUringClient


class RecordingFSUringClient(LMCacheFSUringClient):
    """Add a 100ms counter stream to the existing native plugin interface."""

    def __init__(self, stats_path: str, **kwargs: Any) -> None:
        """Construct the native client and exclusively create its counter stream."""
        self._stream = Path(stats_path).open("x")
        try:
            super().__init__(**kwargs)
        except BaseException:
            self._stream.close()
            raise
        self._stop = Event()
        self._closed = False
        self._thread = Thread(target=self._sample, daemon=True)
        self._thread.start()

    def close(self) -> None:
        """Drain native work, join the recorder, and retain terminal counters."""
        if self._closed:
            return
        super().close()
        self._stop.set()
        self._thread.join()
        self._write(True)
        self._stream.close()
        self._closed = True

    def _write(self, terminal: bool) -> None:
        self._stream.write(
            json.dumps(
                {"time": time.time(), "stats": self.io_stats(), "terminal": terminal}
            )
            + "\n"
        )
        self._stream.flush()

    def _sample(self) -> None:
        while not self._stop.is_set():
            self._write(False)
            self._stop.wait(0.1)
