"""Offline acceptance tests. Synthetic messages/events; never touches QQ/Google."""
import snowluma_source as s
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
spec = importlib.util.spec_from_file_location('pipeline', Path(__file__).with_name('pipeline.py'))
p = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p)

class WorkflowTest(unittest.TestCase):

    def test_calendar_paragraph_rendering_preserves_description_text(self):
        review = self.review()
        review['tasks'][0]['event']['description'] = '必要提醒。'
        p.apply_review(review, self.root)
        op = p.calendar_plan(self.root)[0]
        p.operation_start(op['operation_id'], self.root)
        event = self.event(op)
        event['description'] = event['description'].replace('\n', ' ') + '\n\n'
        self.assertEqual(p.operation_finish(op['operation_id'], event, self.root)['status'], 'confirmed')

    def test_readback_rejects_missing_notes_or_wrong_location(self):
        review = self.review()
        review['tasks'][0]['event'].update(description='必要提醒', location='S204')
        p.apply_review(review, self.root)
        op = p.calendar_plan(self.root)[0]
        p.operation_start(op['operation_id'], self.root)
        event = self.event(op)
        for changes in [{'location': 'wrong'}, {'location': 'S204', 'description': op['marker']}]:
            with self.assertRaisesRegex(ValueError, 'description/location'):
                p.operation_finish(op['operation_id'], {**event, **changes}, self.root)
        with p.open_db(self.root) as db:
            self.assertEqual('inflight', db.execute('SELECT status FROM operation').fetchone()[0])
        p.operation_finish(op['operation_id'], {**event, 'location': 'S204'}, self.root)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        p.save(self.root/'.private/config.json', {'qq_account':'123456789', 'calendar_id':'primary', 'qq_path':r'C:\Example\QQ.exe', 'snowluma_terms_accepted':True, 'backend_cache_acknowledged':True})
        self.health_patch = patch.object(s, 'status', return_value={'healthy': True, 'quarantined': 0, 'gaps': []})
        self.health_patch.start()
        self.addCleanup(self.health_patch.stop)
        p.save(self.root / '.private/message-source.json', {'source': 'snowluma_push_new_only', 'authorization_question_id': 'synthetic-authorization', 'automatic_qq_recovery': False, 'capture_start_epoch': 100, 'capture_start_at': s.now()})
        p.save(self.root / '.private/account-selection.json', {'group_allowlist': ['group:100'], 'private_allowlist': ['user:200']})
        event = {'self_id': int(s.account(self.root)), 'post_type': 'message', 'message_type': 'group', 'group_id': 100, 'time': 101, 'user_id': 200, 'message_seq': 10, 'message_id': 123, 'message': [{'type': 'text', 'data': {'text': 'synthetic task fixture'}}], 'sender': {'user_id': 200}}
        s.ingest(event, self.root)
        p.collect(self.root)
        self.source = p.review_input(self.root)[0]
        s.confirm_natural({'event_key': self.source['key'], 'source_question_id': 'synthetic-local-test', 'user_answer': 'synthetic-confirmation', 'user_confirmed': True}, self.root)

    def tearDown(self):
        self.temp.cleanup()

    def review(self, status='active', end='2026-10-24'):
        return {'reviewed': [{'key': self.source['key'], 'fingerprint': self.source['fingerprint']}], 'tasks': [{'id': 'a' * 24, 'status': status, 'title': '作业提交', 'sources': [self.source['key']], 'date_explicit': True, 'personally_required': True, 'cancellation_explicit': status == 'cancelled', 'event': {'title': '作业提交', 'start_date': '2026-10-23', 'end_date': end, 'timezone_str': 'Asia/Shanghai'}}]}

    def event(self, operation, event_id='synthetic-event'):
        e = operation['event']
        return {'id': event_id, 'status': 'confirmed', 'summary': e['title'], 'description': e['description'], 'start': e['start_date'] + 'T00:00:00', 'end': e['end_date'] + 'T00:00:00', 'visibility': 'private', 'transparency': 'transparent', 'reminders': e['reminders'], 'attendees': []}

    def test_duplicate_review_and_calendar_plan_are_idempotent(self):
        review = self.review()
        p.apply_review(review, self.root)
        p.apply_review(review, self.root)
        one = p.calendar_plan(self.root)
        two = p.calendar_plan(self.root)
        self.assertEqual(one, two)
        p.operation_start(one[0]['operation_id'], self.root)
        p.operation_finish(one[0]['operation_id'], self.event(one[0]), self.root)
        self.assertEqual([], p.calendar_plan(self.root))
        with p.open_db(self.root) as db:
            self.assertEqual(1, db.execute('SELECT revision FROM task').fetchone()[0])

    def test_calendar_description_keeps_provenance_only_in_local_ledger(self):
        review = self.review()
        review['tasks'][0]['event']['description'] = '提交作业。'
        p.apply_review(review, self.root)
        planned = p.calendar_plan(self.root)[0]
        self.assertEqual('提交作业。\nqqtask:' + 'a' * 24, planned['event']['description'])
        with p.open_db(self.root) as db:
            task = json.loads(db.execute('SELECT body FROM task').fetchone()[0])
            self.assertEqual([self.source['key']], task['sources'])
            self.assertIsNotNone(db.execute('SELECT session,time,sender,raw FROM message WHERE key=?', (self.source['key'],)).fetchone())

    def test_user_completion_preserves_existing_calendar_event(self):
        review = self.review()
        p.apply_review(review, self.root)
        op = p.calendar_plan(self.root)[0]
        p.operation_start(op['operation_id'], self.root)
        p.operation_finish(op['operation_id'], self.event(op), self.root)
        review['user_confirmation'] = {'source_question_id': 'synthetic-completion-answer'}
        review['tasks'][0].update(status='completed', completion_explicit=True)
        p.apply_review(review, self.root)
        self.assertEqual([], p.calendar_plan(self.root))
        with p.open_db(self.root) as db:
            self.assertEqual('synthetic-event', db.execute('SELECT event_id FROM task').fetchone()[0])
        review.pop('user_confirmation')
        review['tasks'][0]['status'] = 'active'
        with self.assertRaisesRegex(ValueError, 'cannot be reactivated automatically'):
            p.apply_review(review, self.root)

    def test_user_dismissal_of_historical_excluded_task_creates_no_calendar_action(self):
        review = self.review(status='pending')
        p.apply_review(review, self.root)
        p.save(self.root / '.private/account-selection.json', {'group_allowlist': [], 'private_allowlist': []})
        review['reviewed'] = []
        review['user_confirmation'] = {'source_question_id': 'synthetic-user-answer'}
        review['tasks'][0].update(status='dismissed', dismissal_explicit=True, event=None, personally_required=False)
        p.apply_review(review, self.root)
        self.assertEqual([], p.calendar_plan(self.root))
        with p.open_db(self.root) as db:
            self.assertEqual('dismissed', json.loads(db.execute('SELECT body FROM task').fetchone()[0])['status'])

    def test_user_calendar_pause_preserves_event_and_blocks_pending_update(self):
        review = self.review()
        p.apply_review(review, self.root)
        op = p.calendar_plan(self.root)[0]
        p.operation_start(op['operation_id'], self.root)
        p.operation_finish(op['operation_id'], self.event(op), self.root)
        review['tasks'][0]['event']['description'] = 'New local note'
        p.apply_review(review, self.root)
        planned = p.calendar_plan(self.root)[0]
        review['user_confirmation'] = {'source_question_id': 'synthetic-user-answer'}
        review['tasks'][0].update(calendar_sync_paused=True, calendar_pause_user_confirmed=True)
        p.apply_review(review, self.root)
        self.assertEqual([], p.calendar_plan(self.root))
        with p.open_db(self.root) as db:
            self.assertEqual('synthetic-event', db.execute('SELECT event_id FROM task').fetchone()[0])
            self.assertEqual('user_deferred', db.execute('SELECT status FROM operation WHERE id=?', (planned['operation_id'],)).fetchone()[0])
        review.pop('user_confirmation')
        review['tasks'][0]['notes'] = ['New local source detail while preserving the existing user pause']
        p.apply_review(review, self.root)
        self.assertEqual([], p.calendar_plan(self.root))
        review['tasks'][0].pop('calendar_sync_paused')
        with self.assertRaisesRegex(ValueError, 'cannot be resumed automatically'):
            p.apply_review(review, self.root)

    def test_existing_calendar_task_cannot_be_dismissed_as_if_no_event_exists(self):
        review = self.review()
        p.apply_review(review, self.root)
        op = p.calendar_plan(self.root)[0]
        p.operation_start(op['operation_id'], self.root)
        p.operation_finish(op['operation_id'], self.event(op), self.root)
        review['user_confirmation'] = {'source_question_id': 'synthetic-user-answer'}
        review['tasks'][0].update(status='dismissed', dismissal_explicit=True)
        with self.assertRaisesRegex(ValueError, 'no existing Calendar event'):
            p.apply_review(review, self.root)

    def test_update_and_cancel_reuse_event_id(self):
        p.apply_review(self.review(), self.root)
        one = p.calendar_plan(self.root)[0]
        p.operation_start(one['operation_id'], self.root)
        p.operation_finish(one['operation_id'], self.event(one), self.root)
        p.apply_review(self.review(end='2026-10-25'), self.root)
        two = p.calendar_plan(self.root)[0]
        self.assertEqual('update', two['kind'])
        self.assertEqual('synthetic-event', two['event_id'])
        with self.assertRaisesRegex(ValueError, 'verify-owned'):
            p.operation_start(two['operation_id'], self.root)
        p.verify_owned(two['operation_id'], self.event(one), self.root)
        p.operation_start(two['operation_id'], self.root)
        p.operation_finish(two['operation_id'], self.event(two), self.root)
        p.apply_review(self.review(status='cancelled', end='2026-10-25'), self.root)
        three = p.calendar_plan(self.root)[0]
        self.assertEqual('delete', three['kind'])
        self.assertEqual('synthetic-event', three['event_id'])
        p.verify_owned(three['operation_id'], self.event(two), self.root)
        p.operation_start(three['operation_id'], self.root)
        p.operation_finish(three['operation_id'], {'id': 'synthetic-event', 'status': 'cancelled'}, self.root)
        self.assertEqual([], p.calendar_plan(self.root))

    def test_inflight_operation_blocks_replay(self):
        p.apply_review(self.review(), self.root)
        op = p.calendar_plan(self.root)[0]
        p.operation_start(op['operation_id'], self.root)
        with self.assertRaises(ValueError):
            p.calendar_plan(self.root)
        with self.assertRaises(ValueError):
            p.operation_start(op['operation_id'], self.root)

    def test_changed_source_and_uncertain_dates_cannot_be_committed(self):
        review = self.review()
        review['reviewed'][0]['fingerprint'] = 'stale'
        with self.assertRaises(ValueError):
            p.apply_review(review, self.root)
        review = self.review()
        review['tasks'][0]['date_explicit'] = False
        with self.assertRaises(ValueError):
            p.apply_review(review, self.root)
        with p.open_db(self.root) as db:
            self.assertEqual(0, db.execute('SELECT count(*) FROM task').fetchone()[0])
            self.assertIsNone(db.execute('SELECT reviewed_fingerprint FROM message').fetchone()[0])

    def test_unconfirmed_tasks_do_not_create_events(self):
        review = self.review(status='pending')
        review['tasks'][0].update(date_explicit=False, personally_required=False, event=None)
        p.apply_review(review, self.root)
        self.assertEqual([], p.calendar_plan(self.root))

    def test_stale_plan_and_external_invite_rejected(self):
        p.apply_review(self.review(), self.root)
        old = p.calendar_plan(self.root)[0]
        p.apply_review(self.review(end='2026-10-25'), self.root)
        with self.assertRaises(ValueError):
            p.operation_start(old['operation_id'], self.root)
        op = p.calendar_plan(self.root)[0]
        p.operation_start(op['operation_id'], self.root)
        event = self.event(op)
        event['attendees'] = [{'is_self': False}]
        with self.assertRaises(ValueError):
            p.operation_finish(op['operation_id'], event, self.root)

    def test_calendar_manual_edits_preserved_before_update(self):
        p.apply_review(self.review(), self.root)
        one = p.calendar_plan(self.root)[0]
        event = self.event(one)
        p.operation_start(one['operation_id'], self.root)
        p.operation_finish(one['operation_id'], event, self.root)
        p.apply_review(self.review(end='2026-10-25'), self.root)
        two = p.calendar_plan(self.root)[0]
        self.assertTrue(p.verify_owned(two['operation_id'], event, self.root)['owned_event_unchanged'])
        event['description'] += ' user note'
        with self.assertRaisesRegex(ValueError, 'notes'):
            p.verify_owned(two['operation_id'], event, self.root)
        with self.assertRaisesRegex(ValueError, 'verify-owned'):
            p.operation_start(two['operation_id'], self.root)

    def test_scope_change_blocks_old_review_and_calendar_plan(self):
        p.apply_review(self.review(), self.root)
        p.save(self.root / '.private/account-selection.json', {'group_allowlist': [], 'private_allowlist': ['user:200']})
        self.assertEqual([], p.review_input(self.root))
        with self.assertRaises(ValueError):
            p.apply_review(self.review(), self.root)
        self.assertEqual([], p.calendar_plan(self.root))

    def test_dead_writer_lock_is_archived_and_live_writer_is_not_stolen(self):
        import subprocess, sys, os
        child = subprocess.Popen([sys.executable, '-c', 'pass'])
        child.wait()
        lock = self.root / 'state/pipeline/write.lock'
        lock.write_text(str(child.pid), encoding='utf-8')
        with p.exclusive(self.root):
            self.assertEqual(str(os.getpid()), lock.read_text())
            with self.assertRaises(ValueError):
                with p.exclusive(self.root):
                    pass
        self.assertFalse(lock.exists())
        self.assertEqual(1, len(list(lock.parent.glob('write.stale-*.lock'))))

    def test_run_report_is_immutable(self):
        p.save(self.root / 'state/pipeline/last-attempt.json', {'id': 'fixture-run', 'status': 'partial'})
        report = {'collection_run': 'fixture-run', 'status': 'partial', 'failures': ['fixture']}
        p.write_report(report, self.root)
        with self.assertRaises(ValueError):
            p.write_report(report, self.root)
        data = json.loads((self.root / 'state/pipeline/reports/fixture-run.json').read_text(encoding='utf-8'))
        self.assertEqual('partial', data['status'])

    def test_report_cli_rejects_missing_input_without_traceback(self):
        import subprocess, sys
        result = subprocess.run([sys.executable, str(Path(p.__file__)), 'write-report'],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn('requires --file', result.stderr)
        self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_report_cli_persists_supplied_run_and_daily_report(self):
        import subprocess, sys
        script = self.root/'scripts/pipeline.py'
        script.parent.mkdir(exist_ok=True)
        script.write_text(Path(p.__file__).read_text(encoding='utf-8'), encoding='utf-8')
        (script.parent/'settings.py').write_text(Path(p.__file__).with_name('settings.py').read_text(encoding='utf-8'), encoding='utf-8')
        p.save(self.root/'state/pipeline/last-attempt.json', {'id':'cli-fixture'})
        source = self.root/'state/pipeline/runs/cli-fixture/report-input.json'
        p.save(source, {'collection_run':'cli-fixture','status':'no_changes','new_tasks':[]})
        result = subprocess.run([sys.executable, str(script), 'write-report', '--file', str(source)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads((self.root/'state/pipeline/reports/cli-fixture.json').read_text(encoding='utf-8'))
        self.assertEqual(report['collection_run'], 'cli-fixture')
        self.assertEqual(report['new_tasks'], [])
        daily = list((self.root/'state/pipeline/daily').glob('*.json'))
        self.assertEqual(len(daily), 1)
        self.assertTrue(any(r['collection_run']=='cli-fixture' for r in json.loads(daily[0].read_text(encoding='utf-8'))['runs']))
if __name__ == '__main__':
    unittest.main(verbosity=2)
