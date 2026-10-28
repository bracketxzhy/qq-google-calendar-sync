"""Bounded logon state-machine checks. No real QQ, startup tasks or services."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

import snowluma_startup as startup


def info(ports=True):
    return {'boot': 'synthetic-boot', 'qq': [{'pid': 123, 'created': 'synthetic-time', 'path': r'C:\Example\QQ.exe'}],
            'ports': ([{'port': 3302, 'host': '127.0.0.1', 'pid': 200,
                        'path': str(Path(startup.sys.executable).with_name('pythonw.exe')), 'receiver': True},
                       {'port': 35099, 'host': '127.0.0.1', 'pid': 300,
                        'path': str(startup.PROJECT/'runtime/snowluma-v2.1.1/node.exe'), 'receiver': False}]
                      if ports else [])}


class FakeEnvironment:
    project = startup.PROJECT

    def __init__(self, present=True, healthy=False):
        self.current = info(present)
        self.is_healthy = healthy
        self.prior = {}
        self.records = []
        self.starts = self.attaches = 0
        self.elapsed = 0
        self.allowed = True
        self.panel_items = [{'pid': 123, 'uin': '0', 'status': 'available'}]

    def authorized(self): return self.allowed
    def previous(self): return copy.deepcopy(self.prior)
    def publish(self, record): self.records.append(copy.deepcopy(record))
    def inspect(self): return copy.deepcopy(self.current)
    def health(self): return self.is_healthy
    def start(self):
        self.starts += 1
        self.current['ports'] = info()['ports']
    def panel(self): return self.panel_items
    def attach(self, target):
        self.attaches += 1
        assert self.records[-1]['attach_requested']
        self.is_healthy = True
    def clock(self): return self.elapsed
    def wait(self): self.elapsed += 3


class StartupTests(unittest.TestCase):
    def setUp(self):
        fake = {'qq_account':'123456789','calendar_id':'primary','qq_path':r'C:\Example\QQ.exe',
                'snowluma_terms_accepted':True,'backend_cache_acknowledged':True}
        config_patch = patch.object(startup.settings, 'load', return_value=fake)
        config_patch.start()
        self.addCleanup(config_patch.stop)

    def test_healthy_services_are_reused_without_start_or_attach(self):
        e = FakeEnvironment(healthy=True)
        self.assertEqual(startup.run(e)['status'], 'already_healthy')
        self.assertEqual((e.starts, e.attaches), (0, 0))

    def test_cold_logon_starts_services_and_attaches_exactly_once(self):
        e = FakeEnvironment(present=False)
        r = startup.run(e)
        self.assertEqual(r['status'], 'healthy')
        self.assertEqual((e.starts, e.attaches), (1, 1))
        self.assertFalse(r['qq_restarted'])
        self.assertFalse(r['password_login_attempted'])

    def test_no_qq_times_out_without_launching_or_attaching(self):
        e = FakeEnvironment(); e.current['qq'] = []
        self.assertEqual(startup.run(e, 9)['status'], 'needs_attention')
        self.assertEqual(e.attaches, 0)
        self.assertEqual(e.elapsed, 9)

    def test_multiple_qq_main_processes_stop_before_attach(self):
        e = FakeEnvironment(); e.current['qq'] *= 2
        self.assertEqual(startup.run(e)['status'], 'needs_attention')
        self.assertEqual(e.attaches, 0)

    def test_other_account_is_not_attached(self):
        e = FakeEnvironment(); e.panel_items[0]['uin'] = '999'
        self.assertEqual(startup.run(e)['status'], 'needs_attention')
        self.assertEqual(e.attaches, 0)

    def test_unsafe_listener_stops_before_start_or_attach(self):
        for changes in [{'host': '0.0.0.0'}, {'path': r'C:\other\pythonw.exe'}, {'receiver': False}]:
            e = FakeEnvironment(); e.current['ports'][0].update(changes)
            self.assertEqual(startup.run(e)['status'], 'needs_attention')
            self.assertEqual((e.starts, e.attaches), (0, 0))

    def test_partial_services_are_not_killed_or_restarted(self):
        e = FakeEnvironment(); e.current['ports'].pop()
        self.assertEqual(startup.run(e)['status'], 'needs_attention')
        self.assertEqual((e.starts, e.attaches), (0, 0))

    def test_uncertain_attach_is_not_replayed_on_same_boot(self):
        e = FakeEnvironment()
        e.prior = {'boot': 'synthetic-boot', 'attach_requested': True, 'target': e.current['qq'][0]}
        for _ in range(2):
            r = startup.run(e)
            self.assertEqual(r['status'], 'needs_attention')
            self.assertTrue(r['attach_attempted_this_boot'])
            e.prior = r
        self.assertEqual(e.attaches, 0)

    def test_healthy_duplicate_run_preserves_boot_attempt_marker(self):
        e = FakeEnvironment()
        first = startup.run(e)
        self.assertTrue(first['attach_requested'])
        e.prior = first
        healthy = startup.run(e)
        self.assertEqual(healthy['status'], 'already_healthy')
        self.assertFalse(healthy['attach_requested'])
        self.assertTrue(healthy['attach_attempted_this_boot'])
        e.prior = healthy
        e.is_healthy = False
        self.assertEqual(startup.run(e)['status'], 'needs_attention')
        self.assertEqual(e.attaches, 1)

    def test_new_boot_can_attach_once_after_previous_boot(self):
        e = FakeEnvironment(); e.prior = {'boot': 'old-boot', 'attach_requested': True}
        self.assertEqual(startup.run(e)['status'], 'healthy')
        self.assertEqual(e.attaches, 1)

    def test_online_pending_heartbeat_does_not_load_again(self):
        e = FakeEnvironment(); e.panel_items[0].update(uin='123456789', status='online')
        self.assertEqual(startup.run(e, 6)['status'], 'needs_attention')
        self.assertEqual(e.attaches, 0)

    def test_disabled_authorization_never_starts_or_attaches(self):
        e = FakeEnvironment(); e.allowed = False
        self.assertEqual(startup.run(e)['status'], 'needs_attention')
        self.assertEqual((e.starts, e.attaches), (0, 0))

    def test_attach_failure_is_recorded_without_retry(self):
        e = FakeEnvironment()
        with patch.object(e, 'attach', side_effect=ValueError('Synthetic native attach failure')) as attach:
            r = startup.run(e)
        self.assertEqual(attach.call_count, 1)
        self.assertTrue(r['attach_requested'])
        self.assertEqual(r['status'], 'needs_attention')


if __name__ == '__main__':
    unittest.main()
