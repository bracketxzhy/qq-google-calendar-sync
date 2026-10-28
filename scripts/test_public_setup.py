"""Fresh-install configuration isolation and irreversible-state protection checks."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import settings
import pipeline
import snowluma_source
import setup


class PublicSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        executable = self.root/'QQ.exe'
        executable.touch()
        self.config = {'qq_account': '123456789', 'calendar_id': 'primary', 'qq_path': str(executable),
                       'snowluma_terms_accepted': True, 'backend_cache_acknowledged': True}
        self.scope = {'group_allowlist': ['group:100'], 'private_allowlist': []}
        runtime = self.root/'runtime/snowluma-v2.1.1'
        runtime.mkdir(parents=True)
        for name in ('node.exe', 'index.mjs', 'EULA.md', 'PRIVACY.md'):
            (runtime/name).touch()

    def test_missing_account_configuration_fails_without_using_original_identity(self):
        with self.assertRaisesRegex(ValueError, 'Missing'):
            snowluma_source.account(self.root)

    def test_two_installations_use_distinct_accounts_and_calendar_destinations(self):
        setup.initialize(self.config, self.scope, self.root)
        self.assertEqual('123456789', snowluma_source.account(self.root))
        self.assertEqual('primary', settings.load(self.root)['calendar_id'])
        other = self.root/'other'
        pipeline.save(other/'.private/config.json', {**self.config, 'qq_account': '987654321', 'calendar_id': 'example-calendar'})
        self.assertEqual('987654321', snowluma_source.account(other))
        self.assertEqual('example-calendar', settings.load(other)['calendar_id'])
        self.assertEqual('123456789', snowluma_source.account(self.root))

    def test_setup_cannot_overwrite_existing_secrets(self):
        setup.initialize(self.config, self.scope, self.root)
        marker = self.root/'.private/secret-marker'
        marker.write_text('synthetic-existing-install', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'already exists'):
            setup.initialize(self.config, self.scope, self.root)
        self.assertEqual('synthetic-existing-install', marker.read_text(encoding='utf-8'))

    def test_unacknowledged_backend_cache_or_empty_scope_creates_no_private_files(self):
        with self.assertRaises(ValueError):
            setup.initialize({**self.config, 'backend_cache_acknowledged': False}, self.scope, self.root)
        self.assertFalse((self.root/'.private').exists())
        with self.assertRaises(ValueError):
            setup.initialize(self.config, {'group_allowlist': [], 'private_allowlist': []}, self.root)
        self.assertFalse((self.root/'.private').exists())

    def test_prepare_uses_current_account_and_refuses_to_reset_epoch_or_tokens(self):
        setup.initialize(self.config, self.scope, self.root)
        spec = importlib.util.spec_from_file_location('test_bootstrap', Path(__file__).with_name('start-snowluma-monitor.py'))
        bootstrap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bootstrap)
        bootstrap.PROJECT = self.root
        bootstrap.RUNTIME = self.root/'runtime/snowluma-v2.1.1'
        bootstrap.prepare()
        self.assertTrue((bootstrap.RUNTIME/'config/onebot_123456789.json').is_file())
        secrets = (self.root/'.private/snowluma-secrets.json').read_bytes()
        policy = json.loads((self.root/'.private/message-source.json').read_text(encoding='utf-8'))
        self.assertIsNone(policy['capture_start_epoch'])
        self.assertEqual(2, policy['analysis_interval_hours'])
        self.assertFalse(policy['automatic_qq_recovery'])
        with self.assertRaisesRegex(ValueError, 'already exists'):
            bootstrap.prepare()
        self.assertEqual(secrets, (self.root/'.private/snowluma-secrets.json').read_bytes())


if __name__ == '__main__':
    unittest.main()
