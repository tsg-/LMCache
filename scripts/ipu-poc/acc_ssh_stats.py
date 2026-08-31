#!/usr/bin/env python3
# acc_ssh_stats.py -- periodic ACC core-usage and Falcon transport counters,
# pulled over the netns -> IMC -> ACC SSH path (Naveen's recipe) instead of
# blind serial-console injection. Core usage and transport counters use separate
# textfiles so transport can be sampled more frequently without duplicating
# Prometheus series.
#
# Path: `[ip netns exec <netns>] ssh root@100.0.0.100` (IMC)
#       -> `ssh root@192.168.96.2` (ACC, passwordless)
#
# The netns hop is OPTIONAL and rig-specific. On the target (mmgt) the IMC
# management vport is namespaced, so each card is reached through its own netns
# (IPU1/IPU2). On the initiators (mmgi0/mmgi1) the IMC link is a plain host
# interface holding 100.0.0.1/24, so there is no netns and ssh goes direct --
# an empty netns field selects that path.
#
# Targets come from $ACC_TARGETS as "netns:label:acc-fabric-ip" entries,
# comma-separated, defaulting to the two mmgt cards. Leave netns empty for the
# direct path, e.g. ACC_TARGETS=":acc1:200.0.4.3" on mmgi0.
#
# The IMC root password comes from $IMC_PASSWORD and is deliberately NOT
# defaulted here -- this repo's origin is public. The systemd unit reads it
# from /etc/default/acc-stats; see the instrumentation kit README.
#
# ACC core usage comes from two /proc/stat reads 1s apart -- no mpstat/sar/
# dstat, none of which are installed on this Edge Microvisor Toolkit image
# (minimal aarch64, no package-manager frontend). /proc/stat is the same
# primitive those tools read internally; this skips the extra process fork.
#
# Usage: acc_ssh_stats.py <netns> <acc-label> <acc-fabric-ip> <log-dir> [interval-secs]
# Example: acc_ssh_stats.py IPU2 acc1 200.0.6.3 /root/captures-acc1 30
#
# Oneshot node_exporter textfile modes:
#   acc_ssh_stats.py --core-textfile /var/lib/node_exporter/textfile/acc_stats.prom
#   acc_ssh_stats.py --transport-textfile
#       /var/lib/node_exporter/textfile/acc_transport.prom

import os
import re
import sys
import time
import select

IMC_IP = "100.0.0.100"
IMC_PASSWORD = os.environ.get("IMC_PASSWORD", "")
ACC_IP = "192.168.96.2"

# (netns, acc-label, acc-fabric-ip) -- default is the two MMG-400 cards on mmgt.
DEFAULT_ACC_TARGETS = "IPU2:acc1:200.0.6.3,IPU1:acc2:200.0.5.3"


def parse_targets(spec):
    """Parse "netns:label:ip,..." into [(netns, label, ip)]; netns may be empty."""
    targets = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 3:
            raise ValueError(
                f"bad ACC_TARGETS entry {entry!r}, want netns:label:acc-fabric-ip"
            )
        netns, label, ip = (p.strip() for p in parts)
        if not label or not ip:
            raise ValueError(f"ACC_TARGETS entry {entry!r} needs a label and an IP")
        targets.append((netns, label, ip))
    if not targets:
        raise ValueError("ACC_TARGETS is empty")
    return targets


ACC_TARGETS = parse_targets(os.environ.get("ACC_TARGETS", DEFAULT_ACC_TARGETS))


def ssh_hop(cmd, password, timeout=25):
    """Run cmd under a pty, answering any 'password' prompt with password."""
    pid, master = os.forkpty()
    if pid == 0:
        os.execvp(cmd[0], cmd)
    output = b""
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([master], [], [], 1)
        if master in r:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            if b"password" in chunk.lower():
                time.sleep(0.3)
                os.write(master, (password + "\n").encode())
    try:
        os.kill(pid, 9)
        os.waitpid(pid, 0)
    except (ProcessLookupError, ChildProcessError):
        pass
    return output.decode(errors="replace")


