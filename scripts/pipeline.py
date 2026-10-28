"""Durable QQ collection/task/outbox data utilities. No AI runtime or QQ writes.

AI review and Google Calendar tools are supplied by the existing Codex chat.
Only durable SnowLuma push deliveries are imported; historical source evidence is retained.
"""
from pathlib import Path
import argparse
import contextlib
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
from types import SimpleNamespace
PROJECT = Path(__file__).resolve().parent.parent
import settings

def provider(project=PROJECT):
    config = json.loads((project / '.private/message-source.json').read_text(encoding='utf-8'))
    if config.get('source') != 'snowluma_push_new_only':
        raise ValueError('Only the authorized SnowLuma new-message source is supported')
    return config['source']

class ClosingConnection(sqlite3.Connection):

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()

def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temp, path)

def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()

def open_db(project=PROJECT):
    state = project / 'state/pipeline'
    state.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(state / 'pipeline.sqlite3', factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('\n    CREATE TABLE IF NOT EXISTS coverage(session TEXT PRIMARY KEY, floor INTEGER NOT NULL,\n      high INTEGER, last_run TEXT, phone_verified INTEGER NOT NULL DEFAULT 0);\n    CREATE TABLE IF NOT EXISTS message(key TEXT PRIMARY KEY, session TEXT NOT NULL,\n      short_id TEXT NOT NULL, native_seq TEXT NOT NULL, time INTEGER NOT NULL,\n      sender TEXT NOT NULL, fingerprint TEXT NOT NULL, raw TEXT NOT NULL,\n      reviewed_fingerprint TEXT, last_run TEXT NOT NULL);\n    CREATE TABLE IF NOT EXISTS task(id TEXT PRIMARY KEY, revision INTEGER NOT NULL,\n      body TEXT NOT NULL, fingerprint TEXT NOT NULL, event_id TEXT,\n      applied_revision INTEGER NOT NULL DEFAULT 0, applied_payload TEXT);\n    CREATE TABLE IF NOT EXISTS task_revision(task_id TEXT, revision INTEGER, body TEXT,\n      fingerprint TEXT, recorded_at TEXT, PRIMARY KEY(task_id,revision));\n    CREATE TABLE IF NOT EXISTS operation(id TEXT PRIMARY KEY, task_id TEXT NOT NULL,\n      revision INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,\n      status TEXT NOT NULL, event_id TEXT, evidence TEXT,\n      FOREIGN KEY(task_id) REFERENCES task(id));\n    CREATE TABLE IF NOT EXISTS run(id TEXT PRIMARY KEY, started TEXT, status TEXT, report TEXT);\n    CREATE TABLE IF NOT EXISTS provider_cursor(provider TEXT PRIMARY KEY,last_delivery INTEGER NOT NULL);\n    CREATE TABLE IF NOT EXISTS message_source(key TEXT PRIMARY KEY,provider TEXT NOT NULL,received_at TEXT NOT NULL);\n    ')
    return db

@contextlib.contextmanager
def exclusive(project=PROJECT):
    if os.name != 'nt':
        with _file_exclusive(project):
            yield
        return
    import ctypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    name = 'Local\\QQPipelineWriter-' + digest(str(project.resolve()).casefold())[:24]
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise ValueError('Cannot create pipeline writer mutex')
    acquired = False
    try:
        acquired = kernel.WaitForSingleObject(handle, 0) in (0, 128)
        if not acquired:
            raise ValueError('Another pipeline writer is still running')
        with _file_exclusive(project):
            yield
    finally:
        if acquired:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)

@contextlib.contextmanager
def _file_exclusive(project=PROJECT):
    path = project / 'state/pipeline/write.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        owner = int(path.read_text(encoding='utf-8'))
        if os.name == 'nt':
            import ctypes
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(4096, False, owner)
            if handle:
                kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
                exit_code = ctypes.c_ulong()
                success = kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                kernel.CloseHandle(handle)
                if not success or exit_code.value == 259:
                    raise ValueError('Another pipeline writer is still running')
            elif ctypes.get_last_error() != 87:
                raise ValueError('Cannot verify lock owner; refusing recovery')
        else:
            try:
                os.kill(owner, 0)
            except ProcessLookupError:
                pass
            else:
                raise ValueError('Another pipeline writer is still running')
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        path.rename(path.with_name('write.stale-' + stamp + '.lock'))
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode())
        yield
    finally:
        os.close(fd)
        path.unlink()

