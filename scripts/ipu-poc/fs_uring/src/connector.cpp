// SPDX-License-Identifier: Apache-2.0
#include "connector.h"

#include <fcntl.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <unistd.h>

#include <algorithm>
#include <chrono>
#include <climits>
#include <cstring>
#include <stdexcept>

namespace lmcache::connector {
namespace {
std::string format_shard(size_t index, size_t num_shard_dirs) {
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

size_t shard_index(const std::string& filename, size_t num_shard_dirs) {
  uint64_t h = 14695981039346656037ULL;
  for (unsigned char c : filename) {
    h ^= c;
    h *= 1099511628211ULL;
  }
  return static_cast<size_t>(h % num_shard_dirs);
}

constexpr const char* kPoolDir = ".pool";

// Stores and deletes do blocking metadata work; giving them their own lanes
// keeps them from queueing ahead of loads in the shared pool. 0 = shared.
WorkerPoolConfig lane_config(int store_workers, int delete_workers) {
  if (store_workers < 0 || delete_workers < 0) {
    throw std::invalid_argument("store_workers and delete_workers must be >= 0");
  }
  WorkerPoolConfig config;
  if (store_workers > 0) config.per_op_workers["store"] = store_workers;
  if (delete_workers > 0) config.per_op_workers["delete"] = delete_workers;
  return config;
}
// A load that opened a file just before its delete must finish before the
// file is overwritten for another key; 1 s is far above observed load latency.
constexpr std::chrono::milliseconds kRecycleQuarantine{1000};


class File {
 public:
  int fd = -1;
  File() = default;
  File(const File&) = delete;
  File& operator=(const File&) = delete;
  ~File() { close(); }
  void close() {
    if (fd >= 0) {
      ::close(fd);
      fd = -1;
    }
  }
};

struct Read {
  File file;
  size_t offset = 0;
  bool scheduled = false;
  bool done = false;
};

void maximum(std::atomic<uint64_t>& value, uint64_t candidate) {
  auto old = value.load(std::memory_order_relaxed);
  while (old < candidate && !value.compare_exchange_weak(
                                old, candidate, std::memory_order_relaxed)) {
  }
}

std::runtime_error system_error(const std::string& operation, int error) {
  return std::runtime_error(operation + ": " + std::strerror(error));
}

io_uring_cqe* wait_cqe(io_uring& ring) {
  io_uring_cqe* cqe = nullptr;
  int rc;
  do {
    rc = io_uring_wait_cqe(&ring, &cqe);
  } while (rc == -EINTR || rc == -EAGAIN || rc == -ENOMEM);
  if (rc < 0) throw system_error("io_uring_wait_cqe", -rc);
  return cqe;
}

}  // namespace

UringWorker::UringWorker(unsigned depth) {
  const int rc = io_uring_queue_init(depth, &ring, 0);
  if (rc < 0) throw system_error("io_uring_queue_init", -rc);
}

UringWorker::~UringWorker() { io_uring_queue_exit(&ring); }

FSUringConnector::FSUringConnector(std::string base_path, int num_workers,
                                   std::string relative_tmp_dir,
                                   bool use_odirect, unsigned io_depth,
                                   size_t num_shard_dirs,
                                   size_t recycle_max_files, int store_workers,
                                   int delete_workers)
    : ConnectorBase(num_workers, lane_config(store_workers, delete_workers)),
      base_path_(std::move(base_path)),
      use_odirect_(use_odirect),
      block_size_(0),
      io_depth_(io_depth),
      num_shard_dirs_(num_shard_dirs),
      recycle_max_files_(recycle_max_files) {
  if (io_depth == 0 || io_depth > 4096) {
    throw std::invalid_argument("io_depth must be in [1, 4096]");
  }
  std::filesystem::create_directories(base_path_);
  for (size_t i = 0; i < num_shard_dirs_; ++i) {
    std::filesystem::create_directories(base_path_ /
                                        format_shard(i, num_shard_dirs_));
  }
  if (recycle_max_files_ > 0) {
    pool_prefix_ =
        "r" + std::to_string(::getpid()) + "-" +
        std::to_string(
            std::chrono::steady_clock::now().time_since_epoch().count()) +
        "-";
    const size_t shards = num_shard_dirs_ == 0 ? 1 : num_shard_dirs_;
    for (size_t i = 0; i < shards; ++i) {
      pools_.push_back(std::make_unique<RecyclePool>());
      const auto pool_dir = shard_dir(i) / kPoolDir;
      std::filesystem::create_directories(pool_dir);
      // Files left by an earlier process are immediately reusable.
      for (const auto& entry : std::filesystem::directory_iterator(pool_dir)) {
        pools_[i]->files.emplace_back(entry.path().filename().string(),
                                      std::chrono::steady_clock::time_point{});
        pooled_.fetch_add(1);
      }
    }
  }
  if (!relative_tmp_dir.empty()) {
    tmp_path_ = base_path_ / relative_tmp_dir;
    std::filesystem::create_directories(tmp_path_);
  }
  struct statvfs st{};
  if (::statvfs(base_path_.c_str(), &st) != 0) {
    throw system_error("statvfs", errno);
  }
  block_size_ = st.f_bsize;
  if (block_size_ == 0)
    throw std::runtime_error("filesystem block size is zero");
  // Fail synchronously if any worker cannot obtain a ring. Otherwise the
  // generic worker harness would log the error and leave requests queued.
  // Every worker thread, in the shared pool or a dedicated lane, takes one
  // ring in create_connection().
  const int threads = num_workers + std::max(store_workers, 0) +
                      std::max(delete_workers, 0);
  for (int i = 0; i < threads; ++i) {
    rings_.push_back(std::make_unique<UringWorker>(io_depth_));
  }
  try {
    start_workers();
  } catch (...) {
    close();
    throw;
  }
}

FSUringConnector::~FSUringConnector() { close(); }

IOStats FSUringConnector::io_stats() const {
  return {submitted_.load(), completed_.load(), pending_.load(),
          max_worker_.load(), max_total_.load()};
}

int FSUringConnector::event_fd() const {
  std::lock_guard<std::mutex> lock(lifecycle_mu_);
  return closed_.load() ? -1 : ConnectorBase::event_fd();
}

std::vector<Completion> FSUringConnector::drain_completions() {
  std::lock_guard<std::mutex> lock(lifecycle_mu_);
  return closed_.load() ? std::vector<Completion>{}
                        : ConnectorBase::drain_completions();
}

void FSUringConnector::ensure_open() const {
  if (closed_.load()) throw std::runtime_error("connector is closed");
}

uint64_t FSUringConnector::submit_batch_get(
    const std::vector<std::string>& keys, const std::vector<void*>& bufs,
    const std::vector<size_t>& lens, size_t chunk_size) {
  std::lock_guard<std::mutex> lock(lifecycle_mu_);
  ensure_open();
  return ConnectorBase::submit_batch_get(keys, bufs, lens, chunk_size);
}

uint64_t FSUringConnector::submit_batch_set(
    const std::vector<std::string>& keys, const std::vector<void*>& bufs,
    const std::vector<size_t>& lens, size_t chunk_size) {
  std::lock_guard<std::mutex> lock(lifecycle_mu_);
  ensure_open();
  return ConnectorBase::submit_batch_set(keys, bufs, lens, chunk_size);
}

uint64_t FSUringConnector::submit_batch_exists(
    const std::vector<std::string>& keys) {
  std::lock_guard<std::mutex> lock(lifecycle_mu_);
  ensure_open();
  return ConnectorBase::submit_batch_exists(keys);
}

uint64_t FSUringConnector::submit_batch_delete(
    const std::vector<std::string>& keys) {
  std::lock_guard<std::mutex> lock(lifecycle_mu_);
  ensure_open();
  return ConnectorBase::submit_batch_delete(keys);
}

void FSUringConnector::close() {
  std::lock_guard<std::mutex> lock(lifecycle_mu_);
  ConnectorBase::close();
}

UringWorker* FSUringConnector::create_connection() {
  return rings_.at(next_worker_.fetch_add(1)).get();
}

void FSUringConnector::on_workers_stopped() { rings_.clear(); }

size_t FSUringConnector::choose_num_tiles(Op op, size_t num_items) const {
  return op == Op::BATCH_TILE_GET
             ? 1
             : ConnectorBase::choose_num_tiles(op, num_items);
}

std::string FSUringConnector::filename_for_key(
    const std::string& key) const {
  std::vector<std::string> parts;
  size_t start = 0;
  for (size_t i = 0; i <= key.size(); ++i) {
    if (i == key.size() || key[i] == '@') {
      parts.push_back(key.substr(start, i - start));
      start = i + 1;
    }
  }
  // Both the original 3/4-field keys and current group-aware 4/5-field
  // keys have the same filename transform: escape model '/', prefix rank.
  if (parts.size() < 3 || parts.size() > 5) {
    throw std::runtime_error("malformed filesystem key");
  }
  std::string model = parts[0];
  size_t pos = 0;
  while ((pos = model.find('/', pos)) != std::string::npos) {
    model.replace(pos, 1, "-SEP-");
    pos += 5;
  }
  std::string filename = model + "@0x" + parts[1];
  for (size_t i = 2; i < parts.size(); ++i) {
    if (parts[i].find('/') != std::string::npos) {
      throw std::runtime_error("filesystem key tail contains '/'");
    }
    if (i == parts.size() - 1 && parts[i].empty() && parts.size() > 3) continue;
    filename += "@" + parts[i];
  }
  return filename + ".data";
}

size_t FSUringConnector::shard_of(const std::string& filename) const {
  return num_shard_dirs_ == 0 ? 0 : shard_index(filename, num_shard_dirs_);
}

std::filesystem::path FSUringConnector::shard_dir(size_t shard) const {
  if (num_shard_dirs_ == 0) return base_path_;
  return base_path_ / format_shard(shard, num_shard_dirs_);
}

std::filesystem::path FSUringConnector::path_for_key(
    const std::string& key) const {
  const auto filename = filename_for_key(key);
  return shard_dir(shard_of(filename)) / filename;
}

bool FSUringConnector::recycle_into_pool(const std::filesystem::path& path,
                                         size_t shard) {
  if (pooled_.fetch_add(1) >= recycle_max_files_) {
    pooled_.fetch_sub(1);
    return false;
  }
  // Unique across processes: an earlier process may have left pool files.
  const std::string name = pool_prefix_ + std::to_string(pool_seq_.fetch_add(1));
  std::error_code ec;
  std::filesystem::rename(path, shard_dir(shard) / kPoolDir / name, ec);
  if (ec) {
    pooled_.fetch_sub(1);
    return false;
  }
  auto& pool = *pools_[shard];
  std::lock_guard<std::mutex> lock(pool.mu);
  pool.files.emplace_back(name, std::chrono::steady_clock::now());
  return true;
}

bool FSUringConnector::store_into_recycled(const std::filesystem::path& path,
                                           size_t shard, const void* buf,
                                           size_t len) {
  std::string name;
  {
    auto& pool = *pools_[shard];
    std::lock_guard<std::mutex> lock(pool.mu);
    if (pool.files.empty() ||
        std::chrono::steady_clock::now() - pool.files.front().second <
            kRecycleQuarantine) {
      return false;
    }
    name = std::move(pool.files.front().first);
    pool.files.pop_front();
  }
  pooled_.fetch_sub(1);
  const auto pooled = shard_dir(shard) / kPoolDir / name;
  File file;
  file.fd = ::open(pooled.c_str(), flags_for_buffer(O_WRONLY, buf, len));
  struct stat st{};
  if (file.fd < 0 || ::fstat(file.fd, &st) != 0 ||
      static_cast<size_t>(st.st_size) != len) {
    // Size mismatch or vanished file: drop it and use a fresh file instead.
    file.close();
    std::error_code ignored;
    std::filesystem::remove(pooled, ignored);
    return false;
  }
  try {
    size_t offset = 0;
    while (offset < len) {
      const ssize_t n = ::pwrite(file.fd, static_cast<const char*>(buf) + offset,
                                 len - offset, offset);
      if (n < 0 && errno == EINTR) continue;
      if (n <= 0) throw system_error("pwrite", n < 0 ? errno : EIO);
      offset += n;
    }
    file.close();
    std::filesystem::rename(pooled, path);
  } catch (...) {
    file.close();
    std::error_code ignored;
    std::filesystem::remove(pooled, ignored);
    throw;
  }
  return true;
}

int FSUringConnector::flags_for_buffer(int flags, const void* buf,
                                       size_t len) const {
  if (use_odirect_) {
    if (reinterpret_cast<uintptr_t>(buf) % block_size_ != 0 ||
        len % block_size_ != 0) {
      throw std::runtime_error("O_DIRECT buffer or length is not aligned");
    }
    flags |= O_DIRECT;
  }
  return flags | O_CLOEXEC;
}

void FSUringConnector::read_batch(UringWorker& worker, const Request& req) {
  if (worker.poisoned) {
    throw std::runtime_error(
        "io_uring worker is poisoned after an engine error");
  }
  std::vector<Read> reads(req.keys.size());
  size_t remaining = reads.size();
  unsigned outstanding = 0;
  auto finish = [&](size_t i, bool success) {
    reads[i].done = true;
    reads[i].file.close();
    req.batch->per_key_results[req.start_idx + i] = success ? 1 : 0;
    --remaining;
  };
  try {
    while (remaining > 0) {
      unsigned prepared = 0;
      for (size_t i = 0; i < reads.size() && outstanding + prepared < io_depth_;
           ++i) {
        auto& read = reads[i];
        if (read.done || read.scheduled) continue;
        if (is_stopping()) {
          finish(i, false);
          continue;
        }
        if (read.file.fd < 0) {
          try {
            if (req.buf_lens[i] == 0 || req.buf_lens[i] > INT_MAX) {
              throw std::runtime_error("read length must be in [1, INT_MAX]");
            }
            const auto path = path_for_key(req.keys[i]);
            const int flags =
                flags_for_buffer(O_RDONLY, req.buf_ptrs[i], req.buf_lens[i]);
            read.file.fd = ::open(path.c_str(), flags);
            if (read.file.fd < 0) throw system_error("open for read", errno);
          } catch (const std::exception&) {
            finish(i, false);
            continue;
          }
        }
        auto* sqe = io_uring_get_sqe(&worker.ring);
        if (!sqe) throw std::runtime_error("io_uring submission queue is full");
        io_uring_prep_read(sqe, read.file.fd,
                           static_cast<char*>(req.buf_ptrs[i]) + read.offset,
                           static_cast<unsigned>(req.buf_lens[i] - read.offset),
                           read.offset);
        io_uring_sqe_set_data64(sqe, i + 1);
        read.scheduled = true;
        ++prepared;
      }
      while (prepared > 0) {
        const int rc = io_uring_submit(&worker.ring);
        if (rc == -EINTR) continue;
        if (rc <= 0) throw system_error("io_uring_submit", rc < 0 ? -rc : EIO);
        outstanding += rc;
        prepared -= rc;
        submitted_.fetch_add(rc);
        const auto total = pending_.fetch_add(rc) + rc;
        maximum(max_worker_, outstanding);
        maximum(max_total_, total);
      }
      if (outstanding == 0) continue;
      auto* cqe = wait_cqe(worker.ring);
      const size_t i = io_uring_cqe_get_data64(cqe) - 1;
      const int result = cqe->res;
      io_uring_cqe_seen(&worker.ring, cqe);
      --outstanding;
      pending_.fetch_sub(1);
      completed_.fetch_add(1);
      auto& read = reads.at(i);
      read.scheduled = false;
      if (result <= 0) {
        finish(i, false);
      } else {
        read.offset += static_cast<size_t>(result);
        if (read.offset == req.buf_lens[i]) {
          finish(i, true);
        } else if (read.offset > req.buf_lens[i] ||
                   (use_odirect_ && read.offset % block_size_ != 0)) {
          finish(i, false);
        }
      }
    }
  } catch (...) {
    // Never report a failed batch while the kernel still owns its buffers.
    // Drain every successfully submitted request before closing its fd.
    worker.poisoned = true;
    while (outstanding > 0) {
      unsigned head;
      io_uring_cqe* cqe;
      unsigned retired = 0;
      io_uring_for_each_cqe(&worker.ring, head, cqe) {
        if (retired == outstanding) break;
        ++retired;
      }
      if (retired > 0) {
        io_uring_cq_advance(&worker.ring, retired);
        outstanding -= retired;
        pending_.fetch_sub(retired);
        completed_.fetch_add(retired);
      } else {
        // CQ inspection stays usable when the wait syscall fails. Enter
        // for task-work/overflow progress, then retry; never retire a
        // buffer merely because another wait/enter returned an error.
        (void)io_uring_get_events(&worker.ring);
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
      }
    }
    throw;
  }
}

void FSUringConnector::do_batch_get(UringWorker*& worker, const Request& req) {
  read_batch(*worker, req);
}

void FSUringConnector::do_single_get(UringWorker*& worker,
                                     const std::string& key, void* buf,
                                     size_t len, size_t chunk_size) {
  Request req;
  req.keys = {key};
  req.buf_ptrs = {buf};
  req.buf_lens = {len};
  req.batch_chunk_num_bytes = chunk_size;
  req.batch = std::make_shared<BatchState>();
  req.batch->per_key_results = {0};
  read_batch(*worker, req);
  if (!req.batch->per_key_results[0]) throw std::runtime_error("read failed");
}

void FSUringConnector::do_single_set(UringWorker*& worker,
                                     const std::string& key, const void* buf,
                                     size_t len, size_t chunk_size) {
  (void)worker;
  (void)chunk_size;
  if (is_stopping()) throw std::runtime_error("connector is closing");
  const auto filename = filename_for_key(key);
  const size_t shard = shard_of(filename);
  const auto path = shard_dir(shard) / filename;
  if (std::filesystem::exists(path)) return;
  if (recycle_max_files_ > 0 && store_into_recycled(path, shard, buf, len)) {
    return;
  }
  auto temp = tmp_path_.empty() ? path : tmp_path_ / path.filename();
  if (tmp_path_.empty()) temp.replace_extension(".tmp");
  File file;
  file.fd =
      ::open(temp.c_str(),
             flags_for_buffer(O_CREAT | O_WRONLY | O_TRUNC, buf, len), 0644);
  if (file.fd < 0) throw system_error("open for write", errno);
  try {
    size_t offset = 0;
    while (offset < len) {
      const ssize_t n = ::write(file.fd, static_cast<const char*>(buf) + offset,
                                len - offset);
      if (n < 0 && errno == EINTR) continue;
      if (n <= 0) throw system_error("write", n < 0 ? errno : EIO);
      offset += n;
    }
    file.close();
    std::filesystem::rename(temp, path);
  } catch (...) {
    file.close();
    std::error_code ignored;
    std::filesystem::remove(temp, ignored);
    throw;
  }
}

bool FSUringConnector::do_single_exists(UringWorker*& worker,
                                        const std::string& key) {
  (void)worker;
  return std::filesystem::exists(path_for_key(key));
}

bool FSUringConnector::do_single_delete(UringWorker*& worker,
                                        const std::string& key) {
  (void)worker;
  const auto filename = filename_for_key(key);
  const size_t shard = shard_of(filename);
  const auto path = shard_dir(shard) / filename;
  if (recycle_max_files_ > 0 && recycle_into_pool(path, shard)) return true;
  std::error_code ec;
  return std::filesystem::remove(path, ec);
}

}  // namespace lmcache::connector
