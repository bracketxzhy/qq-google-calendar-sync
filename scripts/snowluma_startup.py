"""One bounded Windows logon attach. No QQ launch, login, kill or recovery loop."""
from pathlib import Path
import contextlib
import ctypes
import importlib.util
import json
import os
import subprocess
import time
import sys
import settings

import pipeline
import snowluma_source as source

PROJECT = Path(__file__).resolve().parent.parent

TASK_NAME = 'QQ SnowLuma Logon Attach'


def hidden_powershell(command):
    return subprocess.check_output(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
        text=True, encoding='utf-8-sig', creationflags=subprocess.CREATE_NO_WINDOW,
        timeout=25)


def snapshot(project=PROJECT):
    # Emit process identity and ownership booleans, never complete command lines.
    qq_path = settings.load(project)['qq_path'].replace("'", "''")
    code = r'''
    $ErrorActionPreference='Stop'
    $all=@(Get-CimInstance Win32_Process)
    $qq=@($all | Where-Object {$_.ExecutablePath -ieq '__QQ_PATH__'})
    $roots=@($qq | Where-Object {$_.CommandLine -notmatch '--type=' -and $_.ParentProcessId -notin $qq.ProcessId} | ForEach-Object {
      @{pid=[int]$_.ProcessId;path=$_.ExecutablePath;created=$_.CreationDate.ToUniversalTime().ToString('o')}
    })
    $ports=@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {$_.LocalPort -in @(3300,3302,35099)} | ForEach-Object {
      $port=$_
      $owner=$all | Where-Object {$_.ProcessId -eq $port.OwningProcess} | Select-Object -First 1
      @{port=[int]$port.LocalPort;host=$port.LocalAddress;pid=[int]$port.OwningProcess;path=$owner.ExecutablePath;
        receiver=($owner.CommandLine -match 'scripts[\\/]snowluma_source.py["\s]+receive')}
    })
    $boot=(Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
    @{qq=$roots;ports=$ports;boot=$boot} | ConvertTo-Json -Depth 5 -Compress
    '''
    return json.loads(hidden_powershell(code.replace('__QQ_PATH__', qq_path)))


def verify_services(info, project=PROJECT):
    ports = info['ports']
    runtime = str(project/'runtime/snowluma-v2.1.1/node.exe').casefold()
    for entry in ports:
        expected = runtime if entry['port'] in (3300, 35099) else str(Path(sys.executable).with_name('pythonw.exe')).casefold()
        if entry['host'] != '127.0.0.1' or str(entry['path']).casefold() != expected:
            raise ValueError('Local service port ownership or loopback binding mismatch')
        if entry['port'] == 3302 and not entry['receiver']:
            raise ValueError('Receiver process identity mismatch')
    present = {entry['port'] for entry in ports}
    if not present:
        return False
    if not {3302, 35099} <= present:
        raise ValueError('Partial local services; manual inspection required, no forced recovery')
    if any(sum(entry['port'] == port for entry in ports) != 1 for port in present):
        raise ValueError('Ambiguous listener ownership')
    return True


def select_target(info, panel, project=PROJECT):
    roots = info['qq']
    if not roots:
        return None
    if len(roots) != 1 or roots[0]['path'].casefold() != settings.load(project)['qq_path'].casefold():
        raise ValueError('QQ main process is not uniquely identified')
    target = roots[0]
    candidates = [item for item in panel if item['pid'] == target['pid']]
    if len(candidates) != 1:
        return None
    item = candidates[0]
    if str(item.get('uin')) not in {'0', source.account(project)}:
        raise ValueError('QQ account mismatch; no native attach permitted')
    if item.get('status') == 'available':
        return target
    if item.get('status') == 'online' and str(item.get('uin')) == source.account(project):
        return None  # Wait for a fresh heartbeat; never attach twice.
    if item.get('status') in {'loading', 'connected', 'initializing'}:
        return None
    raise ValueError('Existing native session is abnormal; no repeated attach')


