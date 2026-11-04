// SPDX-License-Identifier: Apache-2.0

#include "uring_reader.h"

#ifdef LMCACHE_HAVE_LIBURING

  #include <fcntl.h>
  #include <unistd.h>
  #include <algorithm>
  #include <cerrno>
  #include <chrono>
  #include <cstdio>
  #include <cstdlib>
  #include <cstring>
  #include <stdexcept>
  #include <string>
  #include <thread>

namespace lmcache {
namespace connector {

namespace {

// How long reaping may keep failing before the process aborts.
constexpr std::chrono::seconds kReapGiveUp{10};

enum class State : uint8_t { kQueued, kPrepared, kAccepted, kDone };

struct Slot {
  int fd = -1;
  size_t offset = 0;
  uint64_t start_ns = 0;
  State state = State::kQueued;
  bool reserved = false;
  #ifdef LMCACHE_FS_FAULT_INJECTION
  bool faulted = false;
  #endif
};

std::runtime_error engine_error(const std::string& what, int err) {
  return std::runtime_error("io_uring " + what + ": " + std::strerror(err));
}

}  // namespace

struct UringReader::Batch {
  Batch(std::vector<ReadItem>& items_in, uint8_t* ok_in, unsigned depth)
      : items(items_in),
        ok(ok_in),
        slots(items_in.size()),
        lens(items_in.size()),
        start_ns(items_in.size()) {
    for (size_t i = 0; i < items.size(); ++i) lens[i] = items[i].len;
    prepared.reserve(depth);
    // Each read adds at most one entry between two flushes, so this never
    // reallocates, and adding cannot throw during recovery.
    rel_start_ns.reserve(items.size());
  }

  std::vector<ReadItem>& items;
  uint8_t* ok;
  std::vector<Slot> slots;
  std::vector<size_t> lens;
  std::vector<uint64_t> start_ns;  // reserve() output, copied into slots
  std::vector<size_t> prepared;    // slot indices in SQE (= kernel) order
  size_t prepared_head = 0;
  size_t next = 0;  // first queued read
  size_t done = 0;
  unsigned accepted = 0;
  // Reservations to return with the next release, one acquisition each.
  size_t rel_bytes = 0;
  std::vector<uint64_t> rel_start_ns;
  bool finished_any = false;  // a file closed since the last reap

  unsigned num_prepared() const {
    return static_cast<unsigned>(prepared.size() - prepared_head);
  }

  void unreserve(size_t i) {
    Slot& s = slots[i];
    if (s.reserved) {
      rel_bytes += lens[i];
      rel_start_ns.push_back(s.start_ns);
      s.reserved = false;
    }
  }

  void finish(size_t i, bool success) {
    Slot& s = slots[i];
    if (s.fd >= 0) {
      ::close(s.fd);
      s.fd = -1;
    }
    unreserve(i);
    s.state = State::kDone;
    ok[i] = success ? 1 : 0;
    ++done;
    finished_any = true;
  }

  void fail(size_t i, const std::string& why) {
    fprintf(stderr, "[LMCache GET] read of %s failed: %s\n",
            items[i].path.c_str(), why.c_str());
    finish(i, false);
  }

  // Queues an SQE for the unread rest of slot i.  The caller guarantees a
  // free ring slot, so a full SQ is a bookkeeping error.
  void prep(io_uring& ring, size_t i) {
    io_uring_sqe* sqe = io_uring_get_sqe(&ring);
    if (sqe == nullptr) {
      throw std::runtime_error("io_uring submission queue is full");
    }
    Slot& s = slots[i];
    const ReadItem& item = items[i];
    io_uring_prep_read(sqe, s.fd, static_cast<char*>(item.buf) + s.offset,
                       static_cast<unsigned>(item.len - s.offset), s.offset);
    io_uring_sqe_set_data64(sqe, i + 1);
    s.state = State::kPrepared;
    prepared.push_back(i);
  }

