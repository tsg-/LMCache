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
  py::class_<lmcache::connector::FSConnector>(m, "LMCacheFSClient")
      .def(py::init<std::string, int, std::string, bool, size_t, int, size_t,
                    size_t, size_t, int, int>(),
           py::arg("base_path"), py::arg("num_workers"),
           py::arg("relative_tmp_dir") = "", py::arg("use_odirect") = false,
           py::arg("read_ahead_size") = 0, py::arg("num_read_workers") = 0,
           py::arg("read_max_bytes_in_flight") = 0,
           py::arg("num_shard_dirs") = 0, py::arg("recycle_max_files") = 0,
           py::arg("store_workers") = 0, py::arg("delete_workers") = 0)
      .def("read_budget_bytes",
           &lmcache::connector::FSConnector::read_budget_bytes,
           "Bytes allowed outstanding against the device; 0 when there is "
           "no read pool.")
          LMCACHE_BIND_CONNECTOR_METHODS(lmcache::connector::FSConnector);
}
