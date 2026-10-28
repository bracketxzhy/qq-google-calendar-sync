"""Interactive first-install setup. Does not start/attach QQ or contact Google."""
import argparse
import importlib.util
import json
from pathlib import Path
import re
import settings
import pipeline


def initialize(config, scope, project=settings.PROJECT):
    folder = project/'.private'
    if folder.exists():
        raise ValueError('.private already exists; inspect it manually instead of overwriting')
    if not re.fullmatch(r'[1-9][0-9]{4,19}', config.get('qq_account', '')):
        raise ValueError('Enter your own numeric QQ account')
    if not config.get('calendar_id', '').strip():
        raise ValueError('An explicit destination calendar is required')
    if not Path(config.get('qq_path', '')).is_absolute() or not Path(config['qq_path']).is_file():
        raise ValueError('Choose an existing absolute QQ.exe path')
    if config.get('snowluma_terms_accepted') is not True or config.get('backend_cache_acknowledged') is not True:
        raise ValueError('Read and explicitly accept runtime terms and backend caching scope')
    for field, prefix in [('group_allowlist', 'group'), ('private_allowlist', 'user')]:
        if not isinstance(scope.get(field), list) or any(not re.fullmatch(prefix+r':[1-9][0-9]*', item) for item in scope[field]):
            raise ValueError('Invalid explicit conversation allowlist')
    if not scope['group_allowlist'] and not scope['private_allowlist']:
        raise ValueError('Choose at least one conversation to monitor')
    runtime = project/'runtime/snowluma-v2.1.1'
    if not all((runtime/name).is_file() for name in ('node.exe', 'index.mjs', 'EULA.md', 'PRIVACY.md')):
        raise ValueError('Install the official pinned runtime before setup')
    pipeline.save(folder/'config.json', config)
    pipeline.save(folder/'account-selection.json', scope)
    settings.load(project)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    project = settings.PROJECT
    runtime = project/'runtime/snowluma-v2.1.1'
    if not (runtime/'EULA.md').is_file():
        parser.error('Run python scripts/install-runtime.py before setup')
    print('Read runtime/snowluma-v2.1.1/EULA.md and PRIVACY.md before continuing.')
    print('The upstream runtime may passively cache contact metadata and other chats locally.')
    print('This receiver stores/analyzes only your allowlist. AI analysis sends selected content to Codex;')
    print('Google Calendar receives only concise event fields and a stable task marker.')
    terms = input('Accept upstream EULA and privacy policy? Type ACCEPT: ') == 'ACCEPT'
    cache = input('Acknowledge upstream passive caching scope? Type ACCEPT: ') == 'ACCEPT'
    if not (terms and cache):
        parser.error('Setup stopped without accepting terms or configuring QQ')
    config = {'qq_account': input('Your QQ number: ').strip(),
              'calendar_id': input('Google calendar ID (type primary to select your primary): ').strip(),
              'qq_path': input('Absolute path to your QQ.exe: ').strip().strip('"'),
              'snowluma_terms_accepted': terms, 'backend_cache_acknowledged': cache}
    def ids(prompt, prefix):
        raw = input(prompt).strip()
        if not raw:
            return []
        parts = [part.strip() for part in raw.split(',')]
        if any(not part.isdigit() or int(part) <= 0 for part in parts):
            raise ValueError('Conversation IDs must be comma-separated positive integers')
        return list(dict.fromkeys(prefix+':'+part for part in parts))
    scope = {'group_allowlist': ids('Allowed group IDs, comma separated (blank for none): ', 'group'),
             'private_allowlist': ids('Allowed private peer QQ IDs, comma separated (blank for none): ', 'user')}
    initialize(config, scope)
    spec = importlib.util.spec_from_file_location('bootstrap', project/'scripts/start-snowluma-monitor.py')
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    bootstrap.prepare()
    pipeline.save(project/'.private/snowluma-startup.json', {
        'enabled': False, 'authorization': 'explicit_local_setup', 'qq_restart': False, 'qq_password_login': False})
    print('Prepared fresh local configuration. No QQ attach, task registration, or Google write occurred.')
    print('Keep .private/, state/, and runtime/ private; never copy them into a public repository.')


if __name__ == '__main__':
    main()
