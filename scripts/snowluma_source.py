"""Local OneBot event storage only: no AI, QQ sends, history fetches or startup changes."""
from pathlib import Path
import argparse
import datetime as dt
import hashlib
import hmac
import http.server
import json
import os
import sqlite3
import time
import urllib.request

PROJECT = Path(__file__).resolve().parent.parent
import settings

def account(project=PROJECT):
    return settings.load(project)['qq_account']


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self,*args):return None


def local_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def encode(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(x):
    return hashlib.sha256(encode(x).encode()).hexdigest()


def policy(project=PROJECT):
    p = json.loads((project/'.private/message-source.json').read_text(encoding='utf-8'))
    if p['source'] != 'snowluma_push_new_only':
        raise ValueError('Explicit SnowLuma source policy required')
    if not p.get('authorization_question_id') or p.get('automatic_qq_recovery'):
        raise ValueError('Missing authorization or forbidden QQ recovery')
    return p


def sessions(project=PROJECT):
    policy(project)
    x = json.loads((project/'.private/account-selection.json').read_text(encoding='utf-8'))
    return set(x['group_allowlist']) | set(x['private_allowlist'])


def db_open(project=PROJECT):
    folder = project/'state/snowluma'
    folder.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(folder/'capture.sqlite3', timeout=10, factory=ClosingConnection)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA synchronous=FULL')
    c.executescript('''
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS current_event(key TEXT PRIMARY KEY,fingerprint TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS delivery(id INTEGER PRIMARY KEY AUTOINCREMENT,key TEXT NOT NULL,
      session TEXT NOT NULL,kind TEXT NOT NULL,native_seq TEXT NOT NULL,time INTEGER NOT NULL,
      sender TEXT NOT NULL,short_id TEXT NOT NULL,fingerprint TEXT NOT NULL,raw TEXT NOT NULL,received_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS quarantine(key TEXT PRIMARY KEY,session TEXT NOT NULL,
      reason TEXT NOT NULL,raw TEXT NOT NULL,received_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS gap(id INTEGER PRIMARY KEY AUTOINCREMENT,first_uncertain_at TEXT NOT NULL,
      detected_at TEXT NOT NULL,resumed_at TEXT,reason TEXT NOT NULL);
    ''')
    return c


def meta(c, key, default=None):
    r = c.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    return json.loads(r[0]) if r else default


def put(c, key, value):
    c.execute('INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
              (key, encode(value)))


def clean(x):
    if isinstance(x, dict):
        return {k: clean(v) for k,v in x.items() if k not in {'url','rkey','download_url','file_size'}}
    if isinstance(x, list):
        return [clean(v) for v in x]
    return x


def session_of(event, project=PROJECT):
    if event.get('message_type') == 'group':
        return 'group:' + str(event.get('group_id',0))
    if event.get('message_type') == 'private':
        sender = str(event.get('user_id',0))
        peer = str(event.get('target_id',0)) if sender == account(project) else sender
        return 'user:' + peer
    return None


def normalize(session, event, project=PROJECT):
    # This is the SnowLuma event contract, not a NapCat alias or a synthetic
    # short-ID mapping. Preserve the provider and sender-local sequence identity.
    seq = str(event.get('message_seq',''))
    sender = str(event.get('user_id',''))
    timestamp = event.get('time')
    if not seq.isdigit() or int(seq) <= 0 or not sender.isdigit() or int(sender) <= 0:
        raise ValueError('Missing native sequence or sender')
    if type(timestamp) is not int or timestamp <= 0 or timestamp > time.time()+300:
        raise ValueError('Invalid message time')
    if not isinstance(event.get('message'),list) or type(event.get('message_id')) is not int:
        raise ValueError('Unexpected native message payload')
    identity = {'provider':'snowluma','account':account(project),'session':session,
                'native_seq':seq,'time':timestamp,'sender':sender}
    return {'key':digest(identity),'session':session,'kind':session.split(':')[0],
            'native_seq':seq,'time':timestamp,'sender':sender,'short_id':str(event['message_id']),
            'fingerprint':digest({**identity,'message':clean(event['message'])}),'raw':encode(event)}


def ingest(event, project=PROJECT):
    p = policy(project)
    if str(event.get('self_id','')) != account(project):
        return 'wrong_account'
    with db_open(project) as c:
        if event.get('post_type') == 'meta_event':
            if event.get('meta_event_type') == 'heartbeat':
                good = event.get('status',{}).get('online') is True and event.get('status',{}).get('good') is True
                previous_heartbeat=meta(c,'heartbeat_at')
                at=now()
                if p.get('capture_start_epoch') is not None:
                    gap=c.execute('SELECT id FROM gap WHERE resumed_at IS NULL').fetchone()
                    if not good and not gap:
                        c.execute('INSERT INTO gap(first_uncertain_at,detected_at,reason) VALUES(?,?,?)',
                          (previous_heartbeat or p['capture_start_at'],at,'Backend heartbeat reports offline/unhealthy'))
                    elif good and gap:
                        c.execute('UPDATE gap SET resumed_at=? WHERE resumed_at IS NULL',(at,))
                put(c,'heartbeat_at',at); put(c,'backend_online',good)
            return 'metadata'
        if event.get('post_type') not in {'message','message_sent'}:
            return 'ignored_event'
        sid = session_of(event, project)
        # Drop other conversations before saving body, sender or peer metadata.
        if sid not in sessions(project):
            return 'outside_scope'
        floor = p.get('capture_start_epoch')
        if floor is None:
            return 'not_armed'
        if type(event.get('time')) is int and event['time'] < floor:
            return 'before_activation'
        try:
            row = normalize(sid,event,project)
        except ValueError as error:
            c.execute('INSERT OR IGNORE INTO quarantine VALUES(?,?,?,?,?)',
                      (digest(event),sid,str(error),encode(event),now()))
            return 'quarantined'
        prior = c.execute('SELECT fingerprint FROM current_event WHERE key=?',(row['key'],)).fetchone()
        if prior and prior[0] == row['fingerprint']:
            return 'duplicate'
        c.execute('INSERT INTO delivery(key,session,kind,native_seq,time,sender,short_id,fingerprint,raw,received_at) '
                  'VALUES(:key,:session,:kind,:native_seq,:time,:sender,:short_id,:fingerprint,:raw,:received_at)',
                  {**row,'received_at':now()})
        c.execute('INSERT INTO current_event VALUES(?,?) ON CONFLICT(key) DO UPDATE SET fingerprint=excluded.fingerprint',
                  (row['key'],row['fingerprint']))
        put(c,'last_message_at',now())
    return 'stored'


def api_status(project=PROJECT):
    secret = json.loads((project/'.private/snowluma-secrets.json').read_text(encoding='utf-8'))
    opener = local_opener()
    out = {}
    for action in ['get_login_info','get_status']:
        req = urllib.request.Request('http://127.0.0.1:3300/',data=encode({'action':action,'params':{}}).encode(),
            headers={'Content-Type':'application/json','Authorization':'Bearer '+secret['onebot_token']})
        with opener.open(req,timeout=5) as r:
            value=json.load(r)
        if value.get('retcode') != 0:
            raise ValueError('Local account/status request failed')
        out[action] = value['data']
    return str(out['get_login_info']['user_id']) == account(project) and out['get_status'].get('online') is True and out['get_status'].get('good') is True


def status(project=PROJECT, probe=True):
    p = policy(project)
    with db_open(project) as c:
        heartbeat=meta(c,'heartbeat_at'); online=meta(c,'backend_online',False)
        age=(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromisoformat(heartbeat)).total_seconds() if heartbeat else None
        started=meta(c,'receiver_started_at')
        current_receiver_heartbeat=heartbeat and (not started or dt.datetime.fromisoformat(heartbeat)>=dt.datetime.fromisoformat(started))
        healthy=online and age is not None and 0 <= age < 90 and current_receiver_heartbeat
        error=None
        if probe:
            try:
                opener=local_opener()
                with opener.open('http://127.0.0.1:3302/health',timeout=2) as response:
                    receiver_live=response.status==204
                healthy=healthy and receiver_live and api_status(project)
            except Exception as e: healthy=False;error=type(e).__name__
        if p.get('capture_start_epoch') is not None:
            gap=c.execute('SELECT id FROM gap WHERE resumed_at IS NULL').fetchone()
            if not healthy and not gap:
                put(c,'last_health_at',now())
                start=heartbeat or p['capture_start_at']
                c.execute('INSERT INTO gap(first_uncertain_at,detected_at,reason) VALUES(?,?,?)',
                          (start,now(),'Backend/receiver heartbeat unavailable; no history repair promised'))
            elif healthy and gap:
                c.execute('UPDATE gap SET resumed_at=? WHERE resumed_at IS NULL',(now(),))
        return {'checked_at':now(),'source':p['source'],'healthy':bool(healthy),'error_type':error,
                'receiver_pid':meta(c,'receiver_pid'),'heartbeat_at':heartbeat,'heartbeat_age_seconds':age,
                'capture_start_at':p.get('capture_start_at'),'capture_start_epoch':p.get('capture_start_epoch'),
                'messages':c.execute('SELECT count(*) FROM current_event').fetchone()[0],
                'delivery_versions':c.execute('SELECT count(*) FROM delivery').fetchone()[0],
                'quarantined':c.execute('SELECT count(*) FROM quarantine').fetchone()[0],
                'gaps':[dict(r) for r in c.execute('SELECT * FROM gap ORDER BY id')],
                'history_backfill':False,'automatic_qq_recovery':False}


def make_receiver(project=PROJECT, port=3302):
    secret=json.loads((project/'.private/snowluma-secrets.json').read_text(encoding='utf-8'))['push_token'].encode()
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def reply(self,code):
            # Empty non-JSON responses cannot trigger OneBot quick operations.
            self.send_response(code);self.send_header('Content-Length','0');self.end_headers()
        def do_GET(self): self.reply(204 if self.path=='/health' else 404)
        def do_POST(self):
            if self.path!='/events':self.reply(404);return
            try:
                length=int(self.headers.get('Content-Length','-1'))
                if not 0 < length <= 2*1024*1024:self.reply(413);return
                self.connection.settimeout(10)
                raw=self.rfile.read(length)
                expected='sha1='+hmac.new(secret,raw,hashlib.sha1).hexdigest()
                if not hmac.compare_digest(self.headers.get('X-Signature',''),expected):self.reply(401);return
                if self.headers.get('X-Self-ID')!=account(project):self.reply(403);return
                event=json.loads(raw)
                if not isinstance(event,dict):self.reply(400);return
                ingest(event,project)
            except (ValueError,UnicodeError):self.reply(400);return
            except Exception:
                # No body, credentials or chats are emitted into console/error logs.
                self.reply(503);return
            self.reply(204)
    server=http.server.ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.daemon_threads=True
    with db_open(project) as c:
        put(c,'receiver_pid',os.getpid());put(c,'receiver_started_at',now())
    return server


def receiver(project=PROJECT, port=3302):
    with make_receiver(project,port) as server:
        server.serve_forever()


def accepted_types(project=PROJECT):
    path=project/'state/snowluma/acceptance.json'
    if not path.exists():return set()
    a=json.loads(path.read_text(encoding='utf-8'))
    result=set()
    with db_open(project) as c:
        for kind,evidence in a.items():
            if kind in {'group','user'} and evidence.get('source_question_id') and evidence.get('user_confirmed') is True:
                if c.execute('SELECT 1 FROM delivery WHERE key=? AND kind=?',(evidence.get('event_key'),kind)).fetchone():
                    result.add(kind)
    return result


def confirm_natural(evidence, project=PROJECT):
    if not evidence.get('source_question_id') or evidence.get('user_confirmed') is not True or not evidence.get('user_answer'):
        raise ValueError('Explicit user comparison answer and question ID required')
    with db_open(project) as c:
        row=c.execute('SELECT key,kind,session,time,sender,received_at FROM delivery WHERE key=? ORDER BY id LIMIT 1',
                      (evidence.get('event_key'),)).fetchone()
        if not row:raise ValueError('Cannot verify an uncaptured or synthetic production event')
    path=project/'state/snowluma/acceptance.json'
    prior=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    prior[row['kind']]={**evidence,**dict(row),'verified_at':now()}
    # The user answer refers to an actual stored source; never create an event
    # or a verification by toggling a standalone readiness flag.
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(prior,ensure_ascii=False,indent=2)+'\n',encoding='utf-8');os.replace(temp,path)
    return {'verified_kind':row['kind'],'event_key':row['key']}