def parse_proc_stat(text):
    """Return {cpu_label: (user, nice, system, idle, iowait, irq, softirq, steal)}."""
    cpus = {}
    for line in text.splitlines():
        if not line.startswith("cpu"):
            continue
        parts = line.split()
        label = parts[0]
        if label == "cpu" or label[3:].isdigit():
            vals = tuple(int(v) for v in parts[1:9])
            cpus[label] = vals
    return cpus


def busy_pct(before, after):
    """Per-core busy% from two /proc/stat snapshots (jiffies, not seconds)."""
    result = {}
    for label, a in after.items():
        b = before.get(label)
        if b is None:
            continue
        deltas = [a[i] - b[i] for i in range(8)]
        total = sum(deltas)
        idle = deltas[3] + deltas[4]  # idle + iowait
        result[label] = 100.0 * (total - idle) / total if total > 0 else 0.0
    return result


def parse_tele_fields(text):
    """Return [(section, field, value)] for numeric 'field: value' lines.

    Sections are the header lines between '====' separators (Global Counters,
    RX counters, TX counters, RUE counters).
    """
    fields = []
    section = "global"
    header_re = re.compile(
        r"^(Global Counters|RX counters|TX counters|RUE counters):?$"
    )
    field_re = re.compile(r"^([A-Za-z_][\w]*)\s*:\s*(-?\d+)$")
    for line in text.splitlines():
        line = line.strip()
        m = header_re.match(line)
        if m:
            section = m.group(1).lower().replace(" counters", "").replace(" ", "_")
            continue
        m = field_re.match(line)
        if m:
            fields.append((section, m.group(1), int(m.group(2))))
    return fields


