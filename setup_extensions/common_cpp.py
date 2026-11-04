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


def _compiler() -> str | None:
    """Return a C compiler path, honouring ``$CC``."""
    return shutil.which(os.environ.get("CC", "cc"))


def _probe(inc_dir: Path, archive: Path | None) -> bool:
    """Compile the probe against ``inc_dir``; also link ``-shared`` if given.

    Args:
        inc_dir: Directory containing ``liburing.h``.
        archive: Archive to link into a shared object, or ``None`` to only
            check that the header declares ``io_uring_sqe_set_data64``.

    Returns:
        True when the probe compiles (and links).
    """
    cc = _compiler()
    if cc is None:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "probe.c"
        src.write_text(_PROBE_SRC)
        cmd = [cc, "-I", str(inc_dir)]
        if archive is None:
            cmd += ["-fsyntax-only", str(src)]
        else:
            cmd += ["-fPIC", "-shared", str(src), str(archive), "-o"]
            cmd += [str(Path(tmp) / "probe.so")]
        return subprocess.run(cmd, capture_output=True, check=False).returncode == 0


def _archive_is_pic(archive: Path) -> bool:
    """Return True if a static archive can be linked into a shared object.

    Uses ``readelf -r``: absolute 32-bit relocations (``R_X86_64_32``) mean the
    archive was built without ``-fPIC``. Without ``readelf``, falls back to a
    ``-shared`` link probe.

    Args:
        archive: Path to a ``liburing.a``.

    Returns:
        True when no non-PIC relocations are present.
    """
    readelf = shutil.which("readelf")
    if readelf is None:
        logger.warning("lmcache_fs: readelf not found, using a link probe for PIC")
        return _probe(archive.parent.parent / "include", archive)
    proc = subprocess.run(
        [readelf, "-r", str(archive)], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        return False
    return re.search(r"R_X86_64_32(?!S)\b|R_X86_64_32S\b", proc.stdout) is None


def _liburing_search_candidates() -> list[tuple[Path, Path]] | None:
    """List ``(include_dir, archive)`` candidates, or ``None`` if not opted in.

    Linking is opt-in: ``LMCACHE_LIBURING_DIR`` (expects ``include/`` and
    ``lib/``) or ``LMCACHE_WITH_LIBURING=1`` (system directories).
    """
    override = os.environ.get("LMCACHE_LIBURING_DIR")
    if override:
        base = Path(override)
        return [(base / "include", base / "lib" / "liburing.a")]
    if os.environ.get("LMCACHE_WITH_LIBURING") != "1":
        return None
    return [
        (Path(inc), Path(lib) / "liburing.a")
        for inc in ("/usr/include", "/usr/local/include")
        for lib in (
            "/usr/lib/x86_64-linux-gnu",
            "/usr/lib/aarch64-linux-gnu",
            "/usr/lib64",
            "/usr/lib",
            "/usr/local/lib",
        )
    ]


def _find_liburing() -> tuple[Path, Path] | None:
    """Locate ``liburing.h`` and a PIC ``liburing.a`` of version 2.2 or newer.

    Logs why the engine is compiled out or a candidate was rejected.

    Returns:
        ``(include_dir, archive)`` or ``None`` when the engine is compiled out.
    """
    if not sys.platform.startswith("linux"):
        logger.warning("lmcache_fs: not Linux; io_uring engine compiled out")
        return None
    candidates = _liburing_search_candidates()
    if candidates is None:
        logger.warning(
            "lmcache_fs: io_uring engine compiled out "
            "(set LMCACHE_WITH_LIBURING=1 or LMCACHE_LIBURING_DIR to enable)"
        )
        return None
    explicit = os.environ.get("LMCACHE_LIBURING_DIR")
    for inc_dir, archive in candidates:
        has_header = (inc_dir / "liburing.h").is_file()
        has_archive = archive.is_file()
        if not (has_header and has_archive):
            if explicit:
                logger.warning(
                    "lmcache_fs: LMCACHE_LIBURING_DIR=%s is invalid "
                    "(need %s and %s); io_uring engine compiled out",
                    explicit,
                    inc_dir / "liburing.h",
                    archive,
                )
            continue
        if _compiler() is None:
            logger.warning("lmcache_fs: no C compiler to probe %s, skipping", archive)
            continue
        if not _probe(inc_dir, None):
            logger.warning(
                "lmcache_fs: %s is older than liburing 2.2 "
                "(no io_uring_sqe_set_data64), skipping",
                inc_dir / "liburing.h",
            )
            continue
        if not _archive_is_pic(archive):
            logger.warning(
                "lmcache_fs: %s is not position-independent, skipping", archive
            )
            continue
        return inc_dir, archive
    if not explicit:
        logger.warning(
            "lmcache_fs: no usable liburing >= 2.2 PIC archive found "
            "in system dirs; io_uring engine compiled out"
        )
    return None


def _fs_liburing_build_args() -> tuple[list[str], list[str], list[str]]:
    """Compute liburing-related build inputs for ``lmcache_fs``.

    Returns:
        ``(define_flags, include_dirs, extra_objects)``.
    """
    defines: list[str] = []
    include_dirs: list[str] = []
    objects: list[str] = []
    found = _find_liburing()
    if found is not None:
        inc_dir, archive = found
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
