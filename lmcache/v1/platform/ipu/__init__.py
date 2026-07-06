# SPDX-License-Identifier: Apache-2.0
"""IPU-specific platform primitives (RDMA-backed KV cache transfer).

:class:`~lmcache.v1.platform.ipu.rdma_wrapper.IPURdmaWrapper` carries a
``device_type`` ClassVar and a ``wrap`` factory classmethod, which
:func:`~lmcache.v1.platform._registry._discover_wrappers_once` picks
up at run-time -- no static ``register_kv_wrapper`` needed.
"""
