// SPDX-License-Identifier: Apache-2.0
#pragma once

#include "read_budget.h"
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <vector>

namespace lmcache {
namespace connector {

// Reads longer than this go through the synchronous fallback, so every
// length the ring carries fits an SQE and a CQE result.
static constexpr size_t kMaxRingReadBytes = size_t{1} << 30;

// One object for UringReader::read_batch().
struct ReadItem {
  std::filesystem::path path;
  void* buf;
  size_t len;      // in [1, kMaxRingReadBytes]
  int open_flags;  // O_RDONLY, plus O_DIRECT where try_enable_odirect allowed
  // With O_DIRECT, the block size every partial-read offset must be a
  // multiple of; 0 for a buffered read.
  size_t block_size = 0;
};

}  // namespace connector
}  // namespace lmcache

#ifdef LMCACHE_HAVE_LIBURING

  #include <liburing.h>
  #include "fault_injection.h"

namespace lmcache {
namespace connector {

// One io_uring and the loop that pipelines a batch of reads through it.
// Owned by one connector worker and used only from that worker's thread.
//
// Each read moves queued -> prepared -> accepted -> done:
//   queued    in the batch; nothing reserved or open.
//   prepared  budget reserved, file open, SQE in the ring, not yet taken by
//             the kernel.
//   accepted  counted from io_uring_submit's return; the kernel owns the
//             buffer until its CQE is reaped.
//   done      CQE reaped (or never submitted), file closed, budget released.
class UringReader {
 public:
  // Creates a ring of io_depth SQ entries (CQ: the liburing default, twice
  // that).  budget is used in its self-locking mode and must outlive the
  // reader.
  //
  // Raises std::runtime_error if the ring cannot be created.
  UringReader(unsigned io_depth, ReadBudget& budget);
  ~UringReader();

  UringReader(const UringReader&) = delete;
  UringReader& operator=(const UringReader&) = delete;

  // Reads every item, at most io_depth at once, and sets ok[i] to 1 when
  // item i was read in full and 0 otherwise.  A per-read failure (open
  // error, EOF before len, an O_DIRECT partial read leaving an unaligned
  // offset) fails only that item.  Items not yet started when `stopping`
  // becomes true fail; accepted reads are still reaped.
  //
  // Raises std::runtime_error on an engine error (submit or wait failure,
  // a CQE for an unknown request), only after every accepted request has
  // been reaped, every file closed and every reservation released; the
  // unfinished items are 0 and the ring has been recreated.  If the ring
  // cannot be recreated, usable() becomes false and later calls fail every
  // item and raise.  If reaping cannot make progress for 10 s, the process
  // aborts: returning would hand back buffers the kernel may still write.
  void read_batch(std::vector<ReadItem>& items, uint8_t* ok,
                  const std::atomic<bool>& stopping);

  // False once an engine error left the reader without a ring.
  bool usable() const;

 private:
  struct Batch;

  int queue_init();
  int submit(unsigned accepted);
  int wait(io_uring_cqe** cqe);
  // Takes a CQE apart for the batch; false for a request it does not know.
  bool handle_cqe(Batch& b, uint64_t user_data, int res);
  // Reaps every accepted request, closes and releases everything still
  // held, and recreates the ring.  Aborts if reaping cannot progress.
  void recover(Batch& b);

  const unsigned depth_;
  ReadBudget& budget_;
  io_uring ring_;
  bool usable_ = false;
  #ifdef LMCACHE_FS_FAULT_INJECTION
  FsFault fault_ = FsFault::kNone;
  unsigned fault_delay_ms_ = 0;
  bool submit_error_fired_ = false;
  #endif
};

}  // namespace connector
}  // namespace lmcache

#endif  // LMCACHE_HAVE_LIBURING
