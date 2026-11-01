// SPDX-License-Identifier: Apache-2.0
#pragma once

#include "../connector_base.h"
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <unistd.h>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <cstring>
#include <deque>
#include <filesystem>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace lmcache {
namespace connector {

// Key encoding constants — must match fs_l2_adapter.py
static constexpr const char* TMP_EXT = ".tmp";

// Bytes of reads kept outstanding against the device when num_read_workers is
// on and no explicit figure is configured.  Chosen as the value whose worst
// case across a local array, a single NVMe and 8 / 32 ms per-read latency
// is best; the sweep is in
// docs/design/v1/distributed/l2_adapters/fs_native_read_depth.md.
static constexpr size_t kDefaultReadMaxBytesInFlight = size_t{1536} << 20;
// A worker blocked on a full budget is woken once 1/kReadBudgetWakeSlices of
// it is free.
static constexpr size_t kReadBudgetWakeSlices = 8;
// Reads always admitted whatever the budget, so an object larger than half
// of it still overlaps with the next one instead of draining the device
// between them: one 1 GiB object alone reads ~40 GB/s on an array that
// two in flight bring to ~49.
static constexpr size_t kMinReadsInFlight = 2;

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
  // num_read_workers: reader threads, and so the maximum reads in flight.
  //   Zero keeps the legacy path, where reads run on the worker threads
  //   and the depth against the device equals num_workers.
  // read_max_bytes_in_flight: bytes kept outstanding, connector-wide.
  //   Zero selects kDefaultReadMaxBytesInFlight when num_read_workers is
  //   positive.
  // num_shard_dirs > 0 places each object in one of that many hashed
  //   subdirectories of base_path. Every store takes the directory inode
  //   lock for a lookup, a create and a rename, so in one flat directory
  //   concurrent stores, and loads opening files, wait on each other. A
  //   reader configured with a different value cannot find existing objects.
  // recycle_max_files > 0 renames deleted objects into a per-shard ".pool"
  //   directory instead of unlinking them, and later stores of the same size
  //   overwrite a pooled file in place, so the SSD-visible footprint stops
  //   growing without discards. A pooled file is reused only after a 1 s
  //   quarantine, so a load that opened it before the delete can finish.
  // store_workers / delete_workers > 0 give those operations dedicated
  //   worker lanes, so their blocking metadata work does not queue ahead of
  //   loads in the shared num_workers pool.
  FSConnector(std::string base_path, int num_workers,
              std::string relative_tmp_dir = "", bool use_odirect = false,
              size_t read_ahead_size = 0, int num_read_workers = 0,
              size_t read_max_bytes_in_flight = 0, size_t num_shard_dirs = 0,
              size_t recycle_max_files = 0, int store_workers = 0,
              int delete_workers = 0);
  ~FSConnector() override;

  // The byte budget in force, or 0 when there is no read pool.
  size_t read_budget_bytes() const;

 protected:
  WorkerFSConn create_connection() override;
  void do_single_get(WorkerFSConn& conn, const std::string& key, void* buf,
                     size_t len, size_t chunk_size) override;
  void do_single_set(WorkerFSConn& conn, const std::string& key,
                     const void* buf, size_t len, size_t chunk_size) override;
  bool do_single_exists(WorkerFSConn& conn, const std::string& key) override;
  bool do_single_delete(WorkerFSConn& conn, const std::string& key) override;

  // With a read pool a GET batch stays one tile, as MooncakeConnector
  // does: the pool parallelises it, and splitting it across workers first
  // would cap reads in flight at num_workers.
  size_t choose_num_tiles(Op op, size_t num_items) const override;

  // Through the read pool when one is configured, otherwise the base
  // class's legacy path.  Per-key failures are reported the same way.
  void do_batch_get(WorkerFSConn& conn, const Request& req) override;

  // Stops the pool.  Only safe after the workers are joined: a worker may
  // be waiting on a tile only the readers can finish.
  void on_workers_stopped() override;

 private:
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

  // Completion state of one tile, on the submitting worker's stack.
  // Guarded by read_mu_, which also keeps it alive: the worker destroys it
  // only after taking that lock and seeing remaining == 0.
  struct ReadTile {
    std::vector<uint8_t> ok;  // one flag per object
    size_t remaining = 0;
    // Signalled once, by the reader that finishes the last object, so a
    // completion does not wake every worker waiting on some other tile.
    std::condition_variable done_cv;
  };

  // One object to read, owned by the submitting worker's stack and linked
  // into the read queue without allocating.
  struct ReadTask {
    const Request* req = nullptr;
    size_t index = 0;  // into req->keys and tile->ok
    ReadTile* tile = nullptr;
    ReadTask* next = nullptr;
  };

  // Hand a tile to the readers one object at a time, each dispatched as
  // soon as the byte budget has room, and wait for all of them.
  void do_batch_get_pooled(const Request& req);

  // Whether an object of `len` bytes may be dispatched now.  Requires
  // read_mu_.
  bool read_budget_admits(size_t len) const;

  void start_read_pool(int num_read_workers);
  void stop_read_pool();
  void read_thread_main(WorkerFSConn& conn);

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

  // Read pool.  read_conns_ is sized once and never resized, so reader
  // references stay valid; read_max_bytes_in_flight_ is 0 without a pool
  // and never 0 with one.  Everything after read_mu_ is guarded by it.
  size_t read_max_bytes_in_flight_;
  std::vector<WorkerFSConn> read_conns_;
  std::vector<std::thread> read_threads_;
  std::mutex read_mu_;
  std::condition_variable read_cv_;  // a task was queued, or stop
  // Budget room opened.  Signalled only once a slice of the budget is
  // free, so a worker blocked on a full budget wakes once per slice and
  // dispatches a run of objects, not once per finished read.
  std::condition_variable read_budget_cv_;
  size_t read_budget_waiters_ = 0;
  ReadTask* read_queue_head_ = nullptr;
  ReadTask* read_queue_tail_ = nullptr;
  size_t read_bytes_in_flight_ = 0;  // dispatched and not yet finished
  size_t reads_in_flight_ = 0;       // objects behind read_bytes_in_flight_
  bool read_stop_ = false;
};

}  // namespace connector
}  // namespace lmcache
