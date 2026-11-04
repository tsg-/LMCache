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

ReadBudget::ReadBudget(size_t max_bytes)
    : max_bytes_(max_bytes), mu_(own_mu_) {}

ReadBudget::ReadBudget(size_t max_bytes, std::mutex& mu)
    : max_bytes_(max_bytes), mu_(mu) {}

bool ReadBudget::admits(size_t len) const {
  return reads_in_flight_ < kMinReadsInFlight ||
         bytes_in_flight_ + len <= max_bytes_;
}

void ReadBudget::check_held(const std::unique_lock<std::mutex>& held) const {
  if (held.mutex() != &mu_ || !held.owns_lock()) {
    throw std::logic_error("ReadBudget: caller does not hold the budget lock");
  }
}

size_t ReadBudget::reserve(const size_t* lens, size_t n, bool block,
                           uint64_t* start_ns) {
  std::unique_lock<std::mutex> lk(mu_);
  return reserve_locked(lk, lens, n, block, start_ns);
}

size_t ReadBudget::reserve_locked(std::unique_lock<std::mutex>& held,
                                  const size_t* lens, size_t n, bool block,
                                  uint64_t* start_ns) {
  check_held(held);
  if (n == 0) {
    return 0;
  }
  if (block && !admits(lens[0])) {
    ++waiters_;
    cv_.wait(held, [this, lens] { return admits(lens[0]); });
    --waiters_;
  }
  // Counted before anything changes, so a failed registry insert leaves
  // the budget as it was.
  size_t admitted = 0;
  size_t bytes = bytes_in_flight_;
  size_t reads = reads_in_flight_;
  while (admitted < n && (reads < kMinReadsInFlight ||
                          bytes + lens[admitted] <= max_bytes_)) {
    bytes += lens[admitted];
    ++reads;
    ++admitted;
  }
  if (admitted == 0) {
    return 0;
  }
  const uint64_t now = monotonic_ns();
  start_counts_[now] += admitted;
  bytes_in_flight_ = bytes;
  reads_in_flight_ = reads;
  for (size_t i = 0; i < admitted; ++i) {
    start_ns[i] = now;
  }
  if (bytes_in_flight_ > peak_bytes_) {
    peak_bytes_ = bytes_in_flight_;
  }
  return admitted;
}

void ReadBudget::release(size_t bytes, size_t reads, const uint64_t* start_ns,
                         size_t n) {
  std::unique_lock<std::mutex> lk(mu_);
  release_locked(lk, bytes, reads, start_ns, n);
}

void ReadBudget::release_locked(std::unique_lock<std::mutex>& held,
                                size_t bytes, size_t reads,
                                const uint64_t* start_ns, size_t n) {
  check_held(held);
  if (bytes > bytes_in_flight_ || reads > reads_in_flight_) {
    throw std::logic_error("ReadBudget::release returns more than reserved");
  }
  for (size_t i = 0; i < n; ++i) {
    const auto it = start_counts_.find(start_ns[i]);
    if (it == start_counts_.end()) {
      throw std::logic_error("ReadBudget::release of an unregistered read");
    }
    if (--it->second == 0) {
      start_counts_.erase(it);
    }
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
  return start_counts_.empty() ? UINT64_MAX : start_counts_.begin()->first;
}

size_t ReadBudget::max_bytes() const { return max_bytes_; }

size_t ReadBudget::peak_bytes() const {
  std::lock_guard<std::mutex> lk(mu_);
  return peak_bytes_;
}

}  // namespace connector
}  // namespace lmcache
