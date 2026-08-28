#!/usr/bin/env bash
# Continuous ACC capture: passive console stream + periodic tele_cli
# snapshot. Usage: acc_capture_runbook.sh <tty-device> <own-fabric-ip> <log-dir>
#
# Deliberately does NOT live-stream /run/rt_driver.log or /run/rue.log --
# tried that once (tail -f piped into the same console) and reverted it.
# During an actual RUE storm rue.log alone produced 24008 lines in well
# under 0.1s; streaming that over the 460800 baud serial link would take
# many seconds to drain, right when the timing matters most, and risks
# blocking/dropping console output rather than just being slow. Pull
# rt_driver.log/rue.log on demand after a crash instead: find the crash
# window with `grep -n "<date> <hour>:" /run/rt_driver.log` (its own
# clock, not wall-clock-correct but internally consistent), then pull that
# line range with `sed -n` over the same tty pattern used here. See
# irdma-crash-report.txt's THIRD CRASH section for the worked example.
set -euo pipefail
DEV="$1"
IP="$2"
LOGDIR="$3"
mkdir -p "$LOGDIR"

stty -F "$DEV" 460800 raw -echo cs8 -parenb -cstopb -crtscts

# Passive stream: catches kernel console output (RUE storm, Function Reset)
# live, with no polling delay, regardless of when it happens.
nohup cat "$DEV" >> "$LOGDIR/acc-console-live.log" 2>&1 < /dev/null &
echo "started passive console capture (pid $!) -> $LOGDIR/acc-console-live.log"

# Active loop: tele_cli's counters are live/ephemeral with no persistent
# record, so they need periodic snapshots to have a "before" baseline no
# matter how early a crash happens.
(
  while true; do
    {
      echo "=== $(date -u +%Y-%m-%dT%H:%M:%SZ) (host clock, ACC clock is unreliable) ==="
      printf '/opt/falcon/bin/tele_cli -t global -s %s:50051\r\n' "$IP"
    } > "$DEV"
    sleep 30
  done
) < /dev/null >> "$LOGDIR/tele_cli-injector.log" 2>&1 &
echo "started tele_cli injector loop (pid $!), every 30s -> output lands in acc-console-live.log"
