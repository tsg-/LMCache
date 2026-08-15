#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Run a profile-shaped LMCache L2 benchmark.
#
# The profile supplies objects per submit and bytes per object. This helper
# deliberately does not infer storage, fabric, or controller settings.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
MODE=${1:-help}
if [ "$#" -gt 0 ]; then
  shift
fi
PYTHON=${PYTHON:-python3}

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
  BASE_PATH       fs_native storage directory
  PREFIX          read corpus namespace; use a fresh value for store

Optional environment variables:
  PYTHON          Python interpreter with LMCache installed (default: python3)
  L2_ADAPTER      complete L2 adapter JSON; overrides the default fs_native JSON
  NUM_WORKERS     fs_native workers in the default adapter (default: 16)
  IN_FLIGHT       submits in flight (default: 1)
  ROUNDS          measured rounds for store/load (default: 1)
  WARMUP_ROUNDS   warmup rounds for store/load (default: 0)
  DURATION_SEC    sustained window for sustained-load/mixed (default: 60)
  WARMUP_SEC      sustained-load warmup seconds (default: 0)
  READ_WRITE_RATIO mixed read:write ratio (default: 5:1)
  WRITE_PREFIX    fresh store namespace for mixed mode (required for mixed)
  OUTPUT          JSON result path (optional)

The helper prints the resolved profile before submitting work.
EOF
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
  "$PYTHON" - "$profile" <<'PYEOF'
import sys

from lmcache.cli.commands.bench.l2_adapter_bench.geometry import (
    resolve_geometry_profile,
)

profile = sys.argv[1]
geometry = resolve_geometry_profile(profile)
print(f"profile: {profile}")
print(f"model: {geometry.model_name}")
print(f"tokens/chunk: {geometry.tokens_per_chunk}")
print(f"objects/submit: {geometry.objects_per_submit}")
print(f"page: {geometry.page_size_bytes} B ({geometry.data_size_kb} KiB)")
print(f"submit: {geometry.task_size_bytes} B")
PYEOF
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
  local adapter
  require_value BASE_PATH
  require_value PREFIX
  profile_info "$profile"

  adapter=${L2_ADAPTER:-"{\"type\":\"fs_native\",\"base_path\":\"$BASE_PATH\",\"use_odirect\":true,\"num_workers\":${NUM_WORKERS:-16}}"}
  local -a common=(
    -m lmcache.cli.main bench l2
    --l2-adapter "$adapter"
    --kvcache-shape-profile "$profile"
    --key-prefix "$PREFIX"
    --in-flight "${IN_FLIGHT:-1}"
    --l1-align-bytes 4096
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
        --write-key-prefix "$WRITE_PREFIX"
      ;;
  esac
}

case "$MODE" in
  help|-h|--help)
    usage
    ;;
  profiles)
    [ "$#" -eq 0 ] || { usage >&2; exit 2; }
    list_profiles
    ;;
  show)
    [ "$#" -eq 1 ] || { usage >&2; exit 2; }
    profile_info "$1"
    ;;
  store|load|sustained-load|mixed)
    [ "$#" -eq 1 ] || { usage >&2; exit 2; }
    run_bench "$MODE" "$1"
    ;;
  *)
    usage >&2
    exit 2
    ;;
esac
