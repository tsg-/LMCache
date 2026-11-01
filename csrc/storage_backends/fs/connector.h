// SPDX-License-Identifier: Apache-2.0
#pragma once

#include "../connector_base.h"
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <unistd.h>
#include <atomic>
#include <chrono>
#include <cstring>
#include <deque>
#include <filesystem>
#include <memory>
#include <mutex>
#include <string>
#include <utility>
#include <vector>

namespace lmcache {
namespace connector {

// Key encoding constants — must match fs_l2_adapter.py
static constexpr char KEY_SEP = '@';
static constexpr const char* PATH_SLASH_REPLACEMENT = "-SEP-";
static constexpr const char* FILE_EXT = ".data";
static constexpr const char* TMP_EXT = ".tmp";

// Per-worker connection state for the FS connector.
// Each worker maintains its own I/O buffer for O_DIRECT.
struct WorkerFSConn {
  std::filesystem::path base_path;
  std::filesystem::path tmp_dir;  // empty if not configured
  bool use_odirect = false;
  size_t disk_block_size = 0;
  // If > 0, trigger filesystem readahead by issuing a small
  // initial read of this many bytes before reading the rest.
  size_t read_ahead_size = 0;
  // 0 keeps every object directly under base_path.
  size_t num_shard_dirs = 0;
};

class FSConnector : public ConnectorBase<WorkerFSConn> {
 public:
  // num_shard_dirs > 0 places each object in one of that many hashed
  // subdirectories of base_path. Every store takes the directory inode lock
  // for a lookup, a create and a rename, so in one flat directory concurrent
  // stores, and loads opening files, wait on each other. A reader configured
  // with a different value cannot find existing objects.
  //
  // recycle_max_files > 0 renames deleted objects into a per-shard ".pool"
  // directory instead of unlinking them, and later stores of the same size
  // overwrite a pooled file in place. The SSD-visible footprint then stops
  // growing without discards. A pooled file is reused only after a 1 s
  // quarantine, so a load that opened it before the delete can finish.
  //
  // store_workers / delete_workers > 0 give those operations dedicated
  // worker lanes so their blocking metadata work does not queue ahead of
  // loads in the shared num_workers pool.
  FSConnector(std::string base_path, int num_workers,
              std::string relative_tmp_dir = "", bool use_odirect = false,
              size_t read_ahead_size = 0, size_t num_shard_dirs = 0,
              size_t recycle_max_files = 0, int store_workers = 0,
              int delete_workers = 0);
  ~FSConnector() override;

 protected:
  WorkerFSConn create_connection() override;
  void do_single_get(WorkerFSConn& conn, const std::string& key, void* buf,
                     size_t len, size_t chunk_size) override;
  void do_single_set(WorkerFSConn& conn, const std::string& key,
                     const void* buf, size_t len, size_t chunk_size) override;
  bool do_single_exists(WorkerFSConn& conn, const std::string& key) override;
  bool do_single_delete(WorkerFSConn& conn, const std::string& key) override;

 private:
  // Build the filesystem-safe filename from a serialized key string.
  //
  // Input key (from NativeConnectorL2Adapter._object_key_to_string):
  //   Unsalted: "{model}@{kv_rank:08x}@{hash.hex()}"
  //   Salted  : "{model}@{kv_rank:08x}@{hash.hex()}@{cache_salt}"
  //
  // Output filename (matching fs_l2_adapter.py._object_key_to_filename):
  //   Unsalted: "{safe_model}@{kv_rank:#010x}@{hash.hex()}.data"
  //   Salted  : "{safe_model}@{kv_rank:#010x}@{hash.hex()}@{cache_salt}.data"
  //
  // Differences from the input: '/' in model becomes '-SEP-', kv_rank
  // gains a '0x' prefix, and '.data' is appended. Both model_name and
  // cache_salt are forbidden from containing '@' (enforced on the
  // Python side), so the parse is unambiguous.
  static std::string key_to_filename(const std::string& key);

  static std::string replace_all(const std::string& str,
                                 const std::string& from,
                                 const std::string& to);

  // Shard subdirectory name for a filename; stable across processes and
  // builds (FNV-1a, not std::hash).
  static std::string shard_dir_name(const std::string& filename,
                                    size_t num_shard_dirs);

  static std::filesystem::path object_path(const WorkerFSConn& conn,
                                           const std::string& filename);

  struct RecyclePool {
    std::mutex mu;
    std::deque<std::pair<std::string, std::chrono::steady_clock::time_point>>
        files;
  };

  size_t pool_index(const std::string& filename) const;
  std::filesystem::path pool_dir(size_t index) const;
  // Moves an object into its pool. Returns false when the pool is full or the
  // rename fails; the caller then unlinks normally.
  bool recycle_into_pool(const std::filesystem::path& path, size_t index);
  // Overwrites a quarantined pooled file and renames it to file_path.
  // Returns false, with no visible change, when no pooled file is eligible.
  bool store_into_recycled(WorkerFSConn& conn,
                           const std::filesystem::path& file_path, size_t index,
                           const void* buf, size_t len);

  std::string base_path_;
  std::string relative_tmp_dir_;
  bool use_odirect_;
  size_t disk_block_size_;
  size_t read_ahead_size_;
  size_t num_shard_dirs_;
  size_t recycle_max_files_;
  std::vector<std::unique_ptr<RecyclePool>> pools_;
  std::atomic<size_t> pooled_{0};
  std::atomic<uint64_t> pool_seq_{0};
  std::string pool_prefix_;
};

}  // namespace connector
}  // namespace lmcache