def write_textfile(path, lines):
    """Atomically write lines to a node_exporter textfile."""
    import tempfile

    out_dir = os.path.dirname(path)
    fd, tmp_path = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=out_dir)
    try:
        with os.fdopen(fd, "w") as f:
            f.writelines(lines)
        os.chmod(tmp_path, 0o644)
        os.rename(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise


def write_core_prom_textfile(path, results):
    """Write ACC core busy gauges without transport counter series."""
    lines = [
        "# HELP acc_cpu_busy_percent ACC core busy percentage over a 1s "
        "/proc/stat sample\n",
        "# TYPE acc_cpu_busy_percent gauge\n",
    ]
    for acc_label, pct in results:
        for core, val in pct.items():
            metric = f'acc_cpu_busy_percent{{acc="{acc_label}",core="{core}"}}'
            lines.append(f"{metric} {val:.2f}\n")
    write_textfile(path, lines)


def write_transport_prom_textfile(path, results):
    """Write Falcon transport counters without ACC core busy gauge series."""
    lines = [
        "# HELP acc_tele_field Falcon transport-engine counter from tele_cli "
        "-t global\n",
        "# TYPE acc_tele_field counter\n",
    ]
    for acc_label, fields in results:
        for section, field, value in fields:
            metric = (
                f'acc_tele_field{{acc="{acc_label}",section="{section}",'
                f'field="{field}"}}'
            )
            lines.append(f"{metric} {value}\n")
    write_textfile(path, lines)


def sample_remote(netns, remote_cmd):
    """Run one remote ACC command through its IMC management path."""
    hop_cmd = (
        f"ssh -oStrictHostKeyChecking=no -oUserKnownHostsFile=/dev/null "
        f"root@{ACC_IP} '{remote_cmd}'"
    )
    imc_cmd = [
        "ssh",
        "-oStrictHostKeyChecking=no",
        "-oUserKnownHostsFile=/dev/null",
        "-t",
        f"root@{IMC_IP}",
        hop_cmd,
    ]
    if netns:
        imc_cmd = ["ip", "netns", "exec", netns] + imc_cmd
    return ssh_hop(imc_cmd, IMC_PASSWORD)


def sample_core(netns):
    """Read two /proc/stat snapshots from one ACC."""
    remote_cmd = "cat /proc/stat; echo ---SNAP2---; sleep 1; cat /proc/stat"
    return sample_remote(netns, remote_cmd)


def sample_transport(netns, acc_fabric_ip):
    """Read Falcon transport counters from one ACC."""
    return sample_remote(
        netns,
        f"/opt/falcon/bin/tele_cli -t global -s {acc_fabric_ip}:50051 2>&1",
    )


def sample(netns, acc_fabric_ip):
    """Read core and transport data together for the legacy capture loop."""
    remote_cmd = (
        "cat /proc/stat; echo ---SNAP2---; sleep 1; cat /proc/stat; "
        "echo ---TELE---; "
        f"/opt/falcon/bin/tele_cli -t global -s {acc_fabric_ip}:50051 2>&1"
    )
    return sample_remote(netns, remote_cmd)


def run_core_textfile_oneshot(path):
    """Sample every ACC core once and write the core-gauge textfile."""
    results = []
    for netns, acc_label, _acc_fabric_ip in ACC_TARGETS:
        raw = sample_core(netns)
        try:
            proc_stat_part, snap2_part = raw.split("---SNAP2---", 1)
        except ValueError:
            sys.stderr.write(f"WARNING: core sample failed for {acc_label}, skipping\n")
            continue
        before = parse_proc_stat(proc_stat_part)
        after = parse_proc_stat(snap2_part)
        pct = busy_pct(before, after)
        results.append((acc_label, pct))
    write_core_prom_textfile(path, results)


def run_transport_textfile_oneshot(path):
    """Sample every Falcon transport counter once and write its textfile."""
    results = []
    for netns, acc_label, acc_fabric_ip in ACC_TARGETS:
        raw = sample_transport(netns, acc_fabric_ip)
        fields = parse_tele_fields(raw)
        if not fields:
            sys.stderr.write(
                f"WARNING: transport sample failed for {acc_label}, skipping\n"
            )
            continue
        results.append((acc_label, fields))
    write_transport_prom_textfile(path, results)


def main():
    if not IMC_PASSWORD:
        sys.stderr.write(
            "IMC_PASSWORD is unset. Export it, or put it in /etc/default/acc-stats\n"
            "as IMC_PASSWORD=<imc-root-password> for the acc-stats systemd unit.\n"
        )
        sys.exit(1)

    if sys.argv[1:2] == ["--core-textfile"]:
        if len(sys.argv) != 3:
            sys.stderr.write("usage: acc_ssh_stats.py --core-textfile <path>\n")
            sys.exit(1)
        run_core_textfile_oneshot(sys.argv[2])
        return

    if sys.argv[1:2] == ["--transport-textfile"]:
        if len(sys.argv) != 3:
            sys.stderr.write("usage: acc_ssh_stats.py --transport-textfile <path>\n")
            sys.exit(1)
        run_transport_textfile_oneshot(sys.argv[2])
        return

    if len(sys.argv) < 5:
        sys.stderr.write(
            "usage: acc_ssh_stats.py <netns> <acc-label> <acc-fabric-ip> "
            "<log-dir> [interval-secs]\n"
            "       acc_ssh_stats.py --core-textfile <path>\n"
            "       acc_ssh_stats.py --transport-textfile <path>\n"
        )
        sys.exit(1)
    netns, acc_label, acc_fabric_ip, log_dir = sys.argv[1:5]
    interval = float(sys.argv[5]) if len(sys.argv) > 5 else 30.0

    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "acc-ssh-stats.log")

    while True:
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        raw = sample(netns, acc_fabric_ip)
        try:
            proc_stat_part, rest = raw.split("---SNAP2---", 1)
            snap2_part, tele_part = rest.split("---TELE---", 1)
        except ValueError:
            with open(log_path, "a") as f:
                f.write(f"=== {ts} {acc_label} SAMPLE FAILED ===\n{raw}\n")
            time.sleep(interval)
            continue

        before = parse_proc_stat(proc_stat_part)
        after = parse_proc_stat(snap2_part)
        pct = busy_pct(before, after)

        with open(log_path, "a") as f:
            f.write(f"=== {ts} {acc_label} ({netns} -> {acc_fabric_ip}) ===\n")
            for label in sorted(
                pct,
                key=lambda core_label: (core_label != "cpu", core_label),
            ):
                f.write(f"{label}: {pct[label]:.1f}%\n")
            f.write("--- tele_cli global ---\n")
            f.write(tele_part.strip() + "\n")
            f.flush()

        time.sleep(interval)


if __name__ == "__main__":
    main()
