// SPDX-License-Identifier: Apache-2.0
#include <pybind11/pybind11.h>
#include "../connector_pybind_utils.h"
#include "connector.h"

namespace py = pybind11;

PYBIND11_MODULE(lmcache_fs, m) {
  m.def(
      "has_io_uring",
      []() {
#ifdef LMCACHE_HAVE_LIBURING
        return true;
#else
        return false;
#endif
      },
      "True when the extension was built with the io_uring read engine.");
  m.def(
      "has_fault_injection",
      []() {
#ifdef LMCACHE_FS_FAULT_INJECTION
        return true;
#else
        return false;
#endif
      },
      "True when the extension was built with LMCACHE_FS_FAULT_INJECTION, "
      "so LMCACHE_FS_FAULT and LMCACHE_FS_FAULT_DELAY_MS take effect.");
  py::class_<lmcache::connector::FSConnector>(m, "LMCacheFSClient")
      .def(py::init<std::string, int, std::string, bool, size_t, int, size_t,
                    size_t, size_t, int, int, std::string, unsigned>(),
           py::arg("base_path"), py::arg("num_workers"),
           py::arg("relative_tmp_dir") = "", py::arg("use_odirect") = false,
           py::arg("read_ahead_size") = 0, py::arg("num_read_workers") = 0,
           py::arg("read_max_bytes_in_flight") = 0,
           py::arg("num_shard_dirs") = 0, py::arg("recycle_max_files") = 0,
           py::arg("store_workers") = 0, py::arg("delete_workers") = 0,
           py::arg("read_engine") = "pool", py::arg("io_depth") = 64)
      .def("read_budget_bytes",
           &lmcache::connector::FSConnector::read_budget_bytes,
           "Bytes allowed outstanding against the device; 0 on the legacy "
           "path, which has no budget.")
      .def("read_peak_bytes", &lmcache::connector::FSConnector::read_peak_bytes,
           "Most bytes the read engine has had in flight at once; 0 on the "
           "legacy path.")
      .def("read_engine", &lmcache::connector::FSConnector::read_engine,
           "The read engine in use: 'io_uring', 'pool' or 'legacy'.")
      .def("io_depth", &lmcache::connector::FSConnector::io_depth,
           "Reads in flight per io_uring ring, after any lowering for "
           "RLIMIT_NOFILE; 0 when the io_uring engine is not in use.")
          LMCACHE_BIND_CONNECTOR_METHODS(lmcache::connector::FSConnector);
}
