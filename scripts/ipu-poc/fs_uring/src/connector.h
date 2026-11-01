// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <liburing.h>

#include <atomic>
#include <chrono>
#include <deque>
#include <filesystem>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "connector_base.h"

namespace lmcache::connector {

struct UringWorker {
  explicit UringWorker(unsigned depth);
  ~UringWorker();
  UringWorker(const UringWorker&) = delete;
  UringWorker& operator=(const UringWorker&) = delete;
  io_uring ring{};
  bool poisoned = false;
};

struct IOStats {
  uint64_t submitted_reads;
  uint64_t completed_reads;
  uint64_t pending_reads;
  uint64_t max_worker_pending;
  uint64_t max_total_pending;
};

// Buffers belong to the caller until completion or close() returns.
// Each worker owns one ring and handles a whole LOAD batch at a time.
class FSUringConnector : public ConnectorBase<UringWorker*> {
 public:
  FSUringConnector(std::string base_path, int num_workers,
                   std::string relative_tmp_dir = "", bool use_odirect = true,
                   unsigned io_depth = 32, size_t num_shard_dirs = 0,
                   size_t recycle_max_files = 0, int store_workers = 0,
                   int delete_workers = 0);
  ~FSUringConnector() override;

  IOStats io_stats() const;
  int event_fd() const override;
  std::vector<Completion> drain_completions() override;
  uint64_t submit_batch_get(const std::vector<std::string>& keys,
                            const std::vector<void*>& bufs,
                            const std::vector<size_t>& lens,
                            size_t chunk_size) override;
  uint64_t submit_batch_set(const std::vector<std::string>& keys,
                            const std::vector<void*>& bufs,
                            const std::vector<size_t>& lens,
                            size_t chunk_size) override;
  uint64_t submit_batch_exists(const std::vector<std::string>& keys) override;
  uint64_t submit_batch_delete(const std::vector<std::string>& keys) override;
  void close() override;

 protected:
  UringWorker* create_connection() override;
  size_t choose_num_tiles(Op op, size_t num_items) const override;
  void do_batch_get(UringWorker*& worker, const Request& req) override;
  void do_single_get(UringWorker*& worker, const std::string& key, void* buf,
                     size_t len, size_t chunk_size) override;
  void do_single_set(UringWorker*& worker, const std::string& key,
                     const void* buf, size_t len, size_t chunk_size) override;
  bool do_single_exists(UringWorker*& worker, const std::string& key) override;
  bool do_single_delete(UringWorker*& worker, const std::string& key) override;
  void on_workers_stopped() override;

 private:
  std::string filename_for_key(const std::string& key) const;
  size_t shard_of(const std::string& filename) const;
  std::filesystem::path shard_dir(size_t shard) const;
  std::filesystem::path path_for_key(const std::string& key) const;
  // Recycling: a deleted object file is renamed into its shard pool and later
  // overwritten in place by a store of the same size, so the SSD-visible
  // footprint does not grow and freed blocks never need a discard.
  struct RecyclePool {
    std::mutex mu;
    std::deque<std::pair<std::string, std::chrono::steady_clock::time_point>>
        files;
  };
  bool recycle_into_pool(const std::filesystem::path& path, size_t shard);
  bool store_into_recycled(const std::filesystem::path& path, size_t shard,
                           const void* buf, size_t len);
  int flags_for_buffer(int flags, const void* buf, size_t len) const;
  void ensure_open() const;
  void read_batch(UringWorker& worker, const Request& req);

  std::filesystem::path base_path_;
  std::filesystem::path tmp_path_;
  bool use_odirect_;
  size_t block_size_;
  unsigned io_depth_;
  // 0 keeps every object directly under base_path_; otherwise objects go in
  // one of this many FNV-1a hashed subdirectories (same layout as fs_native).
  size_t num_shard_dirs_;
  // 0 disables recycling; otherwise the pool holds at most this many files.
  size_t recycle_max_files_;
  std::vector<std::unique_ptr<RecyclePool>> pools_;
  std::atomic<size_t> pooled_{0};
  std::atomic<uint64_t> pool_seq_{0};
  std::string pool_prefix_;
  std::vector<std::unique_ptr<UringWorker>> rings_;
  std::atomic<size_t> next_worker_{0};
  std::atomic<uint64_t> submitted_{0};
  std::atomic<uint64_t> completed_{0};
  std::atomic<uint64_t> pending_{0};
  std::atomic<uint64_t> max_worker_{0};
  std::atomic<uint64_t> max_total_{0};
  mutable std::mutex lifecycle_mu_;
};

}  // namespace lmcache::connector
