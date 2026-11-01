# SPDX-License-Identifier: Apache-2.0
"""Raw measurements require native drain and durable checkpoints."""

# Standard
from copy import deepcopy
from pathlib import Path
from typing import Any
import hashlib
import os
import sys

# Third Party
import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts/ipu-poc"))


def test_native_raw_status_rejects_pending_or_unpersisted_checkpoint() -> None:
    """Successful payload cannot excuse a pending queue or rejected checkpoint."""
    # Third Party
    from goal5_raw_probe import native_summary

    rows: list[dict[str, Any]] = [
        {
            "time": i / 10,
            "terminal": False,
            "status": {
                "core": {
                    "native_io": {
                        "issued_pending": 16,
                        "outstanding_requests": 32,
                        "peak_issued_pending": 32,
                    },
                    "inflight_io_count": 0,
                    "locked_key_count": 0,
                    "metadata_dirty_total": 56,
                    "metadata_persisted": 56,
                }
            },
        }
        for i in range(11)
    ]
    terminal = deepcopy(rows[-1])
    terminal.update(time=1.1, terminal=True)
    terminal["status"]["core"]["native_io"].update(
        issued_pending=0, outstanding_requests=0
    )
    rows.append(terminal)
    assert native_summary(rows, 0, 1)["mean_sampled_issued_pending"] == 16
    terminal["status"]["core"]["metadata_persisted"] = 55
    with pytest.raises(ValueError, match="checkpoint"):
        native_summary(rows, 0, 1)
    terminal["status"]["core"]["metadata_persisted"] = 56
    terminal["status"]["core"]["native_io"]["outstanding_requests"] = 1
    with pytest.raises(ValueError, match="drain"):
        native_summary(rows, 0, 1)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux restricted policy gate")
def test_gate_hashes_the_loaded_extension_not_its_package_facade() -> None:
    """A wheel's __init__.py hash cannot establish the Rust implementation identity."""
    # Third Party
    native = pytest.importorskip("lmcache_rust_raw_block_io")
    implementation = pytest.importorskip(
        "lmcache_rust_raw_block_io.lmcache_rust_raw_block_io"
    )
    # Third Party
    from goal5_raw_probe import gate

    assert native.RawBlockDevice is implementation.RawBlockDevice
    if Path("/proc/sys/kernel/io_uring_disabled").read_text().strip() != "1":
        pytest.skip("Test requires the authorized restricted lab policy")
    path = Path(implementation.__file__)
    cfg = {
        "backend": "raw_io_uring",
        "repetitions": 2,
        "raw_artifacts": {"mmgi1": {}},
        "hosts": {"mmgi1": {"python": sys.executable, "repo": os.getcwd()}},
        "raw_module_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }
    evidence = gate(cfg, "mmgi1")
    assert evidence["path"] == str(path)
    assert evidence["sha256"] == cfg["raw_module_sha256"]
