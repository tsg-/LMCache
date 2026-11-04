// SPDX-License-Identifier: Apache-2.0
#pragma once

#include "connector_base.h"
#include "connector_interface.h"
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <string>
#include <vector>

namespace py = pybind11;

namespace lmcache {
namespace connector {
namespace pybind_utils {

/*
these utilities:
1. convert between cpp and python types
2. release GIL immediately after extracting buffer pointers

example usage (see `redis/pybind.cpp`):
  py::class_<MyConnector>(m, "MyConnectorClient")
      .def(py::init<...>(), ...)
      LMCACHE_BIND_CONNECTOR_METHODS(MyConnector);
*/

// Docstrings for the raw submit methods (bound by the macro below).
#define LMCACHE_RAW_DOC_BODY(VERB, SIBLING)                      \
  "Submit a batch " VERB                                         \
  " built from raw key fields.\n\n"                              \
  "The batch buffer size is buf_lens[0], as in " SIBLING ".\n\n"
constexpr const char* kRawGetDoc = LMCACHE_RAW_DOC_BODY(
    "load", "submit_batch_get") R"DOC(Arguments:
  model_name, cache_salt: str shared by every key.
  tags_token: str, "" or the token from key_codec.encode_tags.
  kv_ranks, object_group_ids, buf_ptrs, buf_lens: one-dimensional
    C-contiguous buffers of unsigned 64-bit items (format "Q" or "=Q",
    itemsize 8), all of the same non-zero length n.
  chunk_hashes: bytes, n hashes of hash_len bytes each, concatenated.
  hash_len: int > 0 (a value at or above 2^63 raises TypeError from
    argument conversion).
Memory safety: each buf_ptrs[i] must address live memory of at least
  buf_lens[i] bytes, writable, from this call until its completion is drained
  (or the client is closed). The binding dereferences these integers
  without any check, so a bad pointer corrupts the process.
Returns:
  The future id (int), as submit_batch_get.
Raises:
  TypeError: hash_len is at or above 2^63.
  ValueError: an array has the wrong format, rank or layout, the lengths
    disagree, n or hash_len is not positive, or model_name, cache_salt or
    tags_token contains "@".
Trusted input: callers pass fields taken from validated ObjectKeyIdentity
  objects and a token from encode_tags. Salt length and tag contents are
  not re-validated here.
The key strings are byte-identical to key_codec.encode_storage_key.)DOC";
constexpr const char* kRawSetDoc = LMCACHE_RAW_DOC_BODY(
    "store", "submit_batch_set") R"DOC(Arguments:
  model_name, cache_salt: str shared by every key.
  tags_token: str, "" or the token from key_codec.encode_tags.
  kv_ranks, object_group_ids, buf_ptrs, buf_lens: one-dimensional
    C-contiguous buffers of unsigned 64-bit items (format "Q" or "=Q",
    itemsize 8), all of the same non-zero length n.
  chunk_hashes: bytes, n hashes of hash_len bytes each, concatenated.
  hash_len: int > 0 (a value at or above 2^63 raises TypeError from
    argument conversion).
Memory safety: each buf_ptrs[i] must address live memory of at least
  buf_lens[i] bytes from this call until its completion is drained
  (or the client is closed). The binding dereferences these integers
  without any check, so a bad pointer corrupts the process.
Returns:
  The future id (int), as submit_batch_set.
Raises:
  TypeError: hash_len is at or above 2^63.
  ValueError: an array has the wrong format, rank or layout, the lengths
    disagree, n or hash_len is not positive, or model_name, cache_salt or
    tags_token contains "@".
Trusted input: callers pass fields taken from validated ObjectKeyIdentity
  objects and a token from encode_tags. Salt length and tag contents are
  not re-validated here.
The key strings are byte-identical to key_codec.encode_storage_key.)DOC";
#undef LMCACHE_RAW_DOC_BODY

#define LMCACHE_BIND_CONNECTOR_METHODS(ConnectorType)                  \
  .def("event_fd", &ConnectorType::event_fd)                           \
      .def("submit_batch_get",                                         \
           lmcache::connector::pybind_utils::bind_submit_batch_get<    \
               ConnectorType>(),                                       \
           py::arg("keys"), py::arg("memoryviews"))                    \
      .def("submit_batch_set",                                         \
           lmcache::connector::pybind_utils::bind_submit_batch_set<    \
               ConnectorType>(),                                       \
           py::arg("keys"), py::arg("memoryviews"))                    \
      .def("submit_batch_get_raw",                                     \
           lmcache::connector::pybind_utils::bind_submit_batch_get_raw<    \
               ConnectorType>(),                                           \
           lmcache::connector::pybind_utils::kRawGetDoc,                   \
           py::arg("model_name"), py::arg("cache_salt"),                   \
           py::arg("tags_token"), py::arg("kv_ranks"),                     \
           py::arg("object_group_ids"), py::arg("chunk_hashes"),           \
           py::arg("hash_len"), py::arg("buf_ptrs"), py::arg("buf_lens"))  \
      .def("submit_batch_set_raw",                                     \
           lmcache::connector::pybind_utils::bind_submit_batch_set_raw<    \
               ConnectorType>(),                                           \
           lmcache::connector::pybind_utils::kRawSetDoc,                   \
           py::arg("model_name"), py::arg("cache_salt"),                   \
           py::arg("tags_token"), py::arg("kv_ranks"),                     \
           py::arg("object_group_ids"), py::arg("chunk_hashes"),           \
           py::arg("hash_len"), py::arg("buf_ptrs"), py::arg("buf_lens"))  \
      .def("submit_batch_exists",                                      \
           lmcache::connector::pybind_utils::bind_submit_batch_exists< \
               ConnectorType>(),                                       \
           py::arg("keys"))                                            \
      .def("submit_batch_delete",                                      \
           lmcache::connector::pybind_utils::bind_submit_batch_delete< \
               ConnectorType>(),                                       \
           py::arg("keys"))                                            \
      .def("drain_completions",                                        \
           lmcache::connector::pybind_utils::bind_drain_completions<   \
               ConnectorType>())                                       \
      .def("close", &ConnectorType::close)

template <typename ConnectorType>
auto bind_submit_batch_get() {
  return [](ConnectorType& self, const std::vector<std::string>& keys,
            py::list memviews) {
    if (keys.size() != memviews.size()) {
      throw std::runtime_error("keys and memviews size mismatch");
    }
    if (keys.empty()) {
      throw std::runtime_error("keys list is empty");
    }

    // extract all buffer info under GIL
    std::vector<void*> bufs;
    std::vector<size_t> lens;
    bufs.reserve(keys.size());
    lens.reserve(keys.size());

    for (size_t i = 0; i < keys.size(); ++i) {
      py::memoryview mv = memviews[i].cast<py::memoryview>();
      py::buffer_info info = py::buffer(mv).request();
      bufs.push_back(info.ptr);
      lens.push_back(static_cast<size_t>(info.size));
    }

    // use the first buffer's size as batch_chunk_num_bytes (all must match)
    size_t batch_chunk_num_bytes = lens[0];

    py::gil_scoped_release release;
    return self.submit_batch_get(keys, bufs, lens, batch_chunk_num_bytes);
  };
}

template <typename ConnectorType>
auto bind_submit_batch_set() {
  return [](ConnectorType& self, const std::vector<std::string>& keys,
            py::list memviews) {
    if (keys.size() != memviews.size()) {
      throw std::runtime_error("keys and memviews size mismatch");
    }
    if (keys.empty()) {
      throw std::runtime_error("keys list is empty");
    }

    // extract all buffer info under GIL
    std::vector<void*> bufs;
    std::vector<size_t> lens;
    bufs.reserve(keys.size());
    lens.reserve(keys.size());

    for (size_t i = 0; i < keys.size(); ++i) {
      py::memoryview mv = memviews[i].cast<py::memoryview>();
      py::buffer_info info = py::buffer(mv).request();
      bufs.push_back(info.ptr);
      lens.push_back(static_cast<size_t>(info.size));
    }

    // use the first buffer's size as batch_chunk_num_bytes (all must match)
    size_t batch_chunk_num_bytes = lens[0];

    py::gil_scoped_release release;
    return self.submit_batch_set(keys, bufs, lens, batch_chunk_num_bytes);
  };
}

// Copies of the raw submit arguments, taken while holding the GIL.
struct RawBatchArgs {
  std::string model;
  std::string salt;
  std::string token;
  std::vector<uint64_t> ranks;
  std::vector<uint64_t> groups;
  std::vector<uint8_t> hashes;
  size_t hash_len = 0;
  std::vector<void*> bufs;
  std::vector<size_t> lens;
};

// Copy a 1-D C-contiguous unsigned 64-bit buffer ("Q" or "=Q"). memcpy
// keeps this safe for buffers that are not 8-byte aligned.
inline std::vector<uint64_t> copy_u64_buffer(const py::buffer& buf,
                                             const char* name) {
  py::buffer_info info = buf.request();
  if (info.ndim != 1 || info.itemsize != 8 ||
      (info.format != "Q" && info.format != "=Q")) {
    throw py::value_error(std::string(name) +
                          " must be a 1-D buffer of unsigned 64-bit items "
                          "(format 'Q' or '=Q')");
  }
  if (info.shape[0] < 0 || (info.shape[0] > 1 && info.strides[0] != 8)) {
    throw py::value_error(std::string(name) + " must be C-contiguous");
  }
  std::vector<uint64_t> out(static_cast<size_t>(info.shape[0]));
  if (!out.empty()) {
    std::memcpy(out.data(), info.ptr, out.size() * sizeof(uint64_t));
  }
  return out;
}

inline void reject_at(const std::string& value, const char* name) {
  if (value.find('@') != std::string::npos) {
    throw py::value_error(std::string(name) + " must not contain '@'");
  }
}

inline RawBatchArgs prepare_raw_batch(
    const std::string& model_name, const std::string& cache_salt,
    const std::string& tags_token, const py::buffer& kv_ranks,
    const py::buffer& object_group_ids, const py::bytes& chunk_hashes,
    long long hash_len, const py::buffer& buf_ptrs,
    const py::buffer& buf_lens) {
  reject_at(model_name, "model_name");
  reject_at(cache_salt, "cache_salt");
  reject_at(tags_token, "tags_token");

  RawBatchArgs args;
  args.ranks = copy_u64_buffer(kv_ranks, "kv_ranks");
  args.groups = copy_u64_buffer(object_group_ids, "object_group_ids");
  std::vector<uint64_t> ptrs = copy_u64_buffer(buf_ptrs, "buf_ptrs");
  std::vector<uint64_t> byte_lens = copy_u64_buffer(buf_lens, "buf_lens");

  const size_t n = args.ranks.size();
  if (n == 0) {
    throw py::value_error("raw batch is empty");
  }
  if (args.groups.size() != n || ptrs.size() != n || byte_lens.size() != n) {
    throw py::value_error("raw batch arrays must have the same length");
  }
  if (hash_len <= 0) {
    throw py::value_error("hash_len must be > 0");
  }
  if (static_cast<unsigned long long>(hash_len) >
      std::numeric_limits<size_t>::max() / n) {
    throw py::value_error("hash_len * n overflows");
  }
  args.hash_len = static_cast<size_t>(hash_len);

  char* hash_data = nullptr;
  Py_ssize_t hash_bytes = 0;
  if (PyBytes_AsStringAndSize(chunk_hashes.ptr(), &hash_data, &hash_bytes) !=
      0) {
    throw py::error_already_set();
  }
  if (static_cast<size_t>(hash_bytes) != args.hash_len * n) {
    throw py::value_error("len(chunk_hashes) must equal hash_len * n");
  }
  args.hashes.assign(reinterpret_cast<const uint8_t*>(hash_data),
                     reinterpret_cast<const uint8_t*>(hash_data) + hash_bytes);

  args.bufs.reserve(n);
  args.lens.reserve(n);
  for (size_t i = 0; i < n; ++i) {
    if (ptrs[i] >
        static_cast<uint64_t>(std::numeric_limits<uintptr_t>::max())) {
      throw py::value_error("buf_ptrs entry does not fit a pointer");
    }
    if (byte_lens[i] >
        static_cast<uint64_t>(std::numeric_limits<size_t>::max())) {
      throw py::value_error("buf_lens entry does not fit size_t");
    }
    args.bufs.push_back(
        reinterpret_cast<void*>(static_cast<uintptr_t>(ptrs[i])));
    args.lens.push_back(static_cast<size_t>(byte_lens[i]));
  }
  args.model = model_name;
  args.salt = cache_salt;
  args.token = tags_token;
  return args;
}

// Append `value` as lowercase hex, left-padded with zeros to `min_digits`.
inline void append_hex(std::string& out, uint64_t value, size_t min_digits) {
  static const char kDigits[] = "0123456789abcdef";
  char tmp[16];
  size_t count = 0;
  do {
    tmp[count++] = kDigits[value & 0xf];
    value >>= 4;
  } while (value != 0);
  for (size_t i = count; i < min_digits; ++i) {
    out.push_back('0');
  }
  while (count > 0) {
    out.push_back(tmp[--count]);
  }
}

// Build the same strings as key_codec.encode_storage_key:
//   {model}@{rank:08x}@{group:x}@{hash hex}[@{salt}[@{token}]]
// The salt is kept when the token is set, even if empty.
inline std::vector<std::string> build_raw_keys(
    const std::string& model, const std::string& salt,
    const std::string& token, const std::vector<uint64_t>& ranks,
    const std::vector<uint64_t>& groups, const std::vector<uint8_t>& hashes,
    size_t hash_len) {
  const size_t n = ranks.size();
  const size_t capacity =
      model.size() + 3 + 16 + 16 + 2 * hash_len + 2 + salt.size() +
      token.size();
  std::vector<std::string> keys;
  keys.reserve(n);
  for (size_t i = 0; i < n; ++i) {
    std::string key;
    key.reserve(capacity);
    key += model;
    key.push_back('@');
    append_hex(key, ranks[i], 8);
    key.push_back('@');
    append_hex(key, groups[i], 1);
    key.push_back('@');
    const uint8_t* hash = hashes.data() + i * hash_len;
    for (size_t j = 0; j < hash_len; ++j) {
      append_hex(key, hash[j], 2);
    }
    if (!token.empty()) {
      key.push_back('@');
      key += salt;
      key.push_back('@');
      key += token;
    } else if (!salt.empty()) {
      key.push_back('@');
      key += salt;
    }
    keys.push_back(std::move(key));
  }
  return keys;
}

template <typename ConnectorType>
auto bind_submit_batch_get_raw() {
  return [](ConnectorType& self, const std::string& model_name,
            const std::string& cache_salt, const std::string& tags_token,
            const py::buffer& kv_ranks, const py::buffer& object_group_ids,
            const py::bytes& chunk_hashes, long long hash_len,
            const py::buffer& buf_ptrs, const py::buffer& buf_lens) {
    RawBatchArgs args = prepare_raw_batch(
        model_name, cache_salt, tags_token, kv_ranks, object_group_ids,
        chunk_hashes, hash_len, buf_ptrs, buf_lens);
    py::gil_scoped_release release;
    std::vector<std::string> keys =
        build_raw_keys(args.model, args.salt, args.token, args.ranks,
                       args.groups, args.hashes, args.hash_len);
    return self.submit_batch_get(keys, args.bufs, args.lens, args.lens[0]);
  };
}

template <typename ConnectorType>
auto bind_submit_batch_set_raw() {
  return [](ConnectorType& self, const std::string& model_name,
            const std::string& cache_salt, const std::string& tags_token,
            const py::buffer& kv_ranks, const py::buffer& object_group_ids,
            const py::bytes& chunk_hashes, long long hash_len,
            const py::buffer& buf_ptrs, const py::buffer& buf_lens) {
    RawBatchArgs args = prepare_raw_batch(
        model_name, cache_salt, tags_token, kv_ranks, object_group_ids,
        chunk_hashes, hash_len, buf_ptrs, buf_lens);
    py::gil_scoped_release release;
    std::vector<std::string> keys =
        build_raw_keys(args.model, args.salt, args.token, args.ranks,
                       args.groups, args.hashes, args.hash_len);
    return self.submit_batch_set(keys, args.bufs, args.lens, args.lens[0]);
  };
}

template <typename ConnectorType>
auto bind_submit_batch_exists() {
  return [](ConnectorType& self, const std::vector<std::string>& keys) {
    py::gil_scoped_release release;
    return self.submit_batch_exists(keys);
  };
}

template <typename ConnectorType>
auto bind_submit_batch_delete() {
  return [](ConnectorType& self, const std::vector<std::string>& keys) {
    py::gil_scoped_release release;
    return self.submit_batch_delete(keys);
  };
}

template <typename ConnectorType>
auto bind_drain_completions() {
  return [](ConnectorType& self) {
    // call cpp method without holding GIL
    std::vector<Completion> completions;
    {
      py::gil_scoped_release release;
      completions = self.drain_completions();
    }

    // convert results to python objects (requires GIL)
    py::list result;
    for (auto& c : completions) {
      // convert result_bytes to python list of bools if not empty
      py::object bools_obj = py::none();
      if (!c.result_bytes.empty()) {
        py::list bools_list;
        for (uint8_t b : c.result_bytes) {
          bools_list.append(bool(b));
        }
        bools_obj = bools_list;
      }

      // return tuple: (future_id, ok, error, result_bools)
      result.append(py::make_tuple(c.future_id, c.ok, c.error, bools_obj));
    }

    return result;
  };
}
inline WorkerPoolConfig parse_per_op_workers(const py::object& obj) {
  if (obj.is_none()) {
    return {};
  }
  py::dict d = obj.cast<py::dict>();
  std::unordered_map<std::string, int> per_op;
  for (auto& [key, value] : d) {
    std::string key_str = key.cast<std::string>();
    int count = value.cast<int>();
    if (count <= 0) {
      throw std::runtime_error("per_op_workers['" + key_str +
                               "'] must be a positive integer");
    }
    per_op[key_str] = count;
  }
  return {std::move(per_op)};
}

}  // namespace pybind_utils
}  // namespace connector
}  // namespace lmcache
