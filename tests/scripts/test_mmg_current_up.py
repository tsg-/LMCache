# SPDX-License-Identifier: Apache-2.0
"""Exercise the current MMG launcher without touching live SSH or Docker."""

# Standard
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

# Third Party
import pytest

INSTRUMENTATION = (
    Path(__file__).parents[2] / "docs/design/v1/platform/ipu-poc/instrumentation"
)


@pytest.fixture
def launcher(tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    """Copy the real launcher and replace external commands with recorded peers."""
    stack = tmp_path / "stack"
    shutil.copytree(INSTRUMENTATION, stack)
    (stack / ".env").write_text("GRAFANA_PASSWORD=test-only\n")
    commands = tmp_path / "commands"
    commands.mkdir()
    log = tmp_path / "calls.jsonl"
    driver = commands / "driver"
    driver.write_text(
        f"#!{sys.executable}\n"
        """import hashlib,json,os,re,sys,time
from pathlib import Path
name=Path(sys.argv[0]).name
args=sys.argv[1:]
with open(os.environ['MOCK_LOG'],'a') as output:
 output.write(json.dumps([name,*args])+'\\n')
if name=='sleep':pass
elif name=='uname':print('Linux')
elif name=='lsof':sys.exit(1)
elif name=='ss':
 if os.environ.get('MOCK_LISTEN'):
  for port in [19200,19201,19202,19206,19207,19208,19209,19211,19212,19213]:
   print(f'LISTEN 0 128 172.17.0.1:{port} 0.0.0.0:*')
elif name=='ssh':
 if args[-1].startswith('stat '):
  for path in re.findall(r'/var/lib/[^ ]+\\.prom',args[-1]):
   age=600 if os.environ.get('MOCK_STALE') in args[:-1] else 0
   print(path,int(time.time())-age)
elif name=='curl':
 if '/metrics' in args[-1]:print('acc_telemetry_exporter_complete 1')
 elif '/api/health' in args[-1]:
  status='failed' if os.environ.get('MOCK_GRAFANA_BAD') else 'ok'
  print(json.dumps({'database':status}))
 elif '/targets' in args[-1]:
  nodes=['mmgt','mmgt2']+[f'mmgi{i}' for i in range(6)]
  targets=[{'labels':{'host':host,'job':job},'health':'up','lastError':''}
           for job,hosts in [('node_6x2',nodes),
                             ('acc_grpc_6x2',['mmgt','mmgt2'])]
           for host in hosts]
  if os.environ.get('MOCK_MISSING_TARGET'):
   targets=[target for target in targets if target['labels']['host']!='mmgt2']
  print(json.dumps({'data':{'activeTargets':targets}}))
elif name=='docker':
 running=(Path(os.environ['MOCK_STACK'])/'grafana-running').exists()
 grafana_stopped=os.environ.get('MOCK_GRAFANA_STOPPED') and not running
 if args[0]=='network':print('172.17.0.1')
 elif args[0]=='inspect':
  if 'HostIp' in ' '.join(args):print('10.166.96.242')
  elif args[-1]=='lmcache-prom-6x2' and os.environ.get('MOCK_PROM_MISSING'):sys.exit(1)
  elif args[-1]=='lmcache-grafana' and grafana_stopped:print('false')
  else:print('true')
 elif args[0]=='port':
  if 'grafana' in args[1] and grafana_stopped:sys.exit(1)
  print('10.166.96.242:'+('3000' if 'grafana' in args[1] else '9091'))
 elif args[0]=='exec' and any('sha256sum' in x for x in args):
  path=Path(os.environ['MOCK_STACK'])/'profiles/6x2/prometheus.yml'
  digest=hashlib.sha256(path.read_bytes()).hexdigest()
  print(('bad' if os.environ.get('MOCK_DRIFT') else digest)+'  config')
 elif args[0]=='compose' and 'up' in args:
  (Path(os.environ['MOCK_STACK'])/'grafana-running').touch()
 elif args[0]=='compose' and 'port' in args:print('10.166.96.242:9090')
 elif args[0]=='compose' and 'exec' in args:
  path=Path(os.environ['MOCK_STACK'])/'prometheus.yml'
  print(hashlib.sha256(path.read_bytes()).hexdigest()+'  config')
"""
    )
    driver.chmod(0o755)
    for name in ["docker", "ssh", "ss", "lsof", "curl", "sleep", "uname"]:
        (commands / name).symlink_to(driver)
    env = os.environ.copy()
    env.update(
        PATH=str(commands) + os.pathsep + env["PATH"],
        MOCK_LOG=str(log),
        MOCK_STACK=str(stack),
        COLLECTOR_CHECKS="",
        MMG_BENCH_TUNNELS="1",
    )
    return stack, env, log


def test_default_launcher_opens_all_current_tunnels_with_correct_ssh_routes(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    """The default must cover six initiators and both targets with their SSH routes."""
    stack, env, log = launcher
    result = subprocess.run(
        ["bash", str(stack / "up.sh")], env=env, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    forwards = {
        call[call.index("-L") + 1]: call
        for call in calls
        if call[0] == "ssh" and "-L" in call
    }
    assert set(forwards) == {
        "172.17.0.1:19206:127.0.0.1:9100",
        "172.17.0.1:19212:127.0.0.1:9100",
        "172.17.0.1:19207:127.0.0.1:9100",
        "172.17.0.1:19208:127.0.0.1:9100",
        "172.17.0.1:19209:127.0.0.1:9100",
        "172.17.0.1:19200:127.0.0.1:9100",
        "172.17.0.1:19201:127.0.0.1:9100",
        "172.17.0.1:19202:127.0.0.1:9100",
        "172.17.0.1:19211:127.0.0.1:9188",
        "172.17.0.1:19213:127.0.0.1:9188",
        "172.17.0.1:19210:127.0.0.1:9101",
        "172.17.0.1:19220:127.0.0.1:9102",
        "172.17.0.1:19230:127.0.0.1:9103",
        "172.17.0.1:19240:127.0.0.1:9104",
        "172.17.0.1:19250:127.0.0.1:9105",
        "172.17.0.1:19260:127.0.0.1:9106",
    }
    for port, target in [("19201", "lmcache6x2-mmgi4"), ("19202", "lmcache6x2-mmgi5")]:
        call = forwards[f"172.17.0.1:{port}:127.0.0.1:9100"]
        assert "-F" in call and call[-1] == target
    call = forwards["172.17.0.1:19212:127.0.0.1:9100"]
    assert "-F" in call and call[-1] == "lmcache6x6-mmgt2"
    assert "/d/lmcache-6x2" in result.stdout
    assert not any(call[:2] == ["docker", "run"] for call in calls)


def test_check_mode_never_starts_containers_or_opens_tunnels(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    """A live audit must perform reads without restarting the monitoring stack."""
    stack, env, log = launcher
    env["MOCK_LISTEN"] = "1"
    result = subprocess.run(
        ["bash", str(stack / "up.sh"), "--check"],
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert not any(call[0] == "ssh" and "-L" in call for call in calls)
    assert not any(
        call[0] == "docker"
        and any(value in call for value in ["up", "start", "run", "kill"])
        for call in calls
    )
    assert not any(call[0] == "curl" and "-X" in call for call in calls)


def test_unknown_profile_refuses_to_change_anything(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    """A misspelled profile must not silently bring up a different configuration."""
    stack, env, log = launcher
    env["MMG_MONITORING_PROFILE"] = "unknown"
    result = subprocess.run(["bash", str(stack / "up.sh")], env=env)
    assert result.returncode != 0
    assert not log.exists()


def test_check_mode_validates_current_collector_paths(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    """Extra hosts use their own textfile directory and plural QP filename."""
    stack, env, log = launcher
    env.pop("COLLECTOR_CHECKS")
    env["MOCK_LISTEN"] = "1"
    result = subprocess.run(
        ["bash", str(stack / "up.sh"), "--check"], env=env, capture_output=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    stats = {
        call[-2]: call[-1]
        for call in calls
        if call[0] == "ssh" and call[-1].startswith("stat ")
    }
    assert len(stats) == 8
    for host in ["lmcache6x2-mmgi4", "lmcache6x2-mmgi5"]:
        assert "/var/lib/lmcache-6x2/textfile/nvmeof_qps.prom" in stats[host]
        assert "/var/lib/lmcache-6x2/textfile/acc_stats.prom" in stats[host]
    assert "/var/lib/node_exporter/textfile/acc_stats.prom" in stats["mmgi2"]


@pytest.mark.parametrize(
    "failure",
    ["MOCK_DRIFT", "MOCK_STALE", "MOCK_MISSING_TARGET", "MOCK_GRAFANA_BAD"],
)
def test_check_mode_rejects_config_drift_stale_collectors_or_missing_targets(
    launcher: tuple[Path, dict[str, str], Path], failure: str
) -> None:
    """A successful audit requires matching configuration and fresh full coverage."""
    stack, env, _log = launcher
    env.pop("COLLECTOR_CHECKS")
    env["MOCK_LISTEN"] = "1"
    env[failure] = "lmcache6x2-mmgi4" if failure == "MOCK_STALE" else "1"
    result = subprocess.run(
        ["bash", str(stack / "up.sh"), "--check"], env=env, capture_output=True
    )
    assert result.returncode != 0


def test_explicit_legacy_mode_preserves_the_old_launcher(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    """Historical-stack recovery must remain available without six-host startup."""
    stack, env, log = launcher
    env["MMG_MONITORING_PROFILE"] = "legacy"
    result = subprocess.run(
        ["bash", str(stack / "up.sh")], env=env, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    forwards = [
        call[call.index("-L") + 1]
        for call in calls
        if call[0] == "ssh" and "-L" in call
    ]
    assert len(forwards) == 10
    assert all(":191" in endpoint for endpoint in forwards)


def test_bootstrap_preserves_existing_management_binding_and_data_volume(
    launcher: tuple[Path, dict[str, str], Path],
) -> None:
    """Stopped Grafana metadata must preserve its management address on startup."""
    stack, env, log = launcher
    env["MOCK_GRAFANA_STOPPED"] = "1"
    env["MOCK_PROM_MISSING"] = "1"
    result = subprocess.run(
        ["bash", str(stack / "up.sh")], env=env, text=True, capture_output=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    starts = [call for call in calls if call[:2] == ["docker", "run"]]
    assert len(starts) == 1, result.stdout + result.stderr
    assert "10.166.96.242:9091:9090" in starts[0]
    assert "lmcache-telemetry-6x2_prom-data:/prometheus" in starts[0]
    assert (stack / ".env").read_text() == "GRAFANA_PASSWORD=test-only\n"