def task_ready(task, db, project=PROJECT):
    kinds=set();new_source=False
    for key in task['sources']:
        row=db.execute('SELECT session FROM message WHERE key=?',(key,)).fetchone()
        if not row or row[0] not in sessions(project):return False
        if db.execute('SELECT 1 FROM message_source WHERE key=? AND provider=?',(key,'snowluma')).fetchone():
            kinds.add(row[0].split(':')[0]);new_source=True
    return new_source and kinds <= accepted_types(project)


def collect(project, pipeline):
    run_id=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    folder=project/'state/pipeline/runs'/run_id
    report={'id':run_id,'started':now(),'source':'snowluma_push_new_only','status':'collecting','sessions':{},'failures':{}}
    pipeline.save(folder/'result.json',report);pipeline.save(project/'state/pipeline/last-attempt.json',report)
    try:
        health=status(project)
        report['capture']=health
        if not health['healthy']:raise ValueError('Push receiver/backend is offline; this is not no new messages')
        allowed=sessions(project);verified=accepted_types(project)
        with db_open(project) as spool, pipeline.open_db(project) as db:
            db.executescript('CREATE TABLE IF NOT EXISTS provider_cursor(provider TEXT PRIMARY KEY,last_delivery INTEGER NOT NULL);'
                             'CREATE TABLE IF NOT EXISTS message_source(key TEXT PRIMARY KEY,provider TEXT NOT NULL,received_at TEXT NOT NULL);')
            prior=db.execute('SELECT last_delivery FROM provider_cursor WHERE provider=?',('snowluma',)).fetchone()
            cursor=prior[0] if prior else 0
            upper=spool.execute('SELECT coalesce(max(id),0) FROM delivery').fetchone()[0]
            for sid in allowed:
                report['sessions'][sid]={'new_or_changed':0,'source':'durable_push','new_only':True,
                                         'reception_verified':sid.split(':')[0] in verified}
            for r in spool.execute('SELECT * FROM delivery WHERE id>? AND id<=? ORDER BY id',(cursor,upper)):
                if r['session'] not in allowed:continue
                existing=db.execute('SELECT fingerprint FROM message WHERE key=?',(r['key'],)).fetchone()
                if existing is None or existing[0]!=r['fingerprint']:
                    report['sessions'][r['session']]['new_or_changed']+=1
                db.execute('INSERT INTO message(key,session,short_id,native_seq,time,sender,fingerprint,raw,last_run) '
                    'VALUES(:key,:session,:short_id,:native_seq,:time,:sender,:fingerprint,:raw,:run) '
                    'ON CONFLICT(key) DO UPDATE SET fingerprint=excluded.fingerprint,raw=excluded.raw,last_run=excluded.last_run',
                    {**dict(r),'run':run_id})
                db.execute('INSERT INTO message_source VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET received_at=excluded.received_at',
                           (r['key'],'snowluma',r['received_at']))
            # Commit the import cursor in the SAME transaction as the imported
            # sources: restart/replay cannot skip or duplicate committed rows.
            db.execute('INSERT INTO provider_cursor VALUES(?,?) ON CONFLICT(provider) DO UPDATE SET last_delivery=excluded.last_delivery',('snowluma',upper))
            report['import_cursor']=upper
            report['verified_types']=sorted(verified)
            report['status']='collected_push_ready' if verified=={'group','user'} else 'collected_push_unverified'
            if health['quarantined']:report['failures']['quarantine']='Selected events require schema review'
            report['history_completeness_claimed']=False
            db.execute('INSERT INTO run VALUES(?,?,?,?)',(run_id,report['started'],report['status'],pipeline.encode(report)))
    except Exception as e:
        report.update(status='failed',error=str(e) if isinstance(e,ValueError) else type(e).__name__)
        pipeline.save(folder/'result.json',report);pipeline.save(project/'state/pipeline/last-attempt.json',report)
        raise
    pipeline.save(folder/'result.json',report)
    pipeline.save(project/'state/pipeline/last-attempt.json',report)
    pipeline.save(project/'state/pipeline/last-collection.json',report)
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['receive','status','confirm-natural'])
    parser.add_argument('--file',type=Path)
    args=parser.parse_args()
    if args.command=='receive':receiver()
    elif args.command=='status':print(json.dumps(status(),ensure_ascii=False))
    else:print(json.dumps(confirm_natural(json.loads(args.file.read_text(encoding='utf-8')),PROJECT),ensure_ascii=False))
