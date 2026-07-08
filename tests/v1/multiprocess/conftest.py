# SPDX-License-Identifier: Apache-2.0
"""pytest configuration for v1/multiprocess tests.

Set UCX_TLS before any test module is imported so that when nixl_cu12._api
is first imported (at collection time), UCX initializes with TCP transport
instead of auto-selecting shared-memory. UCX shmem transport causes the
second cross-process NIXL transfer to stall when the first transfer's MR
has been deregistered between requests (Python GC finalizer on tensor
collection). Must be set before the first import of any nixl binding.
"""

import os

# Set UCX_TLS before any test module import so UCX initializes with this
# transport when nixl_cu12._api is first imported at collection time.
# The "cma" (Cross-Memory Attach) transport is reliable for inter-process
# transfers on Linux — it uses process_vm_readv/writev which don't require
# additional memory registration beyond what NIXL already does.
# Avoid "posix" (shmem segment) which requires shared mapping state that
# can become inconsistent after MR deregister/re-register across requests.
os.environ.setdefault("UCX_TLS", "cma,self")
