# SPDX-License-Identifier: Apache-2.0
"""Select an exclusively owned recorder file for each fleet benchmark process."""

# Standard
from pathlib import Path
from typing import Any
import os
import re

# Third Party
from goal5_async_stats import RecordingFSUringClient


class FleetRecordingClient(RecordingFSUringClient):
    """Keep the adapter configuration stable while separating attempt counters."""

    def __init__(self, stats_directory: str, **kwargs: Any) -> None:
        """Record public native counters under the launcher's owned artifact name."""
        artifact = os.environ["GOAL5_ARTIFACT"]
        if not re.fullmatch(r"[A-Za-z0-9_-]+", artifact):
            raise ValueError("Unsafe native counter artifact")
        super().__init__(
            stats_path=str(Path(stats_directory) / f"{artifact}-native.jsonl"),
            **kwargs,
        )