def scope_sessions(project):
    provider(project)
    import snowluma_source
    return snowluma_source.sessions(project)

def collect(project=PROJECT):
    with exclusive(project):
        provider(project)
        import snowluma_source
        return snowluma_source.collect(project, SimpleNamespace(save=save, open_db=open_db, encode=encode))

def review_input(project=PROJECT):
    with open_db(project) as db:
        allowed = scope_sessions(project)
        latest = json.loads((project / 'state/pipeline/last-attempt.json').read_text(encoding='utf-8'))
        if latest.get('source') != 'snowluma_push_new_only' or latest.get('status') not in {'collected_push_ready', 'collected_push_unverified'}:
            return []
        return [dict(r) | {'raw': json.loads(r['raw'])} for r in db.execute("SELECT m.* FROM message m JOIN message_source s ON s.key=m.key WHERE s.provider='snowluma' AND (m.reviewed_fingerprint IS NULL OR m.reviewed_fingerprint<>m.fingerprint) ORDER BY m.time,m.native_seq") if r['session'] in allowed]

def validate_event(event):
    if event is None:
        return
    if event.get('timezone_str') != 'Asia/Shanghai':
        raise ValueError('Timezone must be Asia/Shanghai')
    if set(event) - {'title', 'description', 'location', 'start_date', 'end_date', 'start_time', 'end_time', 'timezone_str'}:
        raise ValueError('Unexpected Calendar fields')
    if not event.get('title'):
        raise ValueError('Missing event title')
    timed = bool(event.get('start_time') or event.get('end_time'))
    all_day = bool(event.get('start_date') or event.get('end_date'))
    if timed == all_day:
        raise ValueError('Use exactly one date representation')
    if timed:
        start, end = (dt.datetime.fromisoformat(event[k]) for k in ('start_time', 'end_time'))
        if start.utcoffset() != dt.timedelta(hours=8) or end.utcoffset() != dt.timedelta(hours=8) or end <= start:
            raise ValueError('Invalid Shanghai time boundaries')
    elif dt.date.fromisoformat(event['end_date']) <= dt.date.fromisoformat(event['start_date']):
        raise ValueError('Invalid exclusive all-day end')

