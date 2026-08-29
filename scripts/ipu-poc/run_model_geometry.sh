#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Run a profile-shaped LMCache L2 benchmark.
#
# The profile supplies objects per submit and bytes per object. This helper
# deliberately does not infer storage, fabric, or controller settings.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
MODE=${1:-help}
if [ "$#" -gt 0 ]; then
  shift
fi

detect_python() {
  if [ -n "${PYTHON:-}" ]; then
    return
  fi

  local candidate
  local -a candidates=()
  if [ -n "${LMCACHE_VENV:-}" ]; then
    candidates+=("$LMCACHE_VENV/bin/python")
  fi
  candidates+=(
    "$ROOT/.venv-bench-l2/bin/python"
    "$ROOT/.venv/bin/python"
    "python3"
  )
  for candidate in "${candidates[@]}"; do
    if [[ "$candidate" == */* ]] && [ ! -x "$candidate" ]; then
      continue
    fi
    if "$candidate" -c \
      'import lmcache.cli.commands.bench.l2_adapter_bench.geometry' \
      >/dev/null 2>&1; then
      PYTHON=$candidate
      return
    fi
  done

  echo "ABORT: could not find a Python interpreter with LMCache installed." >&2
  echo "  Set LMCACHE_VENV or PYTHON, or run install_bench_l2_handoff.sh." >&2
  exit 2
}

usage() {
  cat <<'EOF'
Usage:
  run_model_geometry.sh profiles
  run_model_geometry.sh show PROFILE
  run_model_geometry.sh store PROFILE
  run_model_geometry.sh load PROFILE
  run_model_geometry.sh sustained-load PROFILE
  run_model_geometry.sh mixed PROFILE

Run modes require these environment variables:
  BASE_PATH       fs_native storage directory; not required when L2_ADAPTER is set
  PREFIX          read corpus namespace; use a fresh value for store

Optional environment variables:
  PYTHON          Python interpreter with LMCache installed (auto-detected)
  LMCACHE_VENV    Virtual environment to try before checkout-local environments
  L2_ADAPTER      complete L2 adapter JSON; overrides the default fs_native JSON
  NUM_WORKERS     fs_native workers in the default adapter (default: 16)
  L1_ALIGN_BYTES  benchmark L1 buffer alignment (default: 4096 for O_DIRECT).
                  Set to 1 for a buffered adapter. Required for a profile
                  whose object sizes are not 4096-aligned -- `show` says so.
  IN_FLIGHT       submits in flight (default: 1)
  ROUNDS          measured rounds for store/load (default: 1)
  WARMUP_ROUNDS   warmup rounds for store/load (default: 0)
  DURATION_SEC    sustained window for sustained-load/mixed (default: 60)
  WARMUP_SEC      sustained-load warmup seconds (default: 0)
  READ_WRITE_RATIO mixed read:write ratio (default: 5:1)
  WRITE_PREFIX    fresh store namespace for mixed mode (required for mixed)
  OUTPUT          JSON result path (optional)

The helper prints the resolved profile before submitting work.

Object keys are namespaced by PREFIX and the profile's SHA-256, so a corpus
stored under one profile cannot be read back under another. Reuse the same
PREFIX and the same profile file to read a corpus back; the derived namespace
is printed for the record and is not itself a PREFIX value.
EOF
}

profile_sha() {
  local profile=$1
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$profile" | cut -d' ' -f1
  else
    shasum -a 256 "$profile" | cut -d' ' -f1
  fi
}

require_value() {
  local name=$1
  if [ -z "${!name:-}" ]; then
    echo "ABORT: $name must be set" >&2
    exit 2
  fi
}

profile_info() {
  local profile=$1
  [ -f "$profile" ] || { echo "ABORT: profile not found: $profile" >&2; exit 2; }
  "$PYTHON" - "$profile" "${L1_ALIGN_BYTES:-4096}" <<'PYEOF'
import sys

from lmcache.cli.commands.bench.l2_adapter_bench.geometry import (
    PROFILE_MODE_OBJECT_GROUP,
    resolve_submit_geometry,
)

profile = sys.argv[1]
align = int(sys.argv[2])
geometry = resolve_submit_geometry(profile)
print(f"profile: {profile}")
print(f"model: {geometry.model_name}")
print(f"geometry: {geometry.profile_mode}")
print(f"tokens/chunk: {geometry.tokens_per_chunk}")
print(f"objects/submit: {geometry.objects_per_submit}")
if geometry.profile_mode == PROFILE_MODE_OBJECT_GROUP:
    print(f"task archetype: {geometry.task_archetype}")
    print(f"chunks/submit: {geometry.chunks_per_submit}")
    print(f"kv ranks/chunk: {geometry.kv_ranks_per_chunk}")
    for group in geometry.object_groups:
        packed = ", ".join(
            f"{c.name} {c.component_size_bytes} B" for c in group.components
        )
        print(
            f"object group {group.object_group_id} {group.name}: "
            f"{group.object_size_bytes} B [{packed}]"
        )
else:
    print(f"page: {geometry.page_size_bytes} B ({geometry.data_size_kb} KiB)")
print(f"submit: {geometry.task_size_bytes} B")
# Objects are placed at the prefix sum of their predecessors, so one
# unaligned size misaligns everything after it and bench l2 rejects the
# run. Say so here rather than at submit time: the operator's next move
# is to pick a different backend, not to retry.
unaligned = sorted({s for s in geometry.object_sizes_bytes if s % align != 0})
if unaligned:
    print(
        f"NOTE: object sizes {unaligned} are not multiples of {align} B, so "
        f"this profile cannot run against an O_DIRECT backend; use a "
        f"buffered adapter and set L1_ALIGN_BYTES=1"
    )
PYEOF
  echo "sha256: $(profile_sha "$profile")"
}

list_profiles() {
  local profile
  for profile in "$SCRIPT_DIR"/models/*.yaml; do
    profile_info "$profile"
    echo
  done
}

run_bench() {
  local mode=$1 profile=$2
  local adapter sha12 namespace
  if [ -z "${L2_ADAPTER:-}" ]; then
    require_value BASE_PATH
  fi
  require_value PREFIX
  profile_info "$profile"

  # Object keys carry no page size, and a short read of a larger stored
  # object still counts as a hit, so scope the namespace by profile SHA to
  # turn a profile/corpus mismatch into a miss instead of a wrong number.
  sha12=$(profile_sha "$profile")
  sha12=${sha12:0:12}
  namespace="${PREFIX}-${sha12}"
  echo "prefix: $PREFIX (reuse this as PREFIX; do not pass the namespace below)"
  echo "key namespace: $namespace"

  adapter=${L2_ADAPTER:-"{\"type\":\"fs_native\",\"base_path\":\"${BASE_PATH:-}\",\"use_odirect\":true,\"num_workers\":${NUM_WORKERS:-16}}"}
  local -a common=(
    -m lmcache.cli.main bench l2
    --l2-adapter "$adapter"
    --kvcache-shape-profile "$profile"
    --key-prefix "$namespace"
    --in-flight "${IN_FLIGHT:-1}"
    --l1-align-bytes "${L1_ALIGN_BYTES:-4096}"
  )
  if [ -n "${OUTPUT:-}" ]; then
    common+=(--output "$OUTPUT" --format json)
  fi

  case "$mode" in
    store)
      "$PYTHON" "${common[@]}" --only store \
        --rounds "${ROUNDS:-1}" --warmup-rounds "${WARMUP_ROUNDS:-0}"
      ;;
    load)
      "$PYTHON" "${common[@]}" --only load \
        --rounds "${ROUNDS:-1}" --warmup-rounds "${WARMUP_ROUNDS:-0}"
      ;;
    sustained-load)
      "$PYTHON" "${common[@]}" --only load \
        --duration-sec "${DURATION_SEC:-60}" --warmup-sec "${WARMUP_SEC:-0}"
      ;;
    mixed)
      require_value WRITE_PREFIX
      "$PYTHON" "${common[@]}" \
        --duration-sec "${DURATION_SEC:-60}" \
        --read-write-ratio "${READ_WRITE_RATIO:-5:1}" \
        --write-key-prefix "${WRITE_PREFIX}-${sha12}"
      ;;
  esac
}

case "$MODE" in
  help|-h|--help)
    usage
    ;;
  profiles)
    [ "$#" -eq 0 ] || { usage >&2; exit 2; }
    detect_python
    echo "python: $PYTHON"
    list_profiles
    ;;
  show)
    [ "$#" -eq 1 ] || { usage >&2; exit 2; }
    detect_python
    echo "python: $PYTHON"
    profile_info "$1"
    ;;
  store|load|sustained-load|mixed)
    [ "$#" -eq 1 ] || { usage >&2; exit 2; }
    detect_python
    echo "python: $PYTHON"
    run_bench "$MODE" "$1"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