  // The kernel takes SQEs in order, so the first n prepared are accepted.
  void accept(unsigned n) {
    for (unsigned k = 0; k < n; ++k) {
      slots[prepared[prepared_head++]].state = State::kAccepted;
    }
    if (prepared_head == prepared.size()) {
      prepared.clear();
      prepared_head = 0;
    }
    accepted += n;
  }

  void flush(ReadBudget& budget) {
    if (rel_start_ns.empty()) return;
    const size_t bytes = rel_bytes;
    const size_t n = rel_start_ns.size();
    rel_bytes = 0;
    // Cleared whatever happens, so a failed release is never retried.
    try {
      budget.release(bytes, n, rel_start_ns.data(), n);
    } catch (...) {
      rel_start_ns.clear();
      throw;
    }
    rel_start_ns.clear();
  }
};

UringReader::UringReader(unsigned io_depth, ReadBudget& budget)
    : depth_(io_depth), budget_(budget) {
  if (io_depth == 0) {
    throw std::invalid_argument("io_depth must be positive");
  }
  #ifdef LMCACHE_FS_FAULT_INJECTION
  fault_ = fs_fault_from_env();
  fault_delay_ms_ = fs_fault_delay_ms_from_env();
  #endif
  std::memset(&ring_, 0, sizeof(ring_));
  const int rc = queue_init();
  if (rc < 0) {
    throw engine_error("queue_init", -rc);
  }
  usable_ = true;
}

UringReader::~UringReader() {
  if (usable_) io_uring_queue_exit(&ring_);
}

bool UringReader::usable() const { return usable_; }

int UringReader::queue_init() {
  #ifdef LMCACHE_FS_FAULT_INJECTION
  if (fs_fault_from_env() == FsFault::kQueueInitError) return -EPERM;
  #endif
  return io_uring_queue_init(depth_, &ring_, 0);
}

int UringReader::submit(unsigned accepted) {
  #ifdef LMCACHE_FS_FAULT_INJECTION
  if (fault_ == FsFault::kSubmitError && !submit_error_fired_ && accepted > 0) {
    submit_error_fired_ = true;
    return -EIO;
  }
  if (fault_ == FsFault::kSubmitPartial &&
      ring_.sq.sqe_tail - ring_.sq.sqe_head > 1) {
    // Publish only the first SQE; the rest stay queued for the next call,
    // exactly as when the kernel takes fewer than were prepared.
    const unsigned tail = ring_.sq.sqe_tail;
    ring_.sq.sqe_tail = ring_.sq.sqe_head + 1;
    const int rc = io_uring_submit(&ring_);
    ring_.sq.sqe_tail = tail;
    return rc;
  }
  #else
  (void)accepted;
  #endif
  return io_uring_submit(&ring_);
}

int UringReader::wait(io_uring_cqe** cqe) {
  #ifdef LMCACHE_FS_FAULT_INJECTION
  if (fault_ == FsFault::kWaitErrorForever) return -EIO;
  #endif
  return io_uring_wait_cqe(&ring_, cqe);
}

bool UringReader::handle_cqe(Batch& b, uint64_t user_data, int res) {
  if (user_data == 0 || user_data > b.slots.size() ||
      b.slots[user_data - 1].state != State::kAccepted) {
    return false;
  }
  const size_t i = user_data - 1;
  Slot& s = b.slots[i];
  const ReadItem& item = b.items[i];
  --b.accepted;
  #ifdef LMCACHE_FS_FAULT_INJECTION
  if (fault_delay_ms_ > 0) {
    std::this_thread::sleep_for(std::chrono::milliseconds(fault_delay_ms_));
  }
  if (fault_ == FsFault::kCqeEintr && !s.faulted && res > 0) {
    s.faulted = true;
    res = -EINTR;
  } else if (fault_ == FsFault::kCqeShort && res > 1) {
    size_t shorter = static_cast<size_t>(res) / 2;
    if (item.block_size != 0) shorter -= shorter % item.block_size;
    if (shorter > 0) res = static_cast<int>(shorter);
  }
  #endif
  if (res == -EINTR) {
    b.prep(ring_, i);  // the reaped CQE freed this read's ring slot
    return true;
  }
  if (res < 0) {
    b.fail(i, std::strerror(-res));
    return true;
  }
  if (res == 0) {
    b.fail(i, "incomplete read: expected " + std::to_string(item.len) +
                  ", got " + std::to_string(s.offset));
    return true;
  }
  s.offset += static_cast<size_t>(res);
  if (s.offset == item.len) {
    b.finish(i, true);
  } else if (s.offset > item.len) {
    b.fail(i, "read past the requested length");
  } else if (item.block_size != 0 && s.offset % item.block_size != 0) {
    b.fail(i, "O_DIRECT short read left an unaligned offset");
  } else {
    b.prep(ring_, i);  // short read: the rest, from the new offset
  }
  return true;
}

void UringReader::read_batch(std::vector<ReadItem>& items, uint8_t* ok,
                             const std::atomic<bool>& stopping) {
  const size_t n = items.size();
  std::fill(ok, ok + n, uint8_t{0});
  if (!usable_) {
    throw std::runtime_error(
        "io_uring ring is unusable after an earlier engine error");
  }
  if (n == 0) return;

  Batch b(items, ok, depth_);
  // Set by an EMFILE/ENFILE open while reads are in flight: stop refilling
  // until one of them closes its file.
  bool throttled = false;
  try {
    while (b.done < n) {
      if (stopping.load(std::memory_order_acquire)) {
        // Queued reads hold nothing, so they just fail; ok stays 0.
        for (; b.next < n; ++b.next) {
          if (b.slots[b.next].state == State::kQueued) {
            b.slots[b.next].state = State::kDone;
            ++b.done;
          }
        }
      }

      // Fill free ring slots from the front of the batch, reserving them
      // in one acquisition.  Only a reader with nothing in the ring may
      // block for budget, and it first returns what it holds.
      unsigned in_ring = b.accepted + b.num_prepared();
      if (!throttled && b.next < n && in_ring < depth_) {
        const bool block = in_ring == 0;
        if (block) b.flush(budget_);
        const size_t want = std::min<size_t>(depth_ - in_ring, n - b.next);
        const size_t got = budget_.reserve(b.lens.data() + b.next, want, block,
                                           b.start_ns.data() + b.next);
        const size_t end = b.next + got;
        for (size_t i = b.next; i < end; ++i) {
          b.slots[i].reserved = true;
          b.slots[i].start_ns = b.start_ns[i];
        }
        size_t i = b.next;
        for (; i < end; ++i) {
          const int fd = ::open(items[i].path.c_str(), items[i].open_flags);
          if (fd < 0) {
            const int err = errno;
            if ((err == EMFILE || err == ENFILE) && in_ring > 0) {
              for (size_t k = i; k < end; ++k) b.unreserve(k);
  #ifdef LMCACHE_FS_FAULT_INJECTION
              fprintf(stderr, "[LMCache GET] io_uring open hit %s; throttling\n",
                      std::strerror(err));
  #endif
              throttled = true;
              break;
            }
            b.fail(i,
                   std::string("open for read failed: ") + std::strerror(err));
            continue;
          }
          b.slots[i].fd = fd;
          b.prep(ring_, i);
          ++in_ring;
        }
        b.next = i;
      }

      // Hand the kernel everything prepared before blocking on anything.
      while (b.num_prepared() > 0) {
        const int rc = submit(b.accepted);
        if (rc == -EINTR) continue;
        if (rc <= 0) throw engine_error("submit", rc < 0 ? -rc : EIO);
        b.accept(static_cast<unsigned>(rc));
      }

      if (b.accepted == 0) {
        b.flush(budget_);
        continue;
      }

      // Reap one CQE by waiting, then whatever else is ready, and return
      // the group's bytes with one release.
      io_uring_cqe* cqe = nullptr;
      int rc = wait(&cqe);
      if (rc == -EINTR) continue;
      if (rc < 0) throw engine_error("wait_cqe", -rc);
      b.finished_any = false;
      for (;;) {
        const uint64_t user_data = io_uring_cqe_get_data64(cqe);
        const int res = cqe->res;
        io_uring_cqe_seen(&ring_, cqe);
        if (!handle_cqe(b, user_data, res)) {
          throw std::runtime_error("io_uring CQE for an unknown request");
        }
        if (b.accepted == 0 || io_uring_peek_cqe(&ring_, &cqe) != 0) break;
      }
      if (b.finished_any) throttled = false;
      b.flush(budget_);
    }
  } catch (const std::exception& e) {
    recover(b);
    throw std::runtime_error(std::string("io_uring read engine error: ") +
                             e.what());
  }
}

void UringReader::recover(Batch& b) {
  // The kernel owns every accepted buffer until its CQE is reaped, so
  // nothing below runs until the accepted count reaches zero.
  using Clock = std::chrono::steady_clock;
  bool failing = false;
  Clock::time_point failing_since;
  int last_error = 0;
  while (b.accepted > 0) {
    io_uring_cqe* cqe = nullptr;
    const int rc = wait(&cqe);
    if (rc == -EINTR) continue;
    if (rc == 0) {
      const uint64_t user_data = io_uring_cqe_get_data64(cqe);
      io_uring_cqe_seen(&ring_, cqe);
      if (user_data != 0 && user_data <= b.slots.size() &&
          b.slots[user_data - 1].state == State::kAccepted) {
        failing = false;
        --b.accepted;
        b.finish(user_data - 1, false);
        continue;
      }
      // An unknown CQE retires nothing (the request it stands for may still
      // be in the kernel), so it counts toward the abort like a failed wait.
      last_error = EPROTO;
    } else {
      last_error = -rc;
    }
    const auto now = Clock::now();
    if (!failing) {
      failing = true;
      failing_since = now;
    } else if (now - failing_since >= kReapGiveUp) {
      fprintf(stderr,
              "[LMCache GET] io_uring reaping failed: %u requests still "
              "accepted by the kernel after %lld s of failed waits (last "
              "error: %s; ring depth %u, %zu of %zu reads done); aborting so "
              "their buffers are never handed back\n",
              b.accepted, static_cast<long long>(kReapGiveUp.count()),
              std::strerror(last_error), depth_, b.done, b.slots.size());
      fflush(stderr);
      std::abort();
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(1));
  }

  // Nothing is accepted any more: close and release everything else.
  // Unaccepted SQEs are discarded with the ring below.
  for (size_t i = 0; i < b.slots.size(); ++i) {
    Slot& s = b.slots[i];
    if (s.state == State::kDone) continue;
    if (s.fd >= 0) {
      ::close(s.fd);
      s.fd = -1;
    }
    b.unreserve(i);
    s.state = State::kDone;
    b.ok[i] = 0;
  }
  b.done = b.slots.size();
  try {
    b.flush(budget_);
  } catch (const std::exception& e) {
    fprintf(stderr, "[LMCache GET] io_uring budget release failed: %s\n",
            e.what());
  }

  io_uring_queue_exit(&ring_);
  std::memset(&ring_, 0, sizeof(ring_));
  usable_ = false;
  const int rc = queue_init();
  if (rc < 0) {
    fprintf(stderr,
            "[LMCache GET] io_uring ring could not be recreated (%s); this "
            "worker fails its later reads\n",
            std::strerror(-rc));
    return;
  }
  usable_ = true;
}

}  // namespace connector
}  // namespace lmcache

#endif  // LMCACHE_HAVE_LIBURING