def apply_review(review, project=PROJECT):
    """Validate Codex's structured analysis atomically; never call a model/API."""
    allowed = scope_sessions(project)
    with exclusive(project), open_db(project) as db:
        source_rows = {}
        for item in review['reviewed']:
            row = db.execute('SELECT * FROM message WHERE key=?', (item['key'],)).fetchone()
            if not row or row['session'] not in allowed or row['fingerprint'] != item['fingerprint']:
                raise ValueError('Analysis source changed or does not exist')
            source_rows[row['key']] = row
        for task in review['tasks']:
            if task['status'] not in {'pending', 'active', 'cancelled', 'dismissed', 'completed'} or not task['sources']:
                raise ValueError('Invalid task status/evidence')
            prior = db.execute('SELECT * FROM task WHERE id=?', (task['id'],)).fetchone()
            confirmed = review.get('user_confirmation', {})
            prior_task = json.loads(prior['body']) if prior else {}
            if prior_task.get('calendar_sync_paused') and (not task.get('calendar_sync_paused')):
                if not confirmed.get('source_question_id') or not task.get('calendar_resume_user_confirmed'):
                    raise ValueError('User-paused Calendar sync cannot be resumed automatically')
            if prior_task.get('status') == 'dismissed' and task['status'] != 'dismissed' and (not confirmed.get('source_question_id')):
                raise ValueError('User-dismissed task cannot be reactivated automatically')
            if prior_task.get('status') == 'completed' and task['status'] != 'completed' and (not confirmed.get('source_question_id')):
                raise ValueError('User-completed task cannot be reactivated automatically')
            if task['status'] == 'completed' and prior_task.get('status') != 'completed':
                if not prior or not confirmed.get('source_question_id') or (not task.get('completion_explicit')):
                    raise ValueError('Completion requires an existing task and explicit user confirmation')
            historical_dismissal = task['status'] == 'dismissed' and prior is not None and confirmed.get('source_question_id') and task.get('dismissal_explicit') and (set(task['sources']) == set(prior_task['sources']))
            if task['status'] == 'dismissed' and (not historical_dismissal or prior['event_id']):
                raise ValueError('Dismissal needs explicit user confirmation and no existing Calendar event')
            if task.get('calendar_sync_paused'):
                if not prior_task.get('calendar_sync_paused') and (not confirmed.get('source_question_id') or not task.get('calendar_pause_user_confirmed')):
                    raise ValueError('Pausing Calendar sync requires explicit user confirmation')
                if db.execute("SELECT 1 FROM operation WHERE task_id=? AND status IN ('inflight','uncertain')", (task['id'],)).fetchone():
                    raise ValueError('Reconcile uncertain Calendar writes before pausing')
            for key in task['sources']:
                source = db.execute('SELECT session FROM message WHERE key=?', (key,)).fetchone()
                if not source or (source['session'] not in allowed and (not historical_dismissal)):
                    raise ValueError('Task source is excluded from monitoring')
                if key not in source_rows:
                    if not db.execute('SELECT 1 FROM message WHERE key=? AND reviewed_fingerprint=fingerprint', (key,)).fetchone():
                        raise ValueError('Task has unreviewed or unknown source')
            validate_event(task.get('event'))
            if task['status'] == 'active' and (not task.get('date_explicit') or not task.get('personally_required') or (not task.get('event'))):
                raise ValueError('Unconfirmed task cannot become a definite event')
            if task['status'] == 'cancelled' and (not task.get('cancellation_explicit')):
                raise ValueError('Cancellation requires explicit evidence')
            key = task['id']
            if len(key) != 24 or any((c not in '0123456789abcdef' for c in key)):
                raise ValueError('Task ID must be a stable 24-character digest')
            fingerprint = digest(task)
            revision = prior['revision'] + 1 if prior and prior['fingerprint'] != fingerprint else prior['revision'] if prior else 1
            if task['status'] == 'cancelled' and prior is None:
                raise ValueError('Cannot cancel an unknown task')
            db.execute('INSERT INTO task(id,revision,body,fingerprint) VALUES(?,?,?,?)\n                ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,body=excluded.body,fingerprint=excluded.fingerprint', (key, revision, encode(task), fingerprint))
            db.execute('INSERT OR IGNORE INTO task_revision VALUES(?,?,?,?,?)', (key, revision, encode(task), fingerprint, now()))
            if task.get('calendar_sync_paused'):
                db.execute("UPDATE operation SET status='user_deferred' WHERE task_id=? AND status='planned'", (key,))
        for row in source_rows.values():
            db.execute('UPDATE message SET reviewed_fingerprint=? WHERE key=?', (row['fingerprint'], row['key']))
    return {'reviewed': len(source_rows), 'tasks_submitted': len(review['tasks'])}

