// SPDX-License-Identifier: Apache-2.0

#include "connector.h"
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <stdexcept>
#include <string>

namespace lmcache {
namespace connector {

// ---------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------

std::string FSConnector::replace_all(const std::string& str,
                                     const std::string& from,
                                     const std::string& to) {
  std::string result = str;
  size_t pos = 0;
  while ((pos = result.find(from, pos)) != std::string::npos) {
    result.replace(pos, from.size(), to);
    pos += to.size();
  }
  return result;
}

std::string FSConnector::key_to_filename(const std::string& key) {
  // Input key format (from _object_key_to_string):
  //   Unsalted: <model_name>@<kv_rank_hex>@<chunk_hash_hex>
  //   Salted  : <model_name>@<kv_rank_hex>@<chunk_hash_hex>@<cache_salt>
  //
  // Output filename (matching fs_l2_adapter.py._object_key_to_filename):
  //   Unsalted: <model_name_safe>@0x<kv_rank_hex>@<chunk_hash_hex>.data
  //   Salted  :
  //   <model_name_safe>@0x<kv_rank_hex>@<chunk_hash_hex>@<cache_salt>.data
  //
  // The unsalted 3-field shape is bit-identical to the pre-cache_salt
  // format, so existing cache directories remain valid.
  //
  // NOTE: both model_name and cache_salt are forbidden from containing
  // '@' (invariant enforced on the Python side), so splitting on '@'
  // is unambiguous — no marker, no rsplit.

  // Split on '@' — must yield 3 (unsalted) or 4 (salted) fields.
  std::vector<std::string> parts;
  size_t start = 0;
  for (size_t pos = 0; pos <= key.size(); ++pos) {
    if (pos == key.size() || key[pos] == KEY_SEP) {
      parts.emplace_back(key.substr(start, pos - start));
      start = pos + 1;
    }
  }
  if (parts.size() != 3 && parts.size() != 4) {
    throw std::runtime_error(
        "FSConnector: malformed key (expected 3 or 4 '@'-separated fields): " +
        key);
  }

  const std::string& model_name = parts[0];
  const std::string& kv_rank_hex = parts[1];
  const std::string& chunk_hash = parts[2];
  const std::string cache_salt = parts.size() == 4 ? parts[3] : std::string();

  // Replace '/' with '-SEP-' for filesystem safety
  std::string safe_model = replace_all(model_name, "/", PATH_SLASH_REPLACEMENT);

  // Emit filename. Salt is appended at the tail so the unsalted shape
  // matches what older builds wrote to disk.
  std::string result;
  result.reserve(safe_model.size() + kv_rank_hex.size() + chunk_hash.size() +
                 cache_salt.size() + 32);
  result += safe_model;
  result += KEY_SEP;
  result += "0x";
  result += kv_rank_hex;
  result += KEY_SEP;
  result += chunk_hash;
  if (!cache_salt.empty()) {
    result += KEY_SEP;
    result += cache_salt;
  }
  result += FILE_EXT;
  return result;
}

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

static constexpr const char* POOL_DIR = ".pool";
// Far above observed load latency; see the constructor comment in connector.h.
static constexpr std::chrono::milliseconds RECYCLE_QUARANTINE{1000};

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
  return dir / POOL_DIR;
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
  auto& pool = *pools_[index];
  std::lock_guard<std::mutex> lock(pool.mu);
  pool.files.emplace_back(std::move(name), std::chrono::steady_clock::now());
  return true;
}

bool FSConnector::store_into_recycled(WorkerFSConn& conn,
                                      const std::filesystem::path& file_path,
                                      size_t index, const void* buf,
                                      size_t len) {
  std::string name;
  {
    auto& pool = *pools_[index];
    std::lock_guard<std::mutex> lock(pool.mu);
    if (pool.files.empty() ||
        std::chrono::steady_clock::now() - pool.files.front().second <
            RECYCLE_QUARANTINE) {
      return false;
    }
    name = std::move(pool.files.front().first);
    pool.files.pop_front();
  }
  pooled_.fetch_sub(1);
  auto pooled = pool_dir(index) / name;
  int flags = O_WRONLY;
  if (conn.use_odirect) {
    try_enable_odirect(flags, buf, len, conn.disk_block_size);
  }
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
    std::filesystem::remove(pooled);
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

FSConnector::FSConnector(std::string base_path, int num_workers,
                         std::string relative_tmp_dir, bool use_odirect,
                         size_t read_ahead_size, size_t num_shard_dirs,
                         size_t recycle_max_files, int store_workers,
                         int delete_workers)
    : ConnectorBase(num_workers, lane_config(store_workers, delete_workers)),
      base_path_(std::move(base_path)),
      relative_tmp_dir_(std::move(relative_tmp_dir)),
      use_odirect_(use_odirect),
      disk_block_size_(0),
      read_ahead_size_(read_ahead_size),
      num_shard_dirs_(num_shard_dirs),
      recycle_max_files_(recycle_max_files) {
  // Create base directory
  std::filesystem::create_directories(base_path_);

  for (size_t i = 0; i < num_shard_dirs_; ++i) {
    std::filesystem::create_directories(std::filesystem::path(base_path_) /
                                        format_shard(i, num_shard_dirs_));
  }

  if (recycle_max_files_ > 0) {
    // Unique across processes, because an earlier process may have left
    // pool files behind; those are adopted and immediately reusable.
    pool_prefix_ =
        "r" + std::to_string(::getpid()) + "-" +
        std::to_string(
            std::chrono::steady_clock::now().time_since_epoch().count()) +
        "-";
    size_t pools = num_shard_dirs_ == 0 ? 1 : num_shard_dirs_;
    for (size_t i = 0; i < pools; ++i) {
      pools_.push_back(std::make_unique<RecyclePool>());
      std::filesystem::create_directories(pool_dir(i));
      for (const auto& entry :
           std::filesystem::directory_iterator(pool_dir(i))) {
        pools_[i]->files.emplace_back(entry.path().filename().string(),
                                      std::chrono::steady_clock::time_point{});
        pooled_.fetch_add(1);
      }
    }
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

  start_workers();  // IMPORTANT: call at END of constructor
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

}  // namespace connector
}  // namespace lmcache
