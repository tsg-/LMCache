// SPDX-License-Identifier: Apache-2.0
// Test-only fault hooks for the fs_native read engines.  Everything here is
// compiled only with -DLMCACHE_FS_FAULT_INJECTION=1; release builds carry no
// trace of it.
#pragma once

#ifdef LMCACHE_FS_FAULT_INJECTION

  #include <cstdlib>
  #include <cstring>

namespace lmcache {
namespace connector {

// Selected by LMCACHE_FS_FAULT, read when a connector (or ring) is built:
//   submit_partial      io_uring_submit hands the kernel one SQE at a time.
//   submit_error        the first io_uring_submit made while requests are
//                       accepted returns -EIO, once per ring.
//   cqe_short           a read CQE reports half its bytes (block-rounded
//                       under O_DIRECT), so the rest is resubmitted.
//   cqe_eintr           the first CQE of each read reports -EINTR.
//   wait_error_forever  every CQE wait returns -EIO.
//   queue_init_error    io_uring_queue_init returns -EPERM.
enum class FsFault {
  kNone,
  kSubmitPartial,
  kSubmitError,
  kCqeShort,
  kCqeEintr,
  kWaitErrorForever,
  kQueueInitError,
};

inline FsFault fs_fault_from_env() {
  const char* v = std::getenv("LMCACHE_FS_FAULT");
  if (v == nullptr) return FsFault::kNone;
  if (std::strcmp(v, "submit_partial") == 0) return FsFault::kSubmitPartial;
  if (std::strcmp(v, "submit_error") == 0) return FsFault::kSubmitError;
  if (std::strcmp(v, "cqe_short") == 0) return FsFault::kCqeShort;
  if (std::strcmp(v, "cqe_eintr") == 0) return FsFault::kCqeEintr;
  if (std::strcmp(v, "wait_error_forever") == 0) {
    return FsFault::kWaitErrorForever;
  }
  if (std::strcmp(v, "queue_init_error") == 0) return FsFault::kQueueInitError;
  return FsFault::kNone;
}

// LMCACHE_FS_FAULT_DELAY_MS: milliseconds slept before each io_uring CQE is
// processed and before each synchronous do_single_get read; 0 when unset.
inline unsigned fs_fault_delay_ms_from_env() {
  const char* v = std::getenv("LMCACHE_FS_FAULT_DELAY_MS");
  return v == nullptr ? 0u
                      : static_cast<unsigned>(std::strtoul(v, nullptr, 10));
}

}  // namespace connector
}  // namespace lmcache

#endif  // LMCACHE_FS_FAULT_INJECTION
