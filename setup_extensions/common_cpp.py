# SPDX-License-Identifier: Apache-2.0
"""Common C++ extension builders shared by all backends.

These extensions (lmcache_native, Redis, filesystem)
are always compiled regardless of which backend is selected.
"""

# Standard
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).parent.parent

if TYPE_CHECKING:
    # Third Party
    from setuptools.extension import Extension

    # First Party
    from setup_extensions.build_profiles import BuildProfile


@dataclass(frozen=True)
class CommonExtSpec:
    """Declarative spec for a common C++ extension."""

    name: str
    sources: list[str]
    include_dirs: list[str]


COMMON_EXTENSIONS: list[CommonExtSpec] = [
    CommonExtSpec(
        name="lmcache_native",
        sources=[
            "csrc/lmcache_native/bitmap.cpp",
            "csrc/lmcache_native/fold.cpp",
            "csrc/lmcache_native/periodic_event_notifier.cpp",
            "csrc/lmcache_native/pybind.cpp",
            "csrc/lmcache_native/ttl_lock.cpp",
            "csrc/lmcache_native/utils.cpp",
        ],
        include_dirs=["csrc/lmcache_native", "csrc"],
    ),
    CommonExtSpec(
        name="lmcache_redis",
        sources=[
            "csrc/storage_backends/redis/pybind.cpp",
            "csrc/storage_backends/redis/connector.cpp",
        ],
        include_dirs=[
            "csrc/storage_backends",
            "csrc/storage_backends/redis",
        ],
    ),
    CommonExtSpec(
        name="lmcache_fs",
        sources=[
            "csrc/storage_backends/fs/pybind.cpp",
            "csrc/storage_backends/fs/connector.cpp",
        ],
        include_dirs=[
            "csrc/storage_backends",
            "csrc/storage_backends/fs",
        ],
    ),
]


_PROBE_SRC = (
    "#include <liburing.h>\n"
    "void probe(struct io_uring_sqe *sqe) { io_uring_sqe_set_data64(sqe, 1); }\n"
)

_SYSTEM_PREFIXES: list[tuple[str, list[str]]] = [
    (
        "/usr/include",
        ["/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu"]
        + ["/usr/lib64", "/usr/lib"],
    ),
    ("/usr/local/include", ["/usr/local/lib", "/usr/local/lib64"]),
]


def _compiler() -> list[str]:
    """Return the C compiler command from ``$CC`` (default ``cc``).

    Returns:
        The command words, or an empty list when the compiler is not found.
    """
    words = shlex.split(os.environ.get("CC", "cc"))
    if not words or shutil.which(words[0]) is None:
        return []
    return words


def _header_has_set_data64(inc_dir: Path) -> bool:
    """Check that ``liburing.h`` in ``inc_dir`` declares liburing 2.2 APIs.

    Args:
        inc_dir: Directory containing ``liburing.h``.

    Returns:
        True when a probe using ``io_uring_sqe_set_data64`` compiles.
    """
    cc = _compiler()
    if not cc:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "probe.c"
        src.write_text(_PROBE_SRC)
        cmd = [*cc, "-I", str(inc_dir), "-fsyntax-only", str(src)]
        return subprocess.run(cmd, capture_output=True, check=False).returncode == 0


