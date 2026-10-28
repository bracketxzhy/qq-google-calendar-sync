"""Start the authorized receiver and attach once to an existing QQ.
Called manually or by the separately authorized bounded logon bootstrap.
Never launches/restarts QQ or changes Windows startup/Taskbar/Codex settings.
"""
from pathlib import Path
import argparse
import datetime as dt
import json
import os
import secrets
import socket
import subprocess
import time
import urllib.request
import settings
import pipeline
import snowluma_source as source

PROJECT=Path(__file__).resolve().parent.parent
RUNTIME=PROJECT/'runtime/snowluma-v2.1.1'
PANEL='http://127.0.0.1:35099'


def local_api(route,body=None,token=None):
    headers={'Content-Type':'application/json','Origin':PANEL}
    if token:headers['Authorization']='Bearer '+token
    request=urllib.request.Request(PANEL+route,data=None if body is None else source.encode(body).encode(),headers=headers)
    with source.local_opener().open(request,timeout=20) as r:return json.load(r)


def prepare():
    settings.load(PROJECT)
    destination=PROJECT/'.private/message-source.json'
    secret_path=PROJECT/'.private/snowluma-secrets.json'
    if destination.exists() or secret_path.exists():
        raise ValueError('Configuration already exists; refusing to reset epoch or rotate secrets')
    if not (RUNTIME/'node.exe').is_file() or not (RUNTIME/'index.mjs').is_file():
        raise ValueError('Install the pinned official runtime before preparing')
    cfg=settings.load(PROJECT)
    if cfg['snowluma_terms_accepted'] is not True or cfg['backend_cache_acknowledged'] is not True:
        raise ValueError('Read the runtime EULA/privacy and acknowledge backend passive caching first')
    policy={'source':'snowluma_push_new_only','authorization_question_id':'local-setup',
       'authorization_answer':'Explicit local setup by this installation operator',
       'capture_start_epoch':None,'capture_start_at':None,
       'automatic_qq_recovery':False,'history_backfill':False,'analysis_interval_hours':2,
       'scope_policy':'user-maintained group/private allowlists; backend passive cache acknowledged',
       'group_helper_classification':'user-maintained; no automatic classification'}
    pipeline.save(destination,policy)
    secret={'webui_password':secrets.token_urlsafe(32),'onebot_token':secrets.token_urlsafe(32),'push_token':secrets.token_urlsafe(32)}
    pipeline.save(secret_path,secret)
    config=RUNTIME/'config'
    config.mkdir(parents=True,exist_ok=True)
    base={'mode':'snapshot','networks':{'httpServers':[],'httpClients':[],'wsServers':[],'wsClients':[]},
          'statusCommand':{'enabled':False,'swallow':False,'cooldownSeconds':5,'trigger':'#sl'},
          'historySync':{'enabled':False},'notifications':{'channelIds':[]}}
    pipeline.save(config/'onebot.json',base)
    own=json.loads(json.dumps(base))
    own['networks']['httpServers']=[{'name':'local-status','host':'127.0.0.1','port':3300,'path':'/',
      'enableWebSocket':False,'accessToken':secret['onebot_token'],'messageFormat':'array','reportSelfMessage':True}]
    own['networks']['httpClients']=[{'name':'durable-local-capture','url':'http://127.0.0.1:3302/events',
      'accessToken':secret['push_token'],'messageFormat':'array','reportSelfMessage':True,'timeoutMs':5000}]
    pipeline.save(config/f"onebot_{source.account(PROJECT)}.json",own)
    pipeline.save(config/'runtime.json',{'hookAutoLoad':False,'webuiHost':'127.0.0.1','webuiPort':35099,
       'logMaxTotalMb':32,'logRetainDays':1,'logPerUin':False})
    print('Prepared authorized new-message-only source. No capture epoch or QQ attach yet.')


