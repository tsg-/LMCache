// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <map>
#include <mutex>

namespace lmcache {
namespace connector {

// A thread blocked on a full budget is woken once 1/kReadBudgetWakeSlices of
// it is free.
static constexpr size_t kReadBudgetWakeSlices = 8;
// Reads always admitted whatever the budget, so an object larger than half
// of it still overlaps with the next one instead of draining the device
// between them: one 1 GiB object alone reads ~40 GB/s on an array that
// two in flight bring to ~49.
static constexpr size_t kMinReadsInFlight = 2;

// Bytes and reads in flight against the device, connector-wide, and the
// monotonic start time of every read behind them.  Every read reserves here
// before it opens its file and releases after it finishes, so the start
// times also tell a recycling store whether a parked inode may still be
// read.  None of the methods calls out while holding the lock.
//
// Two locking modes.  Built with only max_bytes, the budget owns its mutex
// and reserve()/release() take it.  Built with a caller's mutex, the budget
// state is guarded by that mutex, so a caller already holding it for its
// own state uses reserve_locked()/release_locked() and pays one acquisition
// for both; reserve()/release() still work and take it themselves.
class ReadBudget {
 public:
  // max_bytes: bytes allowed in flight; reads beyond it are admitted only
  //   while fewer than kMinReadsInFlight are in flight.
  explicit ReadBudget(size_t max_bytes);
  // As above, with the budget state guarded by `mu`, which must outlive
  // the budget.
  ReadBudget(size_t max_bytes, std::mutex& mu);

  ReadBudget(const ReadBudget&) = delete;
  ReadBudget& operator=(const ReadBudget&) = delete;

  // Admits the longest prefix of lens[0..n) that the budget allows, in
  // order, stopping at the first read it does not admit.  A read of len
  // bytes is admitted while fewer than kMinReadsInFlight reads are in
  // flight or its bytes fit.  With block true and n > 0, first waits until
  // lens[0] is admitted, so the result is at least 1; with block false it
  // never waits and may return 0.  Writes one monotonic start time
  // (steady_clock, ns) per admitted read to start_ns[0..result), which the
  // caller passes back to release().  One mutex acquisition.
  //
  // Returns the number of reads admitted.
  // Raises std::bad_alloc if the start time cannot be recorded; nothing is
  // then admitted by this call.
  size_t reserve(const size_t* lens, size_t n, bool block, uint64_t* start_ns);

  // reserve() for a caller that already holds the budget's mutex through
  // `held`; a blocking wait releases and retakes it.
  //
  // Raises std::logic_error if `held` does not own the budget's mutex.
  size_t reserve_locked(std::unique_lock<std::mutex>& held, const size_t* lens,
                        size_t n, bool block, uint64_t* start_ns);

  // Returns `bytes` and `reads` from earlier reservations and forgets the
  // n start times in start_ns, then wakes blocked reservers: only once a
  // 1/kReadBudgetWakeSlices slice of the budget is free or fewer than
  // kMinReadsInFlight reads are in flight, so a blocked reserver wakes once
  // per slice, not once per finished read.  One mutex acquisition.
  //
  // Raises std::logic_error if more bytes or reads are returned than are in
  // flight (nothing changed), or if a start time is not registered (the
  // start times before it are already forgotten).
  void release(size_t bytes, size_t reads, const uint64_t* start_ns, size_t n);

  // release() for a caller that already holds the budget's mutex through
  // `held`.
  //
  // Raises std::logic_error as release() does, or if `held` does not own
  // the budget's mutex.
  void release_locked(std::unique_lock<std::mutex>& held, size_t bytes,
                      size_t reads, const uint64_t* start_ns, size_t n);

  // The earliest start time still in flight, or UINT64_MAX when none is.
  uint64_t oldest_start_ns() const;

  // The configured budget.
  size_t max_bytes() const;

  // The most bytes ever in flight at once.
  size_t peak_bytes() const;

 private:
  // Requires mu_.
  bool admits(size_t len) const;
  void check_held(const std::unique_lock<std::mutex>& held) const;

  const size_t max_bytes_;
  std::mutex own_mu_;  // unused when a caller's mutex is given
  std::mutex& mu_;
  std::condition_variable cv_;
  size_t waiters_ = 0;
  size_t bytes_in_flight_ = 0;
  size_t reads_in_flight_ = 0;
  size_t peak_bytes_ = 0;
  // Start time -> reads in flight that started then; one entry per
  // reserve() run, not per read.
  std::map<uint64_t, size_t> start_counts_;
};

}  // namespace connector
}  // namespace lmcache
