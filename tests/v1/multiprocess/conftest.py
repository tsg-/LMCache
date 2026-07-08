# SPDX-License-Identifier: Apache-2.0
"""pytest configuration for v1/multiprocess tests.

Set UCX_TLS before any test module is imported so that when nixl_cu12._api
is first imported (at collection time), UCX initializes with TCP transport.
Shmem transport can have segment-state issues across sequential requests.
Must be set before the first import of any nixl binding.
"""

import os

# Force UCX TCP loopback on both server and worker sides.
os.environ.setdefault("UCX_TLS", "tcp,self")
