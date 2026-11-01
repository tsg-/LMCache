#!/usr/bin/env bash
# Raw target sampler for lmc-ghsm: PCM, CPU, disk and ACC counters every ~2 s.
# Usage: ghsm_telemetry.sh OUTPUT [SAMPLES]
set -euo pipefail

output=$1
samples=${2:-7200}
mkdir -p "$(dirname "$output")"
exec >>"$output" 2>&1
for sample in $(seq 1 "$samples"); do
    printf '\nSAMPLE %s %s\n' "$sample" "$(date +%s.%N)"
    cat /var/lib/node_exporter/textfile/pcm_memory.prom
    cat /var/lib/node_exporter/textfile/pcm_pcie.prom
    printf 'CPU\n'
    cat /proc/stat
    printf 'DISKS\n'
    cat /proc/diskstats
    printf 'ACC\n'
    curl --noproxy '*' -fsS --max-time 5 http://127.0.0.1:9188/metrics |
        grep -E '^acc_telemetry_|^acc_tele_field\{.*section="global",field="(bytes_from_ulp_rc|bytes_to_ulp)"'
    sleep 2
done