def calendar_plan(project=PROJECT):
    allowed = scope_sessions(project)
    with exclusive(project), open_db(project) as db:
        import snowluma_source
        if not snowluma_source.status(project)['healthy']:
            raise ValueError('Push source offline; Calendar planning blocked')
        if db.execute("SELECT 1 FROM operation WHERE status IN ('inflight','uncertain')").fetchone():
            raise ValueError('Reconcile existing in-flight Calendar operation before planning')
        output = []
        for row in db.execute('SELECT * FROM task ORDER BY id').fetchall():
            task = json.loads(row['body'])
            if not snowluma_source.task_ready(task, db, project):
                continue
            sources = [db.execute('SELECT session FROM message WHERE key=?', (key,)).fetchone() for key in task['sources']]
            if any((not source or source['session'] not in allowed for source in sources)):
                continue
            if task['status'] in {'pending', 'dismissed', 'completed'} or task.get('calendar_sync_paused') or row['applied_revision'] == row['revision']:
                continue
            kind = 'delete' if task['status'] == 'cancelled' else 'update' if row['event_id'] else 'create'
            if kind == 'delete' and (not row['event_id']):
                db.execute('UPDATE task SET applied_revision=? WHERE id=?', (row['revision'], row['id']))
                continue
            event = dict(task.get('event') or {})
            marker = 'qqtask:' + row['id']
            if kind != 'delete':
                event['description'] = (event.get('description', '') + '\n' + marker).strip()
                event.update(calendar_id=settings.load(project)['calendar_id'], visibility='private', transparency='transparent', reminders={'use_default': False, 'overrides': []})
            op_id = digest([row['id'], row['revision'], kind])[:32]
            payload = {'event': event, 'event_id': row['event_id'], 'calendar_id': settings.load(project)['calendar_id'], 'marker': marker, 'previous_payload': json.loads(row['applied_payload']) if row['applied_payload'] else None}
            db.execute("UPDATE operation SET status='superseded' WHERE task_id=? AND revision<>? AND status='planned'", (row['id'], row['revision']))
            db.execute('INSERT OR IGNORE INTO operation VALUES(?,?,?,?,?,?,?,?)', (op_id, row['id'], row['revision'], kind, encode(payload), 'planned', row['event_id'], None))
            output.append({'operation_id': op_id, 'task_id': row['id'], 'revision': row['revision'], 'kind': kind, **payload})
        return output

def verify_owned(op_id, event, project=PROJECT):
    """Check managed fields before update/delete; preserve user edits by stopping."""
    with exclusive(project), open_db(project) as db:
        row = db.execute('SELECT * FROM operation WHERE id=?', (op_id,)).fetchone()
        if not row or row['kind'] not in {'update', 'delete'} or row['status'] != 'planned':
            raise ValueError('Expected planned update or cancellation')
        db.execute('UPDATE operation SET evidence=NULL WHERE id=?', (op_id,))
        db.commit()
        payload = json.loads(row['payload'])
        if event.get('id') != row['event_id'] or event.get('status') != 'confirmed' or payload['marker'] not in event.get('description', ''):
            raise ValueError('Event is missing, cancelled, or no longer has the ownership marker')
        expected = payload['previous_payload']
        if not expected:
            raise ValueError('No verified prior Calendar snapshot')
        start = expected.get('start_time') or expected['start_date'] + 'T00:00:00'
        end = expected.get('end_time') or expected['end_date'] + 'T00:00:00'
        fields = {'summary': expected['title'], 'start': start, 'end': end, 'visibility': expected['visibility'], 'transparency': expected['transparency'], 'reminders': expected['reminders'], 'location': expected.get('location')}
        if any((event.get(k) != v for k, v in fields.items())):
            raise ValueError('Calendar was manually changed; review conflict before writing')
        if ''.join(event.get('description', '').split()) != ''.join(expected.get('description', '').split()):
            raise ValueError('Calendar description changed; preserve user notes')
        if any((not attendee.get('is_self', False) for attendee in event.get('attendees', []))):
            raise ValueError('Event has external attendees; no automatic changes')
        db.execute('UPDATE operation SET evidence=? WHERE id=?', (encode({'ownership_verified_at': now(), 'event': event}), op_id))
    return {'operation_id': op_id, 'owned_event_unchanged': True}

def summary(project=PROJECT):
    with open_db(project) as db:
        tasks = [dict(r) for r in db.execute('SELECT id,revision,body,event_id,applied_revision FROM task')]
        return {'checked_at': now(), 'messages': db.execute('SELECT count(*) FROM message').fetchone()[0], 'unreviewed': db.execute('SELECT count(*) FROM message WHERE reviewed_fingerprint IS NULL OR reviewed_fingerprint<>fingerprint').fetchone()[0], 'coverage': [dict(r) for r in db.execute('SELECT * FROM coverage')], 'tasks': [{'id': r['id'], 'title': json.loads(r['body'])['title'], 'status': json.loads(r['body'])['status'], 'revision': r['revision'], 'applied_revision': r['applied_revision'], 'event_id': r['event_id']} for r in tasks], 'operations': [dict(r) for r in db.execute('SELECT id,kind,status,event_id FROM operation')], 'sqlite_integrity': db.execute('pragma integrity_check').fetchone()[0]}