def _archive_is_pic(archive: Path) -> bool:
    """Return True if ``readelf -r`` shows the archive is position-independent.

    Absolute 32-bit relocations (``R_X86_64_32``) mean the archive was built
    without ``-fPIC``. Without ``readelf`` the archive cannot be verified and
    is rejected.

    Args:
        archive: Path to a ``liburing.a``.

    Returns:
        True when readelf ran and no non-PIC relocations are present.
    """
    readelf = shutil.which("readelf")
    if readelf is None:
        logger.warning(
            "lmcache_fs: readelf not found; cannot verify that %s is "
            "position-independent; io_uring engine compiled out",
            archive,
        )
        return False
    proc = subprocess.run(
        [readelf, "-r", str(archive)], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        logger.warning("lmcache_fs: readelf failed on %s, skipping", archive)
        return False
    if re.search(r"R_X86_64_32(?!S)\b|R_X86_64_32S\b", proc.stdout):
        logger.warning("lmcache_fs: %s is not position-independent, skipping", archive)
        return False
    return True


def _liburing_candidates(explicit: str) -> list[tuple[Path, Path]]:
    """List ``(include_dir, archive)`` pairs from one install prefix each.

    Args:
        explicit: Value of ``LMCACHE_LIBURING_DIR`` or an empty string to
            search the system prefixes.

    Returns:
        Candidate pairs in search order.
    """
    if explicit:
        base = Path(explicit)
        return [
            (base / "include", base / lib / "liburing.a") for lib in ("lib", "lib64")
        ]
    return [
        (Path(inc), Path(lib) / "liburing.a")
        for inc, libs in _SYSTEM_PREFIXES
        for lib in libs
    ]


def _find_liburing() -> list[tuple[Path, Path]]:
    """Locate ``liburing.h`` and a PIC ``liburing.a`` of version 2.2 or newer.

    Linking is opt-in via ``LMCACHE_LIBURING_DIR`` (an install prefix with
    ``include/`` and ``lib/``) or ``LMCACHE_WITH_LIBURING=1`` (system
    prefixes). Logs why the engine is compiled out or a candidate was
    rejected.

    Returns:
        A one-element list ``[(include_dir, archive)]``, or an empty list when
        the engine is compiled out.
    """
    if not sys.platform.startswith("linux"):
        logger.warning("lmcache_fs: not Linux; io_uring engine compiled out")
        return []
    explicit = os.environ.get("LMCACHE_LIBURING_DIR", "")
    if not explicit and os.environ.get("LMCACHE_WITH_LIBURING") != "1":
        logger.warning(
            "lmcache_fs: io_uring engine compiled out "
            "(set LMCACHE_WITH_LIBURING=1 or LMCACHE_LIBURING_DIR to enable)"
        )
        return []
    for inc_dir, archive in _liburing_candidates(explicit):
        if not ((inc_dir / "liburing.h").is_file() and archive.is_file()):
            continue
        if not _compiler():
            logger.warning("lmcache_fs: no C compiler to probe %s, skipping", archive)
            continue
        if not _header_has_set_data64(inc_dir):
            logger.warning(
                "lmcache_fs: %s is older than liburing 2.2 "
                "(no io_uring_sqe_set_data64), skipping",
                inc_dir / "liburing.h",
            )
            continue
        if not _archive_is_pic(archive):
            continue
        return [(inc_dir, archive)]
    if explicit:
        logger.warning(
            "lmcache_fs: LMCACHE_LIBURING_DIR=%s has no usable liburing >= 2.2 "
            "(need include/liburing.h and lib/liburing.a); "
            "io_uring engine compiled out",
            explicit,
        )
    else:
        logger.warning(
            "lmcache_fs: no usable liburing >= 2.2 PIC archive found "
            "in system dirs; io_uring engine compiled out"
        )
    return []


def _fs_liburing_build_args() -> tuple[list[str], list[str], list[str]]:
    """Compute liburing-related build inputs for ``lmcache_fs``.

    Returns:
        ``(define_flags, include_dirs, extra_objects)``.
    """
    defines: list[str] = []
    include_dirs: list[str] = []
    objects: list[str] = []
    for inc_dir, archive in _find_liburing():
        defines.append("-DLMCACHE_HAVE_LIBURING=1")
        include_dirs.append(str(inc_dir))
        objects.append(str(archive))
        logger.warning("lmcache_fs: io_uring engine enabled (%s)", archive)
    if os.environ.get("LMCACHE_FS_FAULT_INJECTION") == "1":
        defines.append("-DLMCACHE_FS_FAULT_INJECTION=1")
    return defines, include_dirs, objects


def build_common_cpp(
    profile: "BuildProfile | None" = None,
) -> tuple[list["Extension"], dict]:
    """Build pure C++ extensions that do not depend on any backend.

    Args:
        profile: Resolved backend profile (or ``None``).  Each spec in
            :data:`COMMON_EXTENSIONS` queries ``profile.extra_cxx_flags_for``
            to obtain its per-extension extra flags.

    Returns:
        ``(ext_modules, cmdclass)`` tuple.
    """
    # Third Party
    from torch.utils import cpp_extension

    ext_modules = []
    for spec in COMMON_EXTENSIONS:
        extra_defines: list[str] = []
        extra_includes: list[str] = []
        extra_objects: list[str] = []
        if spec.name == "lmcache_fs":
            extra_defines, extra_includes, extra_objects = _fs_liburing_build_args()
        ext_modules.append(
            cpp_extension.CppExtension(
                "lmcache." + spec.name,
                sources=spec.sources,
                include_dirs=[str(ROOT_DIR / d) for d in spec.include_dirs]
                + extra_includes,
                extra_objects=extra_objects,
                extra_compile_args={
                    "cxx": (
                        (profile.extra_cxx_flags_for(spec) if profile else [])
                        + ["-O3"]
                        + extra_defines
                    ),
                },
            )
        )
    cmdclass = {"build_ext": cpp_extension.BuildExtension}
    return ext_modules, cmdclass
