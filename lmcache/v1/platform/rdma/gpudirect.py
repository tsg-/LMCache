# SPDX-License-Identifier: Apache-2.0
"""GPUDirect RDMA support for direct NIC-to-GPU-HBM data transfer.

When ``LMCACHE_RDMA_GPUDIRECT=1`` is set and CUDA is available, the retrieve
path can bypass host DRAM entirely:

    Target DRAM --RDMA Read--> GPU HBM (KV cache blocks) directly

:class:`GpuDirectBuffer` wraps a CUDA device allocation that has been
registered as an ibverbs MR.  It duck-types :class:`RegisteredBuffer` so it
can be passed directly to :meth:`RdmaTransport.post_read` as ``local_buf``.
"""

from __future__ import annotations

import os
import weakref

import torch

from lmcache.logging import init_logger
from lmcache.v1.platform.rdma.rdma_transport import MrInfo

logger = init_logger(__name__)

# Detect pyverbs availability (mirrors verbs_transport.py guard).
try:
    import pyverbs  # noqa: F401
    _HAS_PYVERBS: bool = True
except ImportError:
    _HAS_PYVERBS: bool = False


def is_gpudirect_available() -> bool:
    """Return True if GPUDirect RDMA can be used on this process.

    The check is intentionally cheap: it reads an env var and queries
    torch.cuda.is_available() — no allocation, no syscall, safe to call
    on every request.

    Returns:
        True when all three preconditions are met:
        - ``LMCACHE_RDMA_GPUDIRECT=1`` is set in the environment
        - ``torch.cuda.is_available()`` returns True
        - pyverbs is installed (``HAS_PYVERBS`` is True)
    """
    if os.environ.get("LMCACHE_RDMA_GPUDIRECT", "0") != "1":
        return False
    if not torch.cuda.is_available():
        return False
    if not _HAS_PYVERBS:
        return False
    return True


class GpuDirectBuffer:
    """A CUDA device memory allocation registered as an RDMA MR.

    Allocates ``length`` bytes of CUDA device memory on ``device``,
    registers the underlying physical pages as an ibverbs MR via the
    transport, and provides a ``to_tensor()`` method to wrap the result
    as a torch tensor without any host-memory copy.

    The class intentionally duck-types :class:`~lmcache.v1.platform.rdma.rdma_transport.RegisteredBuffer`
    by exposing ``addr`` and ``mr`` as top-level attributes, so an instance
    can be passed directly as ``local_buf`` to
    :meth:`~lmcache.v1.platform.rdma.rdma_transport.RdmaTransport.post_read`.

    Args:
        length: Number of bytes to allocate.
        dtype: torch dtype for the resulting tensor.
        shape: Tensor shape (must be consistent with ``length`` and ``dtype``).
        device: CUDA device index.
        transport: An object satisfying the :class:`RdmaTransport` protocol
            (accepts ``object`` to avoid a circular import from
            ``verbs_transport``).

    Raises:
        RuntimeError: If CUDA is not available or MR registration fails.
    """

    def __init__(
        self,
        length: int,
        dtype: torch.dtype,
        shape: tuple[int, ...],
        device: int,
        transport: object,
    ) -> None:
        if not torch.cuda.is_available():
            raise RuntimeError("GpuDirectBuffer requires CUDA")

        # Allocate GPU memory and keep the tensor alive for the buffer's lifetime.
        nbytes_per_element = torch.empty(0, dtype=dtype).element_size()
        numel = length // nbytes_per_element
        self._tensor = torch.empty(numel, dtype=dtype, device=f"cuda:{device}")
        self._length = length
        self._dtype = dtype
        self._shape = shape

        # Register the device-memory pointer as an RDMA MR.
        data_ptr = self._tensor.data_ptr()
        self._mr_info: MrInfo = transport.register_mr(data_ptr, length)  # type: ignore[union-attr]
        self._transport = transport
        self._closed = False

        logger.debug(
            "GpuDirectBuffer: registered GPU MR rkey=%d addr=0x%x len=%d",
            self._mr_info.rkey,
            self._mr_info.addr,
            length,
        )

    # --- duck-typing RegisteredBuffer interface ----------------------------

    @property
    def addr(self) -> int:
        """Device memory base address (for SGE/wr construction).

        Returns:
            The raw CUDA device pointer as an integer.
        """
        return self._tensor.data_ptr()

    @property
    def mr(self) -> MrInfo:
        """Registered MR info.

        Returns:
            The :class:`MrInfo` obtained when this buffer was created.
        """
        return self._mr_info

    # --- tensor view -------------------------------------------------------

    def to_tensor(self) -> torch.Tensor:
        """Return a zero-copy torch tensor backed by this device buffer.

        No host-memory copy is performed.  The returned tensor is a view
        over the existing CUDA allocation — it shares the same storage.

        Returns:
            A CUDA tensor of ``dtype`` and ``shape`` whose data was
            written directly into GPU HBM by the RDMA Read.
        """
        typed = self._tensor.view(self._dtype)
        return torch.as_strided(typed, self._shape, _compact_strides(self._shape))

    # --- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Deregister MR and release the CUDA memory allocation.

        Idempotent: safe to call more than once.
        """
        if self._closed:
            return
        self._closed = True
        try:
            self._transport.deregister_mr(self._mr_info)  # type: ignore[union-attr]
        except Exception:
            logger.warning(
                "GpuDirectBuffer: deregister_mr failed for rkey=%d (ignored)",
                self._mr_info.rkey,
                exc_info=True,
            )
        # Release the CUDA tensor — Python GC frees GPU memory.
        self._tensor = None  # type: ignore[assignment]
        logger.debug(
            "GpuDirectBuffer: closed rkey=%d", self._mr_info.rkey
        )


def _compact_strides(shape: tuple[int, ...]) -> tuple[int, ...]:
    """Compute C-contiguous strides for the given shape.

    Args:
        shape: Tensor shape dimensions.

    Returns:
        Tuple of strides in element units (not bytes), contiguous row-major.
    """
    strides: list[int] = []
    stride = 1
    for dim in reversed(shape):
        strides.append(stride)
        stride *= dim
    return tuple(reversed(strides))


def allocate_gpudirect_buffer(
    length: int,
    dtype: torch.dtype,
    shape: tuple[int, ...],
    transport: object,
    device: int = 0,
) -> GpuDirectBuffer | None:
    """Attempt to allocate a GPU-direct RDMA buffer.

    If GPUDirect is unavailable (env var not set, CUDA absent, pyverbs
    missing) or if allocation / MR registration fails, returns ``None``
    without raising.  The caller falls back to the host-DRAM path.

    Args:
        length: Number of bytes to allocate.
        dtype: torch dtype for the tensor view.
        shape: Tensor shape.
        transport: RdmaTransport instance for MR registration.
        device: CUDA device index (default 0).

    Returns:
        A :class:`GpuDirectBuffer`, or ``None`` if GPUDirect is unavailable
        or allocation failed.
    """
    if not is_gpudirect_available():
        return None
    try:
        return GpuDirectBuffer(
            length=length,
            dtype=dtype,
            shape=shape,
            device=device,
            transport=transport,
        )
    except Exception:
        logger.warning(
            "GPUDirect buffer allocation failed; falling back to host-DRAM path",
            exc_info=True,
        )
        return None
