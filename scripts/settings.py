"""Explicit per-installation configuration. No account or machine defaults."""
import json
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent


def load(project=PROJECT):
    path = project/'.private/config.json'
    if not path.is_file():
        raise ValueError('Missing .private/config.json; run scripts/setup.py first')
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value.get('qq_account'), str) or not value['qq_account'].isdigit() or int(value['qq_account']) <= 0:
        raise ValueError('qq_account must be your numeric account string')
    if not isinstance(value.get('calendar_id'), str) or not value['calendar_id'].strip():
        raise ValueError('calendar_id must explicitly identify the destination calendar')
    if not isinstance(value.get('qq_path'), str) or not Path(value['qq_path']).is_absolute():
        raise ValueError('qq_path must be an absolute path to your QQ.exe')
    for name in ('snowluma_terms_accepted', 'backend_cache_acknowledged'):
        if type(value.get(name)) is not bool:
            raise ValueError(f'{name} must explicitly be true or false')
    return value
