# SPDX-License-Identifier: Apache-2.0
"""Raw FIO controls must stay beyond the LMCache capacity boundary."""

# Standard
from pathlib import Path
import sys

# Third Party
import pytest

sys.path.insert(0, str(Path(__file__).parents[2] / "scripts/ipu-poc"))


def test_fio_jobs_own_disjoint_tail_ranges() -> None:
    """A control must never overwrite the retained raw index or KV slots."""
    # Third Party
    from goal5_raw_fio import fio_args

    cfg = {
        "backend": "raw_io_uring",
        "repetitions": 2,
        "raw_capacity_bytes": {"mmgi1": 8 * 1024**4},
        "hosts": {"mmgi1": {"numa": 1}},
    }
    argv = fio_args(cfg, "mmgi1", "256k", "mixed")
    assert "--filename=/dev/md0" in argv
    assert "--offset=8796093022208" in argv
    assert "--offset_increment=17179869184" in argv
    assert "--size=17179869184" in argv
    assert "--numjobs=32" in argv and "--ioengine=io_uring" in argv
    assert "--direct=1" in argv and "--rwmixread=83" in argv
    assert not any("uring_cmd" in arg for arg in argv)
    cfg["repetitions"] = 3
    with pytest.raises(ValueError, match="two repetitions"):
        fio_args(cfg, "mmgi1", "64k", "read")