class Environment:
    def __init__(self, project=PROJECT):
        self.project = project
        self.path = project/'state/snowluma/startup/last.json'
        spec = importlib.util.spec_from_file_location('snowluma_boot', project/'scripts/start-snowluma-monitor.py')
        self.boot = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.boot)
        self.token = None

    def authorized(self):
        policy = json.loads((self.project/'.private/snowluma-startup.json').read_text(encoding='utf-8'))
        return (policy.get('enabled') is True and policy.get('authorization') == 'explicit_local_setup'
                and policy.get('qq_restart') is False and policy.get('qq_password_login') is False)

    def previous(self):
        return json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}

    def publish(self, record):
        pipeline.save(self.path, record)
        pipeline.save(self.path.parent/'runs'/f"{record['run_id']}.json", record)

    def inspect(self):
        return snapshot(self.project)

    def health(self):
        return source.status(self.project)['healthy']

    def start(self):
        self.boot.start()

    def panel(self):
        if self.token is None:
            secret = json.loads((self.project/'.private/snowluma-secrets.json').read_text(encoding='utf-8'))
            result = self.boot.local_api('/api/login', {'password': secret['webui_password']})
            if not result.get('success') or result.get('mustChangePassword'):
                raise ValueError('Local panel authentication requires manual attention')
            self.token = result['token']
        return self.boot.local_api('/api/processes', token=self.token)['list']

    def attach(self, target):
        current = self.inspect()
        if current['qq'] != [target]:
            raise ValueError('QQ process changed before attach; stopped')
        self.boot.attach(target['pid'])

    def clock(self):
        return time.monotonic()

    def wait(self):
        time.sleep(3)


def run(env, limit=180):
    record = {'run_id': source.now().replace(':', '').replace('.', ''), 'started_at': source.now(),
              'status': 'starting', 'services_started': False, 'attach_requested': False,
              'qq_restarted': False, 'password_login_attempted': False}
    previous = env.previous()
    env.publish(record)
    try:
        if not env.authorized():
            raise ValueError('Windows logon attach is not authorized/enabled')
        info = env.inspect()
        record['boot'] = info['boot']
        record['attach_attempted_this_boot'] = bool(previous.get('boot') == info['boot'] and
            (previous.get('attach_attempted_this_boot') or previous.get('attach_requested')))
        present = verify_services(info, env.project)
        if present and env.health():
            record['status'] = 'already_healthy'
            return record
        if record['attach_attempted_this_boot']:
            record.update(target=previous.get('target'))
            raise ValueError('An attach was already attempted this boot; no automatic replay')
        if not present:
            env.start()
            record['services_started'] = True
            env.publish(record)
        deadline = env.clock() + limit
        while env.clock() < deadline:
            info = env.inspect()
            # Services started in this run may still be binding their ports.
            ports = {entry['port'] for entry in info['ports']}
            if not {3302, 35099} <= ports:
                env.wait()
                continue
            verify_services(info, env.project)
            if env.health():
                record['status'] = 'healthy'
                return record
            target = select_target(info, env.panel(), env.project)
            if target is not None:
                record.update(attach_requested=True, attach_attempted_this_boot=True,
                              target=target, status='attaching')
                # Persist BEFORE the native call so an uncertain result is never replayed.
                env.publish(record)
                env.attach(target)
                while env.clock() < deadline:
                    if env.health():
                        record['status'] = 'healthy'
                        return record
                    env.wait()
                break
            env.wait()
        raise ValueError('QQ/login/heartbeat not ready within bounded startup; manual attention required')
    except Exception as error:
        record.update(status='needs_attention', error_type=type(error).__name__,
                      reason=str(error) if isinstance(error, ValueError) else 'Local startup operation failed; inspect logs')
        return record
    finally:
        record['finished_at'] = source.now()
        env.publish(record)


@contextlib.contextmanager
def singleton():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.CreateMutexW(None, False, 'Local\\QQSnowLumaLogon-' + source.digest(str(PROJECT))[:24])
    if not handle:
        raise ValueError('Cannot create startup mutex')
    already_exists = ctypes.get_last_error() == 183
    try:
        yield not already_exists
    finally:
        kernel.CloseHandle(handle)


def main():
    with singleton() as acquired:
        if not acquired:
            return 4
        result = run(Environment())
        return 0 if result['status'] in {'healthy', 'already_healthy'} else 3


if __name__ == '__main__':
    raise SystemExit(main())
