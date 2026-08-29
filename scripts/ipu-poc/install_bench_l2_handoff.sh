#!/usr/bin/env bash
# install_bench_l2_handoff.sh — LMCache dev install for bench l2 geometry + metrics.
#
# Run from the repository root on a benchmark initiator (GPU/storage test host).
# Does not configure NVMe-oF, fabric IPs, or corpus paths — see README-handoff.md.
#
# Usage:
#   bash scripts/ipu-poc/install_bench_l2_handoff.sh
#   LMCACHE_VENV=/opt/lmcache/.venv bash scripts/ipu-poc/install_bench_l2_handoff.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV="${LMCACHE_VENV:-$ROOT/.venv-bench-l2}"
PYTHON="${PYTHON:-python3.12}"

echo "== LMCache bench l2 geometry handoff install =="
echo "   repo: $ROOT"
echo "   venv: $VENV"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "ERROR: $PYTHON not found. Install Python 3.12+, or set PYTHON to the" >&2
    echo "       interpreter this venv should be built from." >&2
    exit 1
fi

if [ ! -d "$VENV" ]; then
    if command -v uv >/dev/null 2>&1; then
        # --seed: uv omits pip from a venv by default, and the installs below
        # call it directly.
        uv venv --seed --python "$PYTHON" "$VENV"
    else
        "$PYTHON" -m venv "$VENV"
    fi
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "== Python packages =="
# Cython and setuptools are build requirements, not runtime ones: the editable
# install below runs with --no-build-isolation, so any dependency without a
# wheel for this platform (nvtx) has to find them already here.
pip install -U pip wheel setuptools Cython
# Geometry profiles (YAML) and optional ACC collector unit tests.
pip install pyyaml grpcio protobuf
if [ -f "$ROOT/requirements/test.txt" ]; then
    pip install -r "$ROOT/requirements/test.txt"
fi

if ! python -c 'import torch' >/dev/null 2>&1; then
    echo ""
    echo "NOTE: torch is not installed in this venv; installing a CPU build."
    echo "      On a CUDA host, install the matching torch wheel first and"
    echo "      re-run, or the extensions below build against CPU torch."
    echo ""
    pip install torch
fi

# The common C++ extensions include torch headers, so torch has to be
# installed before this runs, not alongside it.
pip install -e "$ROOT" --no-build-isolation

echo "== CLI smoke =="
for flag in --kvcache-shape-profile --kvcache-shape-spec --serve-metrics \
    --duration-sec --key-prefix --read-write-ratio; do
    if ! lmcache bench l2 --help 2>&1 | grep -q -- "$flag"; then
        echo "ERROR: 'lmcache bench l2' missing $flag — wrong branch or stale install." >&2
        exit 1
    fi
done
echo "   bench l2 geometry + sustained/metrics flags: OK"

echo ""
echo "== Next steps =="
echo "1. Read scripts/ipu-poc/README-handoff.md"
echo "2. Model profiles: scripts/ipu-poc/models/*.yaml"
echo "3. Example:"
echo "     lmcache bench l2 --l2-adapter '{\"type\":\"fs_native\",\"base_path\":\"/path/kvcache\"}' \\"
echo "       --kvcache-shape-profile scripts/ipu-poc/models/mixtral_8x22b_fp8.yaml \\"
echo "       --only load --serve-metrics 9101 --metrics-bind-address 127.0.0.1"
echo "4. Host observability (each test host, as root):"
echo "     docs/design/v1/platform/ipu-poc/instrumentation/host/install.sh"
echo "5. Control host Grafana stack:"
echo "     docs/design/v1/platform/ipu-poc/instrumentation/up.sh"
