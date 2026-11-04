// SPDX-License-Identifier: Apache-2.0

#include "connector.h"
#include <sys/file.h>
#include <sys/resource.h>
#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <exception>
#include <stdexcept>
#include <string>
#include <utility>
#include "../keys.h"
#include "fault_injection.h"
#include "uring_reader.h"

namespace lmcache {
namespace connector {

// ---------------------------------------------------------------
// read/write helpers
// ---------------------------------------------------------------

static void write_all(int fd, const void* data, size_t len) {
  size_t written = 0;
  const char* ptr = static_cast<const char*>(data);
  while (written < len) {
    ssize_t n = ::write(fd, ptr + written, len - written);
    if (n < 0) {
      if (errno == EINTR) continue;
      throw std::runtime_error("write failed: " + std::string(strerror(errno)));
    }
    if (n == 0) {
      throw std::runtime_error("write returned 0");
    }
    written += static_cast<size_t>(n);
  }
}

static size_t read_all(int fd, void* buf, size_t len) {
  size_t total = 0;
  char* ptr = static_cast<char*>(buf);
  while (total < len) {
    ssize_t n = ::read(fd, ptr + total, len - total);
    if (n < 0) {
      if (errno == EINTR) continue;
      throw std::runtime_error("read failed: " + std::string(strerror(errno)));
    }
    if (n == 0) break;  // EOF
    total += static_cast<size_t>(n);
  }
  return total;
}

static bool try_enable_odirect(int& flags, const void* buf, size_t len,
                               size_t disk_block_size) {
#ifdef O_DIRECT
  if (disk_block_size == 0 || len % disk_block_size != 0) {
    return false;
  }
  auto addr = reinterpret_cast<std::uintptr_t>(buf);
  if (addr % disk_block_size != 0) {
    throw std::runtime_error(
        "O_DIRECT buffer address is not aligned to filesystem block size");
  }
  flags |= O_DIRECT;
  return true;
#else
  (void)flags;
  (void)buf;
  (void)len;
  (void)disk_block_size;
  return false;
#endif
}

// ---------------------------------------------------------------
// FSConnector
// ---------------------------------------------------------------

// Fixed-width lowercase hex, so every shard name has the same length.
static std::string format_shard(size_t index, size_t num_shard_dirs) {
  size_t width = 1;
  for (size_t n = num_shard_dirs - 1; n >= 16; n /= 16) ++width;
  std::string name;
  do {
    name.insert(name.begin(), "0123456789abcdef"[index % 16]);
    index /= 16;
  } while (index != 0);
  if (name.size() < width) name.insert(0, width - name.size(), '0');
  return name;
}

static size_t shard_index(const std::string& filename, size_t num_shard_dirs) {
  uint64_t h = 14695981039346656037ULL;
  for (unsigned char c : filename) {
    h ^= c;
    h *= 1099511628211ULL;
  }
  return static_cast<size_t>(h % num_shard_dirs);
}

std::string FSConnector::shard_dir_name(const std::string& filename,
                                        size_t num_shard_dirs) {
  return format_shard(shard_index(filename, num_shard_dirs), num_shard_dirs);
}

// Pooled files stay in their shard under this prefix: a rename across
// directories takes the filesystem-wide rename lock, which serialised every
// delete and reuse on a host. Object filenames end in ".data", so the prefix
// cannot collide with a stored object.
static constexpr const char* POOL_PREFIX = ".pool-";
// Far above observed load latency; see the constructor comment in connector.h.
static constexpr uint64_t RECYCLE_QUARANTINE_NS = 1000000000ULL;
// Held by the one connector allowed to recycle in a base_path.
static constexpr const char* RECYCLE_LOCK_NAME = ".lmcache-recycle.lock";

// Rings go only to workers that run gets (the shared pool), claimed at the
// first get; adding a "retrieve" lane here needs a change to that hand-out.
static WorkerPoolConfig lane_config(int store_workers, int delete_workers) {
  if (store_workers < 0 || delete_workers < 0) {
    throw std::invalid_argument(
        "store_workers and delete_workers must be >= 0");
  }
  WorkerPoolConfig config;
  if (store_workers > 0) config.per_op_workers["store"] = store_workers;
  if (delete_workers > 0) config.per_op_workers["delete"] = delete_workers;
  return config;
}

size_t FSConnector::pool_index(const std::string& filename) const {
  return num_shard_dirs_ == 0 ? 0 : shard_index(filename, num_shard_dirs_);
}

std::filesystem::path FSConnector::pool_dir(size_t index) const {
  std::filesystem::path dir(base_path_);
  if (num_shard_dirs_ > 0) dir /= format_shard(index, num_shard_dirs_);
  return dir;
}

bool FSConnector::recycle_into_pool(const std::filesystem::path& path,
                                    size_t index) {
  if (pooled_.fetch_add(1) >= recycle_max_files_) {
    pooled_.fetch_sub(1);
    return false;
  }
  std::string name = pool_prefix_ + std::to_string(pool_seq_.fetch_add(1));
  std::error_code ec;
  std::filesystem::rename(path, pool_dir(index) / name, ec);
  if (ec) {
    pooled_.fetch_sub(1);
    return false;
  }
  // Stamped after the rename, under the pool lock so the deque stays in
  // stamp order: a read that opened the old inode registered before its
  // open and so before this stamp.
  auto& pool = *pools_[index];
  std::lock_guard<std::mutex> lock(pool.mu);
  pool.files.push_back(ParkedFile{std::move(name), monotonic_ns()});
  return true;
}

uint64_t FSConnector::oldest_read_start_ns() const {
  // Each budget takes its own lock; the store path holds none of them.
  return std::min({read_budget_.oldest_start_ns(),
                   uring_budget_.oldest_start_ns(),
                   legacy_reads_.oldest_start_ns()});
}

bool FSConnector::store_into_recycled(WorkerFSConn& conn,
                                      const std::filesystem::path& file_path,
                                      size_t index, const void* buf,
                                      size_t len) {
  // Before taking a pool entry: a misaligned buffer throws here, and a throw
  // after the pop would strand the file outside the pool and the count.
  int flags = O_WRONLY;
  if (conn.use_odirect) {
    try_enable_odirect(flags, buf, len, conn.disk_block_size);
  }
  std::string name;
  {
    auto& pool = *pools_[index];
    std::lock_guard<std::mutex> lock(pool.mu);
    if (pool.files.empty() ||
        monotonic_ns() - pool.files.front().parked_ns < RECYCLE_QUARANTINE_NS) {
      return false;
    }
    // Read after the file was stamped, so a read missing from this figure
    // registered after the park and could not have opened the old inode.
    // A read in flight that started before the park may hold it open.
    if (pool.files.front().parked_ns >= oldest_read_start_ns()) {
      return false;
    }
    name = std::move(pool.files.front().name);
    pool.files.pop_front();
  }
  pooled_.fetch_sub(1);
  auto pooled = pool_dir(index) / name;
  int fd = ::open(pooled.c_str(), flags);
  struct stat st;
  if (fd < 0 || ::fstat(fd, &st) != 0 ||
      static_cast<size_t>(st.st_size) != len) {
    // Different object size or a vanished file: drop it, store fresh instead.
    if (fd >= 0) ::close(fd);
    std::error_code ignored;
    std::filesystem::remove(pooled, ignored);
    return false;
  }
  try {
    write_all(fd, buf, len);
  } catch (...) {
    ::close(fd);
    std::error_code ignored;
    std::filesystem::remove(pooled, ignored);
    throw;
  }
  ::close(fd);
  std::error_code ec;
  std::filesystem::rename(pooled, file_path, ec);
  if (ec) {
    std::error_code ignored;
    std::filesystem::remove(pooled, ignored);
    throw std::runtime_error("rename failed: " + pooled.string() + " -> " +
                             file_path.string() + ": " + ec.message());
  }
  return true;
}

std::filesystem::path FSConnector::object_path(const WorkerFSConn& conn,
                                               const std::string& filename) {
  if (conn.num_shard_dirs == 0) {
    return conn.base_path / filename;
  }
  return conn.base_path / shard_dir_name(filename, conn.num_shard_dirs) /
         filename;
}

// No pool, no budget.  A pool with no figure gets the default.
static size_t effective_read_budget(int num_read_workers, size_t budget) {
  if (num_read_workers <= 0) {
    return 0;
  }
  return budget == 0 ? kDefaultReadMaxBytesInFlight : budget;
}

FSConnector::FSConnector(std::string base_path, int num_workers,
                         std::string relative_tmp_dir, bool use_odirect,
                         size_t read_ahead_size, int num_read_workers,
                         size_t read_max_bytes_in_flight, size_t num_shard_dirs,
                         size_t recycle_max_files, int store_workers,
                         int delete_workers, std::string read_engine,
                         unsigned io_depth)
    : ConnectorBase(num_workers, lane_config(store_workers, delete_workers)),
      base_path_(std::move(base_path)),
      relative_tmp_dir_(std::move(relative_tmp_dir)),
      use_odirect_(use_odirect),
      disk_block_size_(0),
      read_ahead_size_(read_ahead_size),
      num_shard_dirs_(num_shard_dirs),
      recycle_max_files_(recycle_max_files),
      read_budget_(
          effective_read_budget(num_read_workers, read_max_bytes_in_flight),
          read_mu_),
      uring_budget_(read_max_bytes_in_flight == 0 ? kDefaultReadMaxBytesInFlight
                                                  : read_max_bytes_in_flight) {
  if (num_read_workers < 0) {
    throw std::runtime_error("num_read_workers must be >= 0");
  }
  if (read_engine != "auto" && read_engine != "io_uring" &&
      read_engine != "pool") {
    throw std::invalid_argument(
        "read_engine must be one of auto, io_uring, pool; got '" + read_engine +
        "'");
  }
  if (io_depth < 1 || io_depth > 4096) {
    throw std::invalid_argument("io_depth must be in [1, 4096], got " +
                                std::to_string(io_depth));
  }
#ifdef LMCACHE_FS_FAULT_INJECTION
  fault_delay_ms_ = fs_fault_delay_ms_from_env();
#endif

  // Create base directory
  std::filesystem::create_directories(base_path_);

  for (size_t i = 0; i < num_shard_dirs_; ++i) {
    std::filesystem::create_directories(std::filesystem::path(base_path_) /
                                        format_shard(i, num_shard_dirs_));
  }

  // Create tmp directory if configured
  if (!relative_tmp_dir_.empty()) {
    auto tmp_path = std::filesystem::path(base_path_) / relative_tmp_dir_;
    std::filesystem::create_directories(tmp_path);
  }

  // Query disk block size for O_DIRECT
  if (use_odirect_) {
    struct statvfs st;
    if (statvfs(base_path_.c_str(), &st) == 0) {
      disk_block_size_ = st.f_bsize;
    }
  }

  // The recycle lock, then rings or readers, before any worker can take a
  // request.  On failure release and join whatever started: the destructor
  // does not run for a half-built object.
  try {
    if (recycle_max_files_ > 0) {
      init_recycling();
    }
    select_read_engine(read_engine, io_depth);
    if (use_uring_) {
      read_engine_ = "io_uring";
    } else {
      start_read_pool(num_read_workers);
      read_engine_ = num_read_workers > 0 ? "pool" : "legacy";
    }
    start_workers();  // IMPORTANT: call at END of constructor
  } catch (...) {
    close();
    throw;
  }
}

void FSConnector::init_recycling() {
  const auto lock_path = std::filesystem::path(base_path_) / RECYCLE_LOCK_NAME;
  const int fd = ::open(lock_path.c_str(), O_RDWR | O_CREAT | O_CLOEXEC, 0644);
  if (fd < 0 || ::flock(fd, LOCK_EX | LOCK_NB) != 0) {
    const int err = errno;
    if (fd >= 0) ::close(fd);
    fprintf(stderr,
            "[LMCache FS] recycling disabled for %s: %s %s (%s); deletes "
            "unlink and stores create new files\n",
            base_path_.c_str(),
            err == EWOULDBLOCK ? "another connector holds" : "could not lock",
            lock_path.c_str(), strerror(err));
    recycle_max_files_ = 0;
    return;
  }
  recycle_lock_fd_ = fd;
  // Unique across processes, because an earlier process may have left
  // pool files behind; those are adopted and immediately reusable.
  pool_prefix_ =
      std::string(POOL_PREFIX) + std::to_string(::getpid()) + "-" +
      std::to_string(
          std::chrono::steady_clock::now().time_since_epoch().count()) +
      "-";
  size_t pools = num_shard_dirs_ == 0 ? 1 : num_shard_dirs_;
  for (size_t i = 0; i < pools; ++i) {
    pools_.push_back(std::make_unique<RecyclePool>());
    for (const auto& entry : std::filesystem::directory_iterator(pool_dir(i))) {
      std::string name = entry.path().filename().string();
      if (name.rfind(POOL_PREFIX, 0) != 0) continue;
      pools_[i]->files.push_back(ParkedFile{std::move(name), 0});
      pooled_.fetch_add(1);
    }
  }
}

void FSConnector::release_recycle_lock() {
  if (recycle_lock_fd_ >= 0) {
    ::close(recycle_lock_fd_);
    recycle_lock_fd_ = -1;
  }
}

void FSConnector::select_read_engine(const std::string& read_engine,
                                     unsigned io_depth) {
  if (read_engine == "pool") {
    return;
  }
  const bool forced = read_engine == "io_uring";
#ifndef LMCACHE_HAVE_LIBURING
  (void)io_depth;
  if (forced) {
    throw std::runtime_error(
        "read_engine io_uring requested, but lmcache_fs was built without "
        "liburing, so the io_uring engine is compiled out");
  }
#else
  const uint64_t workers = static_cast<uint64_t>(num_workers_);
  struct rlimit nofile;
  if (::getrlimit(RLIMIT_NOFILE, &nofile) == 0 &&
      nofile.rlim_cur != RLIM_INFINITY) {
    uint64_t soft = static_cast<uint64_t>(nofile.rlim_cur);
    const uint64_t need = workers * io_depth + kReadFdMargin;
    // Raise the soft limit toward what the rings need, never past the hard
    // limit and never down.
    if (need > soft) {
      const uint64_t hard = nofile.rlim_max == RLIM_INFINITY
                                ? need
                                : static_cast<uint64_t>(nofile.rlim_max);
      const uint64_t target = std::min(hard, need);
      if (target > soft) {
        struct rlimit raised = nofile;
        raised.rlim_cur = static_cast<rlim_t>(target);
        if (::setrlimit(RLIMIT_NOFILE, &raised) == 0) {
          fprintf(stderr,
                  "[LMCache FS] read_engine %s: raised the RLIMIT_NOFILE "
                  "soft limit from %llu to %llu for %llu io_uring workers x "
                  "io_depth %u\n",
                  read_engine.c_str(), static_cast<unsigned long long>(soft),
                  static_cast<unsigned long long>(target),
                  static_cast<unsigned long long>(workers), io_depth);
          soft = target;
        } else {
          fprintf(stderr,
                  "[LMCache FS] read_engine %s: could not raise the "
                  "RLIMIT_NOFILE soft limit from %llu to %llu: %s\n",
                  read_engine.c_str(), static_cast<unsigned long long>(soft),
                  static_cast<unsigned long long>(target), strerror(errno));
        }
      }
    }
    if (need > soft) {
      if (forced) {
        throw std::runtime_error(
            "read_engine io_uring: num_workers x io_depth + " +
            std::to_string(kReadFdMargin) + " = " + std::to_string(workers) +
            " x " + std::to_string(io_depth) + " + " +
            std::to_string(kReadFdMargin) + " = " + std::to_string(need) +
            " open files exceeds the RLIMIT_NOFILE soft limit of " +
            std::to_string(soft) +
            "; raise the limit or lower io_depth or num_workers");
      }
      const uint64_t fit =
          soft > kReadFdMargin ? (soft - kReadFdMargin) / workers : 0;
      if (fit < kMinAutoIoDepth) {
        fprintf(stderr,
                "[LMCache FS] read_engine auto: RLIMIT_NOFILE soft limit %llu "
                "fits io_depth %llu for %llu io_uring workers, below the "
                "auto floor of %u; using the read pool\n",
                static_cast<unsigned long long>(soft),
                static_cast<unsigned long long>(fit),
                static_cast<unsigned long long>(workers), kMinAutoIoDepth);
        return;
      }
      fprintf(stderr,
              "[LMCache FS] read_engine auto: io_depth lowered from %u to "
              "%llu so %llu workers fit RLIMIT_NOFILE %llu\n",
              io_depth, static_cast<unsigned long long>(fit),
              static_cast<unsigned long long>(workers),
              static_cast<unsigned long long>(soft));
      io_depth = static_cast<unsigned>(fit);
    }
  }
  // All rings or none: a worker without one could not serve gets.
  try {
    readers_.reserve(workers);
    for (uint64_t i = 0; i < workers; ++i) {
      readers_.push_back(
          std::make_unique<UringReader>(io_depth, uring_budget_));
    }
  } catch (const std::exception& e) {
    readers_.clear();
    if (forced) {
      throw std::runtime_error(
          std::string("read_engine io_uring is unavailable: ") + e.what());
    }
    fprintf(stderr,
            "[LMCache FS] read_engine auto: io_uring is unavailable (%s); "
            "using the read pool\n",
            e.what());
    return;
  }
  use_uring_ = true;
  io_depth_ = io_depth;
#endif
}

FSConnector::~FSConnector() { close(); }

WorkerFSConn FSConnector::create_connection() {
  WorkerFSConn conn;
  conn.base_path = base_path_;
  if (!relative_tmp_dir_.empty()) {
    conn.tmp_dir = std::filesystem::path(base_path_) / relative_tmp_dir_;
  }
  conn.use_odirect = use_odirect_;
  conn.disk_block_size = disk_block_size_;
  conn.read_ahead_size = read_ahead_size_;
  conn.num_shard_dirs = num_shard_dirs_;
  return conn;
}

void FSConnector::do_single_get(WorkerFSConn& conn, const std::string& key,
                                void* buf, size_t len, size_t chunk_size) {
  std::string filename = key_to_filename(key);
  auto file_path = object_path(conn, filename);

  int flags = O_RDONLY;
  bool do_odirect = conn.use_odirect &&
                    try_enable_odirect(flags, buf, len, conn.disk_block_size);

  int fd = ::open(file_path.c_str(), flags);
  if (fd < 0) {
    throw std::runtime_error("open for read failed: " + file_path.string() +
                             ": " + strerror(errno));
  }
#ifdef LMCACHE_FS_FAULT_INJECTION
  if (fault_delay_ms_ > 0) {
    std::this_thread::sleep_for(std::chrono::milliseconds(fault_delay_ms_));
  }
#endif

  try {
    size_t n;
    bool use_read_ahead =
        !do_odirect && conn.read_ahead_size > 0 && len > conn.read_ahead_size;
    if (use_read_ahead) {
      // Trigger filesystem readahead with a small initial
      // read, then read the remainder.
      size_t ra = conn.read_ahead_size;
      size_t n_head = read_all(fd, buf, ra);
      if (n_head < ra) {
        // Short read on the head portion — treat as
        // incomplete
        n = n_head;
      } else {
        size_t n_tail = read_all(fd, static_cast<char*>(buf) + ra, len - ra);
        n = n_head + n_tail;
      }
    } else {
      n = read_all(fd, buf, len);
    }
    if (n != len) {
      throw std::runtime_error("incomplete read for " + file_path.string() +
                               ": expected " + std::to_string(len) + ", got " +
                               std::to_string(n));
    }
  } catch (...) {
    ::close(fd);
    throw;
  }
  ::close(fd);
}

void FSConnector::do_single_set(WorkerFSConn& conn, const std::string& key,
                                const void* buf, size_t len,
                                size_t chunk_size) {
  std::string filename = key_to_filename(key);
  auto file_path = object_path(conn, filename);

  // Skip if already stored on disk
  if (std::filesystem::exists(file_path)) {
    return;
  }

  if (recycle_max_files_ > 0 &&
      store_into_recycled(conn, file_path, pool_index(filename), buf, len)) {
    return;
  }

  // Determine temp file path
  std::filesystem::path tmp_path;
  if (!conn.tmp_dir.empty()) {
    tmp_path = conn.tmp_dir / filename;
  } else {
    tmp_path = file_path;
    tmp_path.replace_extension(TMP_EXT);
  }

  int flags = O_CREAT | O_WRONLY | O_TRUNC;
  if (conn.use_odirect) {
    try_enable_odirect(flags, buf, len, conn.disk_block_size);
  }

  int fd = ::open(tmp_path.c_str(), flags, 0644);
  if (fd < 0) {
    throw std::runtime_error("open for write failed: " + tmp_path.string() +
                             ": " + strerror(errno));
  }

  try {
    write_all(fd, buf, len);
  } catch (...) {
    ::close(fd);
    // Clean up temp file on failure
    std::filesystem::remove(tmp_path);
    throw;
  }
  ::close(fd);

  // Atomic rename: tmp -> final
  std::error_code ec;
  std::filesystem::rename(tmp_path, file_path, ec);
  if (ec) {
    // Try to clean up, but prioritize reporting the original error.
    std::error_code remove_ec;
    std::filesystem::remove(tmp_path, remove_ec);
    throw std::runtime_error("rename failed: " + tmp_path.string() + " -> " +
                             file_path.string() + ": " + ec.message());
  }
}

bool FSConnector::do_single_exists(WorkerFSConn& conn, const std::string& key) {
  std::string filename = key_to_filename(key);
  auto file_path = object_path(conn, filename);
  return std::filesystem::exists(file_path);
}

bool FSConnector::do_single_delete(WorkerFSConn& conn, const std::string& key) {
  std::string filename = key_to_filename(key);
  auto file_path = object_path(conn, filename);
  if (recycle_max_files_ > 0 &&
      recycle_into_pool(file_path, pool_index(filename))) {
    return true;
  }
  std::error_code ec;
  return std::filesystem::remove(file_path, ec);
}

// ---------------------------------------------------------------
// Read pool
// ---------------------------------------------------------------

size_t FSConnector::read_budget_bytes() const {
  return use_uring_ ? uring_budget_.max_bytes() : read_budget_.max_bytes();
}

size_t FSConnector::read_peak_bytes() const {
  return use_uring_ ? uring_budget_.peak_bytes() : read_budget_.peak_bytes();
}

size_t FSConnector::read_bytes_in_flight() const {
  return use_uring_ ? uring_budget_.bytes_in_flight()
                    : read_budget_.bytes_in_flight();
}

std::string FSConnector::read_engine() const { return read_engine_; }

unsigned FSConnector::io_depth() const { return io_depth_; }

bool FSConnector::recycling_enabled() const { return recycle_max_files_ > 0; }

size_t FSConnector::choose_num_tiles(Op op, size_t num_items) const {
  if (op != Op::BATCH_TILE_GET || (read_threads_.empty() && !use_uring_)) {
    return ConnectorBase::choose_num_tiles(op, num_items);
  }
  return 1;
}

void FSConnector::do_batch_get(WorkerFSConn& conn, const Request& req) {
  if (use_uring_) {
    do_batch_get_uring(conn, req);
    return;
  }
  if (read_threads_.empty()) {
    if (recycle_max_files_ == 0) {
      ConnectorBase::do_batch_get(conn, req);
      return;
    }
    // One zero-byte registration for the whole tile, before any open: the
    // recycle guard needs only the earliest start time.
    const size_t no_bytes = 0;
    uint64_t start_ns = 0;
    legacy_reads_.reserve(&no_bytes, 1, true, &start_ns);
    try {
      ConnectorBase::do_batch_get(conn, req);
    } catch (...) {
      legacy_reads_.release(0, 1, &start_ns, 1);
      throw;
    }
    legacy_reads_.release(0, 1, &start_ns, 1);
    return;
  }
  do_batch_get_pooled(req);
}

// The ring takes every object except those the synchronous path must keep
// handling as today: longer than kMaxRingReadBytes, empty, or buffered
// with read_ahead_size in effect.  Those are read after the ring batch,
// one at a time, each reserved in uring_budget_ like a ring read.  A
// misaligned O_DIRECT buffer or a bad key fails that key alone.
void FSConnector::do_batch_get_uring(WorkerFSConn& conn, const Request& req) {
#ifdef LMCACHE_HAVE_LIBURING
  if (conn.reader == nullptr) {
    const size_t index = next_reader_.fetch_add(1);
    if (index >= readers_.size()) {
      throw std::logic_error("more get workers than io_uring rings");
    }
    conn.reader = readers_[index].get();
  }
  auto& results = req.batch->per_key_results;
  const size_t num_keys = req.keys.size();
  std::vector<ReadItem> items;
  std::vector<size_t> ring_keys;  // key index of items[j]
  std::vector<size_t> sync_keys;
  items.reserve(num_keys);
  ring_keys.reserve(num_keys);
  for (size_t i = 0; i < num_keys; ++i) {
    results[req.start_idx + i] = 0;
    const size_t len = req.buf_lens[i];
    try {
      int flags = O_RDONLY;
      const bool direct =
          conn.use_odirect &&
          try_enable_odirect(flags, req.buf_ptrs[i], len, conn.disk_block_size);
      const bool read_ahead =
          !direct && conn.read_ahead_size > 0 && len > conn.read_ahead_size;
      if (len == 0 || len > kMaxRingReadBytes || read_ahead) {
        sync_keys.push_back(i);
        continue;
      }
      items.push_back(ReadItem{object_path(conn, key_to_filename(req.keys[i])),
                               req.buf_ptrs[i], len, flags,
                               direct ? conn.disk_block_size : 0});
      ring_keys.push_back(i);
    } catch (const std::exception& e) {
      fprintf(stderr, "[LMCache GET] key %s failed: %s\n", req.keys[i].c_str(),
              e.what());
    }
  }

  std::vector<uint8_t> ok(items.size(), 0);
  std::exception_ptr engine_failure;
  try {
    conn.reader->read_batch(items, ok.data(), stop_);
  } catch (...) {
    engine_failure = std::current_exception();
  }
  for (size_t j = 0; j < items.size(); ++j) {
    results[req.start_idx + ring_keys[j]] = ok[j];
  }

  for (size_t i : sync_keys) {
    if (is_stopping()) {
      break;
    }
    const size_t len = req.buf_lens[i];
    uint64_t start_ns = 0;
    uring_budget_.reserve(&len, 1, true, &start_ns);
    uint8_t key_ok = 1;
    try {
      do_single_get(conn, req.keys[i], req.buf_ptrs[i], len,
                    req.batch_chunk_num_bytes);
    } catch (const std::exception& e) {
      key_ok = 0;
      fprintf(stderr, "[LMCache GET] key %s failed: %s\n", req.keys[i].c_str(),
              e.what());
    }
    uring_budget_.release(len, 1, &start_ns, 1);
    results[req.start_idx + i] = key_ok;
  }

  if (engine_failure) {
    std::rethrow_exception(engine_failure);
  }
#else
  ConnectorBase::do_batch_get(conn, req);
#endif
}

// No store or delete runs once the workers are joined, so the next
// connector on base_path may recycle.
void FSConnector::on_workers_stopped() {
  stop_read_pool();
  release_recycle_lock();
}

void FSConnector::do_batch_get_pooled(const Request& req) {
  const size_t num_keys = req.keys.size();
  ReadTile tile;
  tile.ok.assign(num_keys, 1);
  tile.remaining = num_keys;
  // Allocated before anything is published, so nothing below can throw
  // while readers hold pointers into this frame.
  std::vector<ReadTask> tasks(num_keys);
  std::vector<uint64_t> start_ns(num_keys);

  // The budget shares read_mu_, so each admitted run is reserved and queued
  // in one critical section, and a blocking reservation releases read_mu_
  // only after every earlier reserved read is queued for a reader.
  std::unique_lock<std::mutex> lk(read_mu_);
  size_t queued = 0;
  try {
    while (queued < num_keys) {
      const size_t admitted = read_budget_.reserve_locked(
          lk, req.buf_lens.data() + queued, num_keys - queued, true,
          start_ns.data() + queued);
      for (size_t i = queued; i < queued + admitted; ++i) {
        tasks[i] = ReadTask{&req, i, &tile, nullptr, start_ns[i]};
        if (read_queue_tail_ != nullptr) {
          read_queue_tail_->next = &tasks[i];
        } else {
          read_queue_head_ = &tasks[i];
        }
        read_queue_tail_ = &tasks[i];
        read_cv_.notify_one();
      }
      queued += admitted;
    }
  } catch (...) {
    // Only reserve_locked() can throw, with read_mu_ still held and nothing
    // reserved by the failed call.  Readers hold pointers into this frame,
    // so the objects not queued fail and the queued ones are waited for.
    for (size_t i = queued; i < num_keys; ++i) {
      tile.ok[i] = 0;
    }
    tile.remaining -= num_keys - queued;
  }
  tile.done_cv.wait(lk, [&tile] { return tile.remaining == 0; });
  lk.unlock();

  for (size_t i = 0; i < num_keys; ++i) {
    req.batch->per_key_results[req.start_idx + i] = tile.ok[i];
  }
}

// Connections are built here so a failure surfaces in the constructor.
void FSConnector::start_read_pool(int num_read_workers) {
  if (num_read_workers <= 0) {
    return;
  }
  const size_t num_threads = static_cast<size_t>(num_read_workers);
  read_conns_.reserve(num_threads);
  for (size_t i = 0; i < num_threads; ++i) {
    read_conns_.push_back(create_connection());
  }
  read_threads_.reserve(num_threads);
  for (size_t i = 0; i < num_threads; ++i) {
    read_threads_.emplace_back(
        [this, i] { this->read_thread_main(read_conns_[i]); });
  }
}

// Only after the workers are joined; see on_workers_stopped().
void FSConnector::stop_read_pool() {
  {
    std::lock_guard<std::mutex> lk(read_mu_);
    read_stop_ = true;
  }
  read_cv_.notify_all();
  for (auto& t : read_threads_) {
    if (t.joinable()) {
      t.join();
    }
  }
  read_threads_.clear();
  read_conns_.clear();
}

void FSConnector::read_thread_main(WorkerFSConn& conn) {
  // The lock taken to record a completion is kept to pop the next task, so
  // a busy reader takes read_mu_ once per object, not twice.
  std::unique_lock<std::mutex> lk(read_mu_);
  for (;;) {
    read_cv_.wait(lk,
                  [this] { return read_stop_ || read_queue_head_ != nullptr; });
    if (read_queue_head_ == nullptr) {
      return;  // the predicate admits an empty queue only on stop
    }
    ReadTask* task = read_queue_head_;
    read_queue_head_ = task->next;
    if (read_queue_head_ == nullptr) {
      read_queue_tail_ = nullptr;
    }
    lk.unlock();

    uint8_t ok = 1;
    const Request& req = *task->req;
    const std::string& key = req.keys[task->index];
    try {
      do_single_get(conn, key, req.buf_ptrs[task->index],
                    req.buf_lens[task->index], req.batch_chunk_num_bytes);
    } catch (const std::exception& e) {
      ok = 0;
      fprintf(stderr, "[LMCache GET] key %s failed: %s\n", key.c_str(),
              e.what());
    } catch (...) {
      ok = 0;
      fprintf(stderr, "[LMCache GET] key %s failed: unknown exception\n",
              key.c_str());
    }

    // The submitting worker may destroy `task` and its tile as soon as it
    // takes read_mu_ and sees remaining == 0, so nothing touches them
    // after the notify below; notifying under the lock keeps the tile
    // alive until then.
    lk.lock();
    read_budget_.release_locked(lk, req.buf_lens[task->index], 1,
                                &task->start_ns, 1);
    task->tile->ok[task->index] = ok;
    if (--task->tile->remaining == 0) {
      task->tile->done_cv.notify_one();
    }
  }
}

}  // namespace connector
}  // namespace lmcache
