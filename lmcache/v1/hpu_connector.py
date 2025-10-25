# Copyright 2024-2025 LMCache Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Standard
from typing import List, Optional, Tuple, Union
import abc

# Third Party
import torch

# First Party
from lmcache.integration.vllm.utils import ENGINE_NAME
from lmcache.logging import init_logger
from lmcache.utils import _lmcache_nvtx_annotate
from lmcache.v1.compute.blend.utils import LMCBlenderBuilder
from lmcache.v1.memory_management import GPUMemoryAllocator  # noqa: E501
from lmcache.v1.memory_management import MemoryFormat, MemoryObj
from lmcache.v1.gpu_connector import GPUConnectorInterface

try:
    # First Party
    import lmcache.c_ops as lmc_ops
except (ModuleNotFoundError, ImportError):
    lmc_ops = None


logger = init_logger(__name__)


class VLLMPagedMemHPUConnectorV2(GPUConnectorInterface):
    """
    The GPU KV cache should be a nested tuple of K and V tensors.
    More specifically, we have:
    - GPUTensor = Tuple[KVLayer, ...]
    - KVLayer = Tuple[Tensor, Tensor]
    - Tensor: [num_blocks, block_size, num_heads, head_size]
    It will produce / consume memory object with KV_2LTD format
    """
    def __init__(
        self,
        hidden_dim_size: int,
        num_layers: int,
        use_gpu: bool = False,
        **kwargs,
    ):
        """
        If use_gpu is true, it will create a gpu intermediate buffer. In this
        case, it requires the following kwargs:
        - chunk_size: The MAX size of the chunk to be copied to GPU.
        - dtype: The data type of the intermediate buffer.
        """
        self.hidden_dim_size = hidden_dim_size
        self.num_layers = num_layers
        self.kv_cache_pointers = torch.empty(
            num_layers, dtype=torch.int64, device="cpu"
        )
        # Not sure we need a dict here. Maybe a single GPU connector always
        # works with a single device?
        self.kv_cache_pointers_on_gpu: dict[int, torch.Tensor] = {}
        self.page_buffer_size = 0
        self.kvcaches: Optional[List[torch.Tensor]] = None
        self.gpu_buffer: Optional[torch.Tensor] = None
        self.use_mla = "use_mla" in kwargs and kwargs["use_mla"]
        if use_gpu:
            assert "chunk_size" in kwargs, (
                "chunk_size should be provided to create a GPU buffer."
            )
            assert "dtype" in kwargs, "dtype should be provided to create a GPU buffer."
            assert "device" in kwargs, (
                "device should be provided to create a GPU buffer."
            )
            shape = self.get_shape(kwargs["chunk_size"])
            self.gpu_buffer = torch.empty(
                shape, dtype=kwargs["dtype"], device=kwargs["device"]
            )

    def _initialize_pointers(self, kv_caches: List[torch.Tensor]) -> torch.Tensor:
        self.kv_cache_pointers.numpy()[:] = [t.data_ptr() for t in kv_caches]
        device = kv_caches[0].device
        # Allow both CUDA and HPU devices
        assert device.type in ("cuda", "hpu"), "The device should be CUDA or HPU."
        idx = device.index
        if idx not in self.kv_cache_pointers_on_gpu:
            self.kv_cache_pointers_on_gpu[idx] = torch.empty(
                self.num_layers, dtype=torch.int64, device=device
            )
        self.kv_cache_pointers_on_gpu[idx].copy_(self.kv_cache_pointers)
        if self.use_mla:
            # kv_caches[0].shape: [num_pages, page_size, head_size]
            assert kv_caches[0].dim() == 3
            self.page_buffer_size = kv_caches[0].shape[0] * kv_caches[0].shape[1]
        else:
            # kv_caches[0].shape: [2, num_pages, page_size, num_heads, head_size]
            assert kv_caches[0].dim() == 5
            self.page_buffer_size = kv_caches[0].shape[1] * kv_caches[0].shape[2]
        return self.kv_cache_pointers_on_gpu[idx]

    @_lmcache_nvtx_annotate
    def to_gpu(self, memory_obj: MemoryObj, start: int, end: int, **kwargs):
        """Expect a kwarg 'kvcaches' which is a nested tuple of K and V tensors.
        The kvcaches should correspond to the "WHOLE token sequence".
        Note:
          1. This function expects the 'slot_mapping' is a "full slot mapping"
             where it's length is the same as the whole token sequence.
          2. In the case that there is prefix caching, slot_mapping will starts
             with -1s until the end of the matched prefix. The start and end
             should NEVER overlap with the prefix caching (which means the
             underlying CUDA kernel will never see -1 in slot_mapping)
        :raises ValueError: If 'kvcaches' is not provided in kwargs.
        :raises AssertionError: If the memory object does not have a tensor.
        :raises ValueError: If 'slot_mapping' is not provided in kwargs.
        """
        assert memory_obj.tensor is not None
        self.initialize_kvcaches_ptr(**kwargs)
        assert self.kvcaches is not None, (
            "kvcaches should be provided in kwargs or initialized beforehand."
        )
        if self.use_mla:
            if memory_obj.metadata.fmt != MemoryFormat.KV_MLA_FMT:
                raise ValueError(
                    "The memory object should be in KV_MLA_FMT format in"
                    " order to be processed by VLLMPagedMemGPUConnector"
                )
        else:
            if memory_obj.metadata.fmt != MemoryFormat.KV_2LTD:
                raise ValueError(
                    "The memory object should be in KV_2LTD format in"
                    " order to be processed by VLLMPagedMemGPUConnector"
                )
        if "slot_mapping" not in kwargs:
            raise ValueError("'slot_mapping' should be provided in kwargs.")

        slot_mapping: torch.Tensor = kwargs["slot_mapping"]
        if lmc_ops:
            kv_cache_pointers = self._initialize_pointers(self.kvcaches)
            lmc_ops.multi_layer_kv_transfer(  # type: ignore
                memory_obj.tensor,
                kv_cache_pointers,
                slot_mapping[start:end],
                self.kvcaches[0].device,
                self.page_buffer_size,
                False,
                self.use_mla,
            )
        else:
            # Manual fallback path (no extension)
            if self.gpu_buffer is not None:
                assert self.gpu_buffer.device == self.kvcaches[0].device
                tmp_gpu_buffer = self.gpu_buffer[:, :, : end - start, :]
                tmp_gpu_buffer[0] = memory_obj.tensor[0].to(slot_mapping.device)
                tmp_gpu_buffer[1] = memory_obj.tensor[1].to(slot_mapping.device)
                # kvcaches[i] shape: [2, num_pages, page_size, num_heads, head_size]
                num_pages = self.kvcaches[0].shape[1]
                page_size = self.kvcaches[0].shape[2]
                num_heads = self.kvcaches[0].shape[3]
                head_size = self.kvcaches[0].shape[4]
                page_buffer_size = num_pages * page_size
                hd_shape = num_heads * head_size
                for i in range(len(self.kvcaches)):
                    self.kvcaches[i][0].view(page_buffer_size, hd_shape).index_copy_(
                        0, slot_mapping[start:end], tmp_gpu_buffer[0][i]
                    )
                    self.kvcaches[i][1].view(page_buffer_size, hd_shape).index_copy_(
                        0, slot_mapping[start:end], tmp_gpu_buffer[1][i]
                    )

    @_lmcache_nvtx_annotate
    def from_gpu(self, memory_obj: MemoryObj, start: int, end: int, **kwargs):
        """Expect a kwarg 'kvcaches' which is a nested tuple of K and V tensors.
        The kvcaches should correspond to the "WHOLE token sequence".
        Will set the memory_obj.metadata.fmt to MemoryFormat.KV_2LTD.
        Note:
          1. This function expects the 'slot_mapping' is a "full slot mapping"
             where it's length is the same as the whole token sequence.
          2. In the case that there is prefix caching, slot_mapping will starts
             with -1s until the end of the matched prefix. The start and end
             should NEVER overlap with the prefix caching (which means the
             underlying CUDA kernel will never see -1 in slot_mapping)
        :raises ValueError: If 'kvcaches' is not provided in kwargs,
        :raises AssertionError: If the memory object does not have a tensor.
        :raises ValueError: If 'slot_mapping' is not provided in kwargs.
        """
        assert memory_obj.tensor is not None
        self.initialize_kvcaches_ptr(**kwargs)
        assert self.kvcaches is not None, (
            "kvcaches should be provided in kwargs or initialized beforehand."
        )
        if "slot_mapping" not in kwargs:
            raise ValueError("'slot_mapping' should be provided in kwargs.")

        slot_mapping: torch.Tensor = kwargs["slot_mapping"]
        if lmc_ops:
            kv_cache_pointers = self._initialize_pointers(self.kvcaches)
            if self.gpu_buffer is None or end - start != self.gpu_buffer.shape[2]:
                lmc_ops.multi_layer_kv_transfer(  # type: ignore
                    memory_obj.tensor,
                    kv_cache_pointers,
                    slot_mapping[start:end],
                    self.kvcaches[0].device,
                    self.page_buffer_size,
                    True,
                    self.use_mla,
                )
            else:
                # kvcaches -> gpu_buffer -> memobj
                assert self.gpu_buffer.device == self.kvcaches[0].device
                tmp_gpu_buffer = self.gpu_buffer[:, :, : end - start, :]
                lmc_ops.multi_layer_kv_transfer(  # type: ignore
                    tmp_gpu_buffer,
                    kv_cache_pointers,
                    slot_mapping[start:end],
                    self.kvcaches[0].device,
                    self.page_buffer_size,
                    True,
                    self.use_mla,
                )
                memory_obj.tensor.copy_(tmp_gpu_buffer, non_blocking=True)
        else:
            # Manual fallback path (no extension)
            if self.gpu_buffer is not None:
                assert self.gpu_buffer.device == self.kvcaches[0].device
                tmp_gpu_buffer = self.gpu_buffer[:, :, : end - start, :]
                # kvcaches[i] shape: [2, num_pages, page_size, num_heads, head_size]
                num_pages = self.kvcaches[0].shape[1]
                page_size = self.kvcaches[0].shape[2]
                num_heads = self.kvcaches[0].shape[3]
                head_size = self.kvcaches[0].shape[4]
                page_buffer_size = num_pages * page_size
                hd_shape = num_heads * head_size
                layers = range(len(self.kvcaches))
                tmp_gpu_buffer[0] = torch.stack(
                    tuple(
                        self.kvcaches[i][0]
                        .view(page_buffer_size, hd_shape)
                        .index_select(0, slot_mapping[start:end])
                        for i in layers
                    ),
                    dim=0,
                )
                tmp_gpu_buffer[1] = torch.stack(
                    tuple(
                        self.kvcaches[i][1]
                        .view(page_buffer_size, hd_shape)
                        .index_select(0, slot_mapping[start:end])
                        for i in layers
                    ),
                    dim=0,
                )
                memory_obj.tensor.copy_(tmp_gpu_buffer, non_blocking=True)

        if self.use_mla:
            memory_obj.metadata.fmt = MemoryFormat.KV_MLA_FMT

    # TODO(Jiayi): need to optimize to enable real batching
    def batched_to_gpu(self, memory_objs, starts, ends, **kwargs):
        for memory_obj, start, end in zip(memory_objs, starts, ends, strict=False):
            self.to_gpu(memory_obj, start, end, **kwargs)

    # TODO(Jiayi): need to optimize to enable real batching
    def batched_from_gpu(self, memory_objs, starts, ends, **kwargs):
        for memory_obj, start, end in zip(memory_objs, starts, ends, strict=False):
            self.from_gpu(memory_obj, start, end, **kwargs)

    def get_shape(self, num_tokens: int) -> torch.Size:
        kv_size = 1 if self.use_mla else 2
        return torch.Size([kv_size, self.num_layers, num_tokens, self.hidden_dim_size])
