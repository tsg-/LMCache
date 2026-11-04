#!/bin/bash
# Build lmcache_fs in place on a rig host, statically linking a PIC liburing.
#
# Usage: build_lmcache_fs.sh <lmcache-tree>
#   PY=<python>            interpreter (default <tree>/.venv-bench-l2/bin/python)
#   NO_LIBURING=1          skip liburing; the io_uring engine is compiled out
#   LMCACHE_FS_FAULT_INJECTION=1  adds -DLMCACHE_FS_FAULT_INJECTION=1
set -euo pipefail

TREE=${1:?usage: build_lmcache_fs.sh <lmcache-tree>}
TREE=$(cd "$TREE" && pwd)
PY=${PY:-$TREE/.venv-bench-l2/bin/python}
LIBURING_VERSION=2.12
LIBURING_URL=https://github.com/axboe/liburing/archive/refs/tags/liburing-${LIBURING_VERSION}.tar.gz
PREFIX=$TREE/build/liburing

DEFS=()
LIBS=()
INCS=()
if [ "${NO_LIBURING:-0}" != 1 ]; then
  if [ ! -f "$PREFIX/lib/liburing.a" ]; then
    mkdir -p "$PREFIX"
    SRC=$(mktemp -d)
    trap 'rm -rf "$SRC"' EXIT
    curl -fsSL "$LIBURING_URL" | tar -xz -C "$SRC" --strip-components=1
    (cd "$SRC" && CFLAGS=-fPIC ./configure --prefix="$PREFIX" >/dev/null &&
      make -j"$(nproc)" -C src >/dev/null && make -C src install >/dev/null)
  fi
  # liburing >= 2.2 is required (io_uring_sqe_set_data64).
  if ! printf '#include <liburing.h>\nvoid p(struct io_uring_sqe *s){io_uring_sqe_set_data64(s,1);}\n' |
    cc -x c -fsyntax-only -I"$PREFIX/include" - 2>/dev/null; then
    echo "error: $PREFIX/include/liburing.h is older than liburing 2.2; remove $PREFIX and rerun" >&2
    exit 1
  fi
  DEFS+=(-DLMCACHE_HAVE_LIBURING=1)
  INCS+=(-I"$PREFIX/include")
  LIBS+=("$PREFIX/lib/liburing.a")
fi
if [ "${LMCACHE_FS_FAULT_INJECTION:-0}" = 1 ]; then
  DEFS+=(-DLMCACHE_FS_FAULT_INJECTION=1)
fi

PYBIND_INC=$("$PY" -c 'import pybind11; print(pybind11.get_include())' 2>/dev/null ||
  "$PY" -c 'import os, torch; print(os.path.join(os.path.dirname(torch.__file__), "include"))')
PY_INC=$("$PY" -c 'import sysconfig; print(sysconfig.get_paths()["include"])')
EXT=$("$PY" -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')
OUT=$TREE/lmcache/lmcache_fs$EXT

cd "$TREE"
c++ -O3 -std=c++17 -shared -fPIC "${DEFS[@]}" \
  -I"$PYBIND_INC" -I"$PY_INC" "${INCS[@]}" \
  -Icsrc/storage_backends -Icsrc/storage_backends/fs \
  csrc/storage_backends/fs/pybind.cpp csrc/storage_backends/fs/connector.cpp \
  "${LIBS[@]}" -o "$OUT"
echo "built $OUT (liburing: $([ "${NO_LIBURING:-0}" = 1 ] && echo off || echo "$PREFIX"))"
