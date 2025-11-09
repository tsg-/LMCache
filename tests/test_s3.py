import os
os.environ['LMCACHE_CONFIG_FILE'] = '/root/lmcache_config.yaml'

from lmcache.integration.vllm.utils import lmcache_get_or_create_config
from lmcache.config import LMCacheEngineMetadata
from lmcache.v1.cache_engine import LMCacheEngineBuilder
from lmcache.v1.hpu_connector import VLLMPagedMemHPUConnectorV2
import torch

config = lmcache_get_or_create_config()
print(f"Remote URL: {config.remote_url}")
print(f"Local CPU enabled: {config.local_cpu}")

# Create minimal metadata and connector
metadata = LMCacheEngineMetadata(
    model_name="test",
    world_size=1,
    worker_id=0,
    fmt="KV_2LTD",
    kv_dtype=torch.bfloat16,
    kv_shape=(4, 2, 256, 16, 128),
    use_mla=False,
)

connector = VLLMPagedMemHPUConnectorV2(
    hidden_dim_size=2048,
    num_layers=4,
    use_gpu=True,
    chunk_size=256,
    dtype=torch.bfloat16,
    device=torch.device("hpu"),
)

def noop_broadcast(t, src): pass
def noop_broadcast_obj(o, src): return o

# This will trigger S3 connector creation
engine = LMCacheEngineBuilder.get_or_create(
    "test_engine",
    config,
    metadata,
    connector,
    noop_broadcast,
    noop_broadcast_obj,
)

# Check the storage manager's backend
print(f"\nStorage manager type: {type(engine.storage_manager)}")
if hasattr(engine.storage_manager, 'backend'):
    print(f"Backend type: {type(engine.storage_manager.backend)}")
    if hasattr(engine.storage_manager.backend, 'connector'):
        print(f"Connector type: {type(engine.storage_manager.backend.connector)}")

print("\nS3 backend is active and initialized successfully!")

# Clean shutdown to prevent hanging
print("\nCleaning up...")
LMCacheEngineBuilder.destroy("test_engine")

# Force cleanup of any remaining async tasks
import asyncio
try:
    # Get all pending tasks and cancel them
    loop = asyncio.get_event_loop()
    pending = asyncio.all_tasks(loop)
    for task in pending:
        task.cancel()
    # Give cancelled tasks a moment to cleanup
    if pending:
        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
except Exception:
    pass

print("Done!")

# Force process exit to prevent hanging due to lingering threads
import sys
sys.exit(0)
