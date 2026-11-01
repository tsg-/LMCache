// SPDX-License-Identifier: Apache-2.0
// Forced include for a separate test build; never used in the release plugin.
#pragma once
#include <liburing.h>

#include <atomic>
#include <cstdlib>
#include <cstring>

namespace lmcache_test {
inline int wait_cqe(io_uring* ring, io_uring_cqe** cqe) {
  if (std::getenv("LMCACHE_TEST_WAIT_ERROR")) return -EIO;
  return io_uring_wait_cqe(ring, cqe);
}

inline int queue_init(unsigned entries, io_uring* ring, unsigned flags) {
  static std::atomic<unsigned> count{0};
  if (count.fetch_add(1) > 0 && std::getenv("LMCACHE_TEST_REINIT_ERROR")) {
    // liburing clears the ring before a failed setup; reproduce that state.
    std::memset(ring, 0, sizeof(*ring));
    return -EPERM;
  }
  return io_uring_queue_init(entries, ring, flags);
}

inline int submit(io_uring* ring) {
  if (std::getenv("LMCACHE_TEST_SUBMIT_ERROR")) return -EIO;
  static std::atomic<unsigned> count{0};
  if (count.fetch_add(1) > 0 &&
      std::getenv("LMCACHE_TEST_SUBMIT_AFTER_FIRST_ERROR")) {
    return -EIO;
  }
  return io_uring_submit(ring);
}
}  // namespace lmcache_test

#define io_uring_wait_cqe lmcache_test::wait_cqe
#define io_uring_queue_init lmcache_test::queue_init
#define io_uring_submit lmcache_test::submit