def task_context(project=PROJECT):
    allowed = scope_sessions(project)
    with open_db(project) as db:
        return [{'revision': r['revision'], 'event_id': r['event_id'], 'applied_revision': r['applied_revision'], 'task': json.loads(r['body'])} for r in db.execute('SELECT * FROM task ORDER BY id') if all(((source := db.execute('SELECT session FROM message WHERE key=?', (key,)).fetchone()) and source['session'] in allowed for key in json.loads(r['body'])['sources']))]

def write_report(report, project=PROJECT):
    latest = json.loads((project / 'state/pipeline/last-attempt.json').read_text(encoding='utf-8'))
    if report['collection_run'] != latest['id']:
        raise ValueError('Report must refer to this collection attempt')
    limits = ['New messages since capture activation; no history completeness claim; uncertain intervals recorded', 'Fixed user-maintained group/private allowlists; automatic group-helper classification unavailable', 'Group announcements disabled']
    report = {**report, 'recorded_at': now(), 'state': summary(project), 'source_limits': limits}
    target = project / 'state/pipeline/reports' / f"{latest['id']}.json"
    if target.exists():
        raise ValueError('Run report already exists; immutable evidence cannot be overwritten')
    save(target, report)
    date = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date().isoformat()
    reports = []
    for file in (project / 'state/pipeline/reports').glob('*.json'):
        item = json.loads(file.read_text(encoding='utf-8'))
        if dt.datetime.fromisoformat(item['recorded_at']).astimezone(dt.timezone(dt.timedelta(hours=8))).date().isoformat() == date:
            reports.append(item)
    save(project / 'state/pipeline/daily' / f'{date}.json', {'date': date, 'timezone': 'Asia/Shanghai', 'runs': reports})
    return {'report_written': True, 'daily_runs': len(reports), 'collection_run': latest['id']}

def redact_for_review(value):
    if isinstance(value, dict):
        return {k: redact_for_review(v) for k, v in value.items() if k not in {'token', 'rkey', 'url', 'download_url', 'jumpUrl', 'preview', 'tagIcon', 'avatar'}}
    if isinstance(value, list):
        return [redact_for_review(v) for v in value]
    if isinstance(value, str) and value.startswith('{'):
        try:
            return redact_for_review(json.loads(value))
        except ValueError:
            pass
    return value

def operation_start(op_id, project=PROJECT):
    with exclusive(project), open_db(project) as db:
        row = db.execute('SELECT * FROM operation WHERE id=?', (op_id,)).fetchone()
        if not row or row['status'] != 'planned':
            raise ValueError('Operation is not planned; reconcile instead of replaying')
        if row['kind'] in {'update', 'delete'}:
            evidence = json.loads(row['evidence'] or '{}')
            checked = evidence.get('ownership_verified_at')
            if not checked or not 0 <= (dt.datetime.fromisoformat(now()) - dt.datetime.fromisoformat(checked)).total_seconds() <= 300:
                raise ValueError('Fresh verify-owned check required before Calendar write')
        if db.execute('SELECT revision FROM task WHERE id=?', (row['task_id'],)).fetchone()[0] != row['revision']:
            raise ValueError('Stale task revision; refresh Calendar plan')
        task = json.loads(db.execute('SELECT body FROM task WHERE id=?', (row['task_id'],)).fetchone()[0])
        allowed = scope_sessions(project)
        import snowluma_source
        if not snowluma_source.status(project)['healthy'] or not snowluma_source.task_ready(task, db, project):
            raise ValueError('Fresh online and independently verified push source required')
        for key in task['sources']:
            source = db.execute('SELECT session FROM message WHERE key=?', (key,)).fetchone()
            if not source or source['session'] not in allowed:
                raise ValueError('Operation source is no longer monitored')
        db.execute("UPDATE operation SET status='inflight' WHERE id=?", (op_id,))
    return {'operation_id': op_id, 'status': 'inflight'}

