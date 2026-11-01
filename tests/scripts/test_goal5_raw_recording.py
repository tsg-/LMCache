# SPDX-License-Identifier: Apache-2.0
"""The recorder must survive public JSON parsing and factory type selection."""

# Standard
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts/ipu-poc"))


def test_recording_config_retains_its_public_factory_type() -> None:
    """An alias alone must not silently select the uninstrumented raw factory."""
    # Third Party
    from goal5_raw_recording import RecordingRawConfig

    config = RecordingRawConfig.from_dict(
        {
            "type": "goal5_raw_recording",
            "device_path": "/dev/md0",
            "slot_bytes": 69632,
            "io_engine": "io_uring",
            "use_uring_cmd": False,
        }
    )
    assert isinstance(config, RecordingRawConfig)
    assert config.type == "goal5_raw_recording"
    selected = config.to_core_config()
    assert selected.device_path == "/dev/md0"
    assert selected.io_engine == "io_uring" and selected.use_odirect
    assert selected.slot_bytes == 69632 and not selected.use_uring_cmd
