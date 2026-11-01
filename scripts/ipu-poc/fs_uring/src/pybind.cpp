// SPDX-License-Identifier: Apache-2.0
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include "connector.h"
#include "connector_pybind_utils.h"

namespace py = pybind11;
using lmcache::connector::FSUringConnector;

namespace {
uint64_t submit_buffers(FSUringConnector& self,
                        const std::vector<std::string>& keys,
                        const py::list& views, bool store) {
  if (keys.empty() || keys.size() != views.size()) {
    throw std::runtime_error("nonempty keys and buffers must have equal size");
  }
  std::vector<void*> bufs;
  std::vector<size_t> lens;
  for (const auto& value : views) {
    auto view = py::cast<py::memoryview>(value);
    auto info = py::buffer(view).request(!store);
    if (info.itemsize != 1 || info.ndim != 1 || info.strides[0] != 1) {
      throw std::runtime_error("buffers must be contiguous byte memoryviews");
    }
    bufs.push_back(info.ptr);
    lens.push_back(static_cast<size_t>(info.size));
  }
  py::gil_scoped_release release;
  return store ? self.submit_batch_set(keys, bufs, lens, lens[0])
               : self.submit_batch_get(keys, bufs, lens, lens[0]);
}
}  // namespace

PYBIND11_MODULE(lmcache_fs_uring, m) {
  py::class_<FSUringConnector>(m, "LMCacheFSUringClient")
      .def(py::init<std::string, int, std::string, bool, unsigned, size_t,
                    size_t, int, int>(),
           py::arg("base_path"), py::arg("num_workers"),
           py::arg("relative_tmp_dir") = "", py::arg("use_odirect") = true,
           py::arg("io_depth") = 32, py::arg("num_shard_dirs") = 0,
           py::arg("recycle_max_files") = 0, py::arg("store_workers") = 0,
           py::arg("delete_workers") = 0)
      .def("io_stats",
           [](const FSUringConnector& self) {
             const auto stats = self.io_stats();
             py::dict result;
             result["submitted_reads"] = stats.submitted_reads;
             result["completed_reads"] = stats.completed_reads;
             result["pending_reads"] = stats.pending_reads;
             result["max_worker_pending"] = stats.max_worker_pending;
             result["max_total_pending"] = stats.max_total_pending;
             return result;
           })
      .def("event_fd", &FSUringConnector::event_fd)
      .def("submit_batch_get",
           [](FSUringConnector& self, const std::vector<std::string>& keys,
              const py::list& views) {
             return submit_buffers(self, keys, views, false);
           })
      .def("submit_batch_set",
           [](FSUringConnector& self, const std::vector<std::string>& keys,
              const py::list& views) {
             return submit_buffers(self, keys, views, true);
           })
      .def("submit_batch_exists",
           lmcache::connector::pybind_utils::bind_submit_batch_exists<
               FSUringConnector>())
      .def("submit_batch_delete",
           lmcache::connector::pybind_utils::bind_submit_batch_delete<
               FSUringConnector>())
      .def("drain_completions",
           lmcache::connector::pybind_utils::bind_drain_completions<
               FSUringConnector>())
      .def("close", &FSUringConnector::close,
           py::call_guard<py::gil_scoped_release>());
}
