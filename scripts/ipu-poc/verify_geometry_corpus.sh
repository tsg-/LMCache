#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# verify_geometry_corpus.sh — check that a profile-shaped corpus is bound to the
# profile that wrote it.
#
# Object keys carry no page size, and the fs adapter reports a hit once the
# requested buffer is full, so an unscoped key prefix lets one model's profile
# read another model's corpus and report a plausible bandwidth number. The
# helpers scope keys by profile SHA-256 to turn that mismatch into a miss. This
# script proves that on the machine and filesystem under test.
#
# Usage:
#   bash scripts/ipu-poc/verify_geometry_corpus.sh
#   BASE_PATH=/mnt/lmcache-kvcache bash scripts/ipu-poc/verify_geometry_corpus.sh
#
# Environment:
#   PYTHON      interpreter with this checkout installed (default: autodetected)
#   BASE_PATH   scratch storage directory (default: a temporary directory)
#   KEEP        set to 1 to keep a temporary BASE_PATH for inspection
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
TEST_FILE=tests/scripts/test_model_geometry_scripts.py
# These two profiles both use a 262144 B page, and the smaller one's key range
# is a subset of the larger one's, so without SHA scoping a Mixtral load of a
# Llama-405B corpus reports 56 of 56 hits over another model's bytes. A pair
# with differing page sizes would miss on length alone and prove nothing.
STORED=$SCRIPT_DIR/models/llama3_405b_fp8.yaml
MISMATCHED=$SCRIPT_DIR/models/mixtral_8x22b_fp8.yaml

if [ -z "${PYTHON:-}" ]; then
  declare -a candidates=()
  # Only when set: an empty LMCACHE_VENV would offer /bin/python, which exists
  # on some hosts without LMCache and would win over the checkout's own venv.
  if [ -n "${LMCACHE_VENV:-}" ]; then
    candidates+=("$LMCACHE_VENV/bin/python")
  fi
  candidates+=("$ROOT/.venv-bench-l2/bin/python" "$ROOT/.venv/bin/python")
  for candidate in "${candidates[@]}"; do
    if [ -x "$candidate" ]; then
      PYTHON=$candidate
      break
    fi
  done
fi
PYTHON=${PYTHON:-python3}

echo "== prerequisites =="
echo "   repo: $ROOT"
echo "   python: $PYTHON"
if ! "$PYTHON" -c 'import lmcache.cli.commands.bench.l2_adapter_bench.geometry' \
  >/dev/null 2>&1; then
  echo "ABORT: $PYTHON cannot import LMCache." >&2
  echo "  Run: bash scripts/ipu-poc/install_bench_l2_handoff.sh" >&2
  exit 2
fi
if ! "$PYTHON" -m pytest --version >/dev/null 2>&1; then
  echo "ABORT: pytest is missing. Run: pip install -r requirements/test.txt" >&2
  exit 2
fi

LOG=$(mktemp)
cleanup() {
  rm -f "$LOG"
  if [ -n "${SCRATCH:-}" ] && [ "${KEEP:-0}" != 1 ]; then
    rm -rf "$SCRATCH"
  fi
}
trap cleanup EXIT
if [ -n "${BASE_PATH:-}" ]; then
  mkdir -p "$BASE_PATH"
else
  SCRATCH=$(mktemp -d)
  BASE_PATH=$SCRATCH
fi
echo "   base path: $BASE_PATH"

echo
echo "== namespace regression tests =="
( cd "$ROOT" && "$PYTHON" -m pytest -q "$TEST_FILE" )

# The tests run against a temporary directory. Repeat the load-bearing case on
# the storage actually under test, where O_DIRECT alignment and the adapter's
# real page handling apply.
PREFIX=verify-$(date +%s)
export BASE_PATH PREFIX PYTHON

echo
echo "== live store =="
bash "$SCRIPT_DIR/run_model_geometry.sh" store "$STORED"

# A complete miss is a legitimate outcome here, so tolerate a nonzero exit and
# read the success count out of the result table instead.
load_success() {
  local profile=$1
  bash "$SCRIPT_DIR/run_model_geometry.sh" load "$profile" >"$LOG" 2>&1 || true
  cat "$LOG" >&2
  awk -F: '/^Total success:/ { gsub(/ /, "", $2); print $2 }' "$LOG"
}

echo
echo "== live load with the storing profile (expect hits) =="
matching=$(load_success "$STORED")
if [ "${matching:-0}" -le 0 ]; then
  echo "FAIL: the storing profile read back no objects (success=${matching:-none})" >&2
  exit 1
fi

echo
echo "== live load with a mismatched profile (expect zero hits) =="
mismatched=$(load_success "$MISMATCHED")
if [ "${mismatched:-1}" -ne 0 ]; then
  echo "FAIL: a mismatched profile read ${mismatched:-none} objects; corpus" \
    "identity is not bound to the profile" >&2
  exit 1
fi

echo
echo "== byte readback of the stored corpus =="
"$PYTHON" "$SCRIPT_DIR/geom_readback.py" \
  --base-path "$BASE_PATH" --key-prefix "$PREFIX" \
  --profile "$STORED" --submits 1

echo
echo "== byte readback under the mismatched profile (expect FAIL) =="
if "$PYTHON" "$SCRIPT_DIR/geom_readback.py" \
  --base-path "$BASE_PATH" --key-prefix "$PREFIX" \
  --profile "$MISMATCHED" --submits 1; then
  echo "FAIL: readback verified a corpus written by another profile" >&2
  exit 1
fi

echo
echo "== PASS =="
echo "   $matching objects hit under the storing profile, 0 under a mismatch."
echo "   Byte readback matched the deterministic store fill."