def operation_finish(op_id, event, project=PROJECT):
    with exclusive(project), open_db(project) as db:
        row = db.execute('SELECT * FROM operation WHERE id=?', (op_id,)).fetchone()
        if not row or row['status'] != 'inflight':
            raise ValueError('No matching in-flight operation')
        payload = json.loads(row['payload'])
        if row['kind'] == 'delete':
            if event.get('status') != 'cancelled' or event.get('id') != row['event_id']:
                raise ValueError('Cancellation not verified')
        else:
            expected = payload['event']
            if event.get('status') != 'confirmed' or not event.get('id'):
                raise ValueError('Created/updated event is not confirmed')
            if payload['marker'] not in event.get('description', '') or event.get('summary') != expected['title']:
                raise ValueError('Event identity/title verification failed')
            if ' '.join(event.get('description', '').split()) != ' '.join(expected.get('description', '').split()) or (event.get('location') or '') != (expected.get('location') or ''):
                raise ValueError('Event description/location verification failed')
            if row['event_id'] and event.get('id') != row['event_id']:
                raise ValueError('Event ID changed on update')
            if event.get('visibility') != 'private' or event.get('transparency') != 'transparent' or event.get('reminders') != expected['reminders']:
                raise ValueError('Event privacy/reminders verification failed')
            if any((not attendee.get('is_self', False) for attendee in event.get('attendees', []))):
                raise ValueError('Unexpected external attendee')
            if expected.get('start_date'):
                if event.get('start') != expected['start_date'] + 'T00:00:00' or event.get('end') != expected['end_date'] + 'T00:00:00':
                    raise ValueError('All-day boundaries do not match')
            elif event.get('start') != expected['start_time'] or event.get('end') != expected['end_time']:
                raise ValueError('Timed boundaries do not match')
        db.execute("UPDATE operation SET status='confirmed',event_id=?,evidence=? WHERE id=?", (event['id'], encode(event), op_id))
        db.execute('UPDATE task SET event_id=?,applied_revision=?,applied_payload=? WHERE id=?', (None if row['kind'] == 'delete' else event['id'], row['revision'], encode(payload['event']), row['task_id']))
    return {'operation_id': op_id, 'status': 'confirmed', 'event_id': event['id']}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['collect', 'review-input', 'task-context', 'apply-review', 'calendar-plan', 'verify-owned', 'operation-start', 'operation-finish', 'summary', 'write-report'])
    parser.add_argument('--file', type=Path)
    parser.add_argument('--operation')
    args = parser.parse_args()
    if args.command in {'apply-review', 'write-report', 'verify-owned', 'operation-finish'} and args.file is None:
        parser.error(f'{args.command} requires --file <JSON file>; create the input file before running this command')
    if args.command in {'verify-owned', 'operation-start', 'operation-finish'} and not args.operation:
        parser.error(f'{args.command} requires --operation <operation ID>')
    if args.command == 'collect':
        result = collect()
    elif args.command == 'review-input':
        rows = review_input()
        path = PROJECT / 'state/pipeline/review-input.json'
        save(path, redact_for_review(rows))
        result = {'unreviewed': len(rows), 'review_file': str(path)}
    elif args.command == 'apply-review':
        result = apply_review(json.loads(args.file.read_text(encoding='utf-8')))
    elif args.command == 'calendar-plan':
        result = calendar_plan()
    elif args.command == 'summary':
        result = summary()
    elif args.command == 'task-context':
        result = task_context()
    elif args.command == 'write-report':
        result = write_report(json.loads(args.file.read_text(encoding='utf-8')))
    elif args.command == 'verify-owned':
        result = verify_owned(args.operation, json.loads(args.file.read_text(encoding='utf-8')))
    elif args.command == 'operation-start':
        result = operation_start(args.operation)
    else:
        result = operation_finish(args.operation, json.loads(args.file.read_text(encoding='utf-8')))
    print(json.dumps(result, ensure_ascii=False, indent=2))
if __name__ == '__main__':
    main()
