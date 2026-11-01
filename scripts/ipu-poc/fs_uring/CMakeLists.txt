cmake_minimum_required(VERSION 3.20)
project(lmcache_fs_uring LANGUAGES CXX)
option(LMCACHE_TEST_FAULTS "Compile the isolated liburing fault-test variant" OFF)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(Python COMPONENTS Interpreter Development.Module REQUIRED)
find_package(pybind11 CONFIG REQUIRED)
find_package(Threads REQUIRED)
find_package(PkgConfig REQUIRED)
pkg_check_modules(URING REQUIRED IMPORTED_TARGET liburing)

pybind11_add_module(lmcache_fs MODULE vendor/lmcache/fs/connector.cpp
                    vendor/lmcache/fs/pybind.cpp)
target_link_libraries(lmcache_fs PRIVATE Threads::Threads)
target_include_directories(lmcache_fs PRIVATE vendor/lmcache)

if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/src/connector.cpp")
  pybind11_add_module(lmcache_fs_uring MODULE src/connector.cpp src/pybind.cpp)
  target_include_directories(lmcache_fs_uring PRIVATE vendor/lmcache src)
  target_link_libraries(lmcache_fs_uring PRIVATE Threads::Threads
                        PkgConfig::URING)
  target_compile_options(lmcache_fs_uring PRIVATE -Wall -Wextra -Werror)
  if(LMCACHE_TEST_FAULTS)
    target_compile_options(lmcache_fs_uring PRIVATE
      -include "${CMAKE_CURRENT_SOURCE_DIR}/tests/fault_api.h")
  endif()
endif()

if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/lmcache-runtime/csrc/lmcache_native")
  set(NATIVE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/lmcache-runtime/csrc")
  pybind11_add_module(lmcache_native MODULE
    "${NATIVE_DIR}/lmcache_native/bitmap.cpp"
    "${NATIVE_DIR}/lmcache_native/fold.cpp"
    "${NATIVE_DIR}/lmcache_native/periodic_event_notifier.cpp"
    "${NATIVE_DIR}/lmcache_native/pybind.cpp"
    "${NATIVE_DIR}/lmcache_native/ttl_lock.cpp"
    "${NATIVE_DIR}/lmcache_native/utils.cpp")
  target_include_directories(lmcache_native PRIVATE
    "${NATIVE_DIR}" "${NATIVE_DIR}/lmcache_native")
  target_link_libraries(lmcache_native PRIVATE Threads::Threads)
  set_target_properties(lmcache_native PROPERTIES LIBRARY_OUTPUT_DIRECTORY
    "${CMAKE_CURRENT_SOURCE_DIR}/lmcache-runtime/lmcache")
endif()