def start():
    source.policy(PROJECT)
    cfg=settings.load(PROJECT)
    if cfg['snowluma_terms_accepted'] is not True or cfg['backend_cache_acknowledged'] is not True:
        raise ValueError('Explicit terms and backend cache acknowledgement required')
    for port in [3300,3302,35099]:
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1',port))==0:
                raise ValueError('Required local port is already occupied; no alternate port or restart')
    folder=PROJECT/'state/snowluma';folder.mkdir(parents=True,exist_ok=True)
    with open(folder/'receiver-errors.log','ab',buffering=0) as log:
        receiver=subprocess.Popen([str(Path(os.sys.executable).with_name('pythonw.exe')),
          str(PROJECT/'scripts/snowluma_source.py'),'receive'],cwd=PROJECT,stdin=subprocess.DEVNULL,
          stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
    pipeline.save(folder/'receiver-process.json',{'pid':receiver.pid,'started_at':source.now()})
    for _ in range(30):
        try:
            with source.local_opener().open('http://127.0.0.1:3302/health',timeout=1) as response:
                if response.status==204:break
        except OSError:time.sleep(.2)
    else:raise ValueError('Receiver did not start; QQ has not been attached')
    secret=json.loads((PROJECT/'.private/snowluma-secrets.json').read_text(encoding='utf-8'))
    env=os.environ.copy();env.update({'SNOWLUMA_HOOK_AUTOLOAD':'0','SNOWLUMA_TELEMETRY':'0',
      'SNOWLUMA_ACCEPT_EULA':'1','SNOWLUMA_ACCEPT_PRIVACY':'1','SNOWLUMA_WEBUI_BOOTSTRAP_PASSWORD':secret['webui_password']})
    with open(folder/'runtime-console.log','ab',buffering=0) as log:
        node=subprocess.Popen([str(RUNTIME/'node.exe'),str(RUNTIME/'index.mjs')],cwd=RUNTIME,env=env,
          stdin=subprocess.DEVNULL,stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW)
    pipeline.save(folder/'runtime-process.json',{'supervisor_pid':node.pid,'started_at':source.now(),'automatic_attach':False})
    print(json.dumps({'receiver_pid':receiver.pid,'snowluma_supervisor_pid':node.pid,'qq_attached':False}))


def attach(pid):
    secret=json.loads((PROJECT/'.private/snowluma-secrets.json').read_text(encoding='utf-8'))
    login=local_api('/api/login',{'password':secret['webui_password']})
    if not login.get('success') or login.get('mustChangePassword'):raise ValueError('Local panel authentication failed')
    token=login['token']
    process=[p for p in local_api('/api/processes',token=token)['list'] if p['pid']==pid]
    if len(process)!=1 or str(process[0].get('uin')) not in {'0',source.account(PROJECT)}:
        raise ValueError('Target process not uniquely identified or account differs')
    if process[0].get('status')=='online' and str(process[0].get('uin'))==source.account(PROJECT):
        print('Existing process already connected; no second native attach requested.');return
    if process[0].get('status')!='available':
        raise ValueError('Existing attach is pending or abnormal; no automatic repeat')
    own=local_api('/api/config/'+source.account(PROJECT),token=token)['config']
    if own['statusCommand']['enabled'] or own['historySync']['enabled']:
        raise ValueError('Unsafe automatic action configuration')
    if len(own['networks']['httpClients'])!=1 or own['networks']['httpClients'][0]['url']!='http://127.0.0.1:3302/events':
        raise ValueError('Push endpoint mismatch')
    current=json.loads(subprocess.check_output(['powershell.exe','-NoProfile','-Command',
      f"Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" | Select-Object ProcessId,ExecutablePath,ParentProcessId | ConvertTo-Json"],text=True,creationflags=subprocess.CREATE_NO_WINDOW))
    if current['ExecutablePath'].casefold()!=settings.load(PROJECT)['qq_path'].casefold():raise ValueError('QQ path mismatch')
    policy=source.policy(PROJECT)
    if policy['capture_start_epoch'] is None:
        policy['capture_start_at']=source.now();policy['capture_start_epoch']=int(time.time())
        pipeline.save(PROJECT/'.private/message-source.json',policy)
    # The receiver is already listening and armed before the native attach,
    # so the first legitimate event can be persisted immediately.
    result=local_api('/api/processes/'+str(pid)+'/load',{},token)
    evidence={'at':source.now(),'target_pid':pid,'response':result}
    pipeline.save(PROJECT/'state/snowluma/attach-result.json',evidence)
    record=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    pipeline.save(PROJECT/'state/snowluma/attach-attempts'/f'{record}.json',evidence)
    deadline=time.monotonic()+12
    while time.monotonic()<deadline:
        try:
            if source.api_status(PROJECT):break
        except (OSError,ValueError):pass
        time.sleep(.3)
    else:
        local_api('/api/processes/'+str(pid)+'/unload',{},token)
        raise ValueError('Account/online verification failed; native attach was unloaded without QQ restart')
    print(json.dumps({'attach_success':result.get('success'),'status':result.get('process',{}).get('status'),
       'capture_start_at':policy['capture_start_at'],'target_pid':pid}))


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('command',choices=['prepare','start','attach'])
    parser.add_argument('--pid',type=int);args=parser.parse_args()
    if args.command=='prepare':prepare()
    elif args.command=='start':start()
    else:
        if not args.pid:raise ValueError('An explicitly verified QQ PID is required')
        attach(args.pid)
