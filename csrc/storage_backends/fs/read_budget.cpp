// SPDX-License-Identifier: Apache-2.0

#include "read_budget.h"
#include <chrono>
#include <stdexcept>

namespace lmcache {
namespace connector {

static uint64_t monotonic_ns() {
  return static_cast<uint64_t>(
      std::chrono::duration_cast<std::chrono::nanoseconds>(
          std::chrono::steady_clock::now().time_since_epoch())
          .count());
}

ReadBudget::ReadBudget(size_t max_bytes) : max_bytes_(max_bytes) {}

bool ReadBudget::admits(size_t len) const {
  return reads_in_flight_ < kMinReadsInFlight ||
         bytes_in_flight_ + len <= max_bytes_;
}

size_t ReadBudget::reserve(const size_t* lens, size_t n, bool block,
                           uint64_t* start_ns) {
  if (n == 0) {
    return 0;
  }
  std::unique_lock<std::mutex> lk(mu_);
  if (block && !admits(lens[0])) {
    ++waiters_;
    cv_.wait(lk, [this, lens] { return admits(lens[0]); });
    --waiters_;
  }
  const uint64_t now = monotonic_ns();
  size_t admitted = 0;
  try {
    while (admitted < n && admits(lens[admitted])) {
      start_ns_.insert(now);
      bytes_in_flight_ += lens[admitted];
      ++reads_in_flight_;
      start_ns[admitted] = now;
      ++admitted;
    }
  } catch (...) {
    // Undo this call's admissions, so the caller owns nothing to release.
    for (size_t i = 0; i < admitted; ++i) {
      start_ns_.erase(start_ns_.find(now));
      bytes_in_flight_ -= lens[i];
      --reads_in_flight_;
    }
    throw;
  }
  if (bytes_in_flight_ > peak_bytes_) {
    peak_bytes_ = bytes_in_flight_;
  }
  return admitted;
}

void ReadBudget::release(size_t bytes, size_t reads, const uint64_t* start_ns,
                         size_t n) {
  std::lock_guard<std::mutex> lk(mu_);
  if (bytes > bytes_in_flight_ || reads > reads_in_flight_ ||
      n > start_ns_.size()) {
    throw std::logic_error("ReadBudget::release returns more than reserved");
  }
  for (size_t i = 0; i < n; ++i) {
    if (start_ns_.find(start_ns[i]) == start_ns_.end()) {
      throw std::logic_error("ReadBudget::release of an unregistered read");
    }
  }
  for (size_t i = 0; i < n; ++i) {
    const auto it = start_ns_.find(start_ns[i]);
    if (it == start_ns_.end()) {
      // Only reachable when one start time is passed more often than it
      // was registered.
      throw std::logic_error("ReadBudget::release of an unregistered read");
    }
    start_ns_.erase(it);
  }
  bytes_in_flight_ -= bytes;
  reads_in_flight_ -= reads;

  // Large reads can hold more than the budget, so the free bytes are
  // computed without wrapping.
  const size_t free_bytes =
      bytes_in_flight_ < max_bytes_ ? max_bytes_ - bytes_in_flight_ : 0;
  if (waiters_ != 0 && (reads_in_flight_ < kMinReadsInFlight ||
                        free_bytes >= max_bytes_ / kReadBudgetWakeSlices)) {
    cv_.notify_all();
  }
}

uint64_t ReadBudget::oldest_start_ns() const {
  std::lock_guard<std::mutex> lk(mu_);
  return start_ns_.empty() ? UINT64_MAX : *start_ns_.begin();
}

size_t ReadBudget::max_bytes() const { return max_bytes_; }

size_t ReadBudget::peak_bytes() const {
  std::lock_guard<std::mutex> lk(mu_);
  return peak_bytes_;
}

}  // namespace connector
}  // namespace lmcache
