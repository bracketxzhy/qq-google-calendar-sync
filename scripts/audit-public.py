"""Audit exact Git index blobs before publication; prints locations, never secrets."""
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

PROJECT = Path(__file__).resolve().parent.parent
ALLOWED_ROOT_FILES = {'.gitignore', 'README.md', 'AGENTS.md', 'LICENSE', 'SECURITY.md',
                      'THIRD_PARTY_NOTICES.md', 'runtime.lock.json', 'VALIDATION.md'}
ALLOWED_FOLDERS = {'scripts', 'docs', 'examples', '.github'}
FORBIDDEN_PARTS = {'.private', 'state', 'runtime', 'work', 'outputs', 'dist', '.codex',
                   '__pycache__', '.venv', 'venv', 'logs', 'data', 'node_modules'}
FORBIDDEN_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.log', '.pem', '.key', '.pyc', '.zip', '.exe', '.dll', '.node'}
RULES = {
    'private-key': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----'),
    'github-token': re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})\b'),
    'api-key': re.compile(r'\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}\b'),
    'google-token': re.compile(r'\bya29\.[A-Za-z0-9_-]{20,}'),
    'user-home': re.compile(r'(?i)[A-Z]:[\\/]Users[\\/](?!Public\b|Example\b|YOUR_USER\b)[^\s\x22\x27<>]+'),
    'original-authorization': re.compile(r'call_[a-f0-9]{24,}|01a[0-9a-f]{5}-[0-9a-f-]{27,}'),
}
EMAIL = re.compile(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}')


def audit(project=PROJECT):
    names = subprocess.check_output(['git', 'ls-files', '-z'], cwd=project).decode('utf-8').split('\0')
    names = [name for name in names if name]
    if not names:
        raise ValueError('No staged/tracked files; stage the public source first')
    issues = []
    for name in names:
        path = PurePosixPath(name)
        if any(part in FORBIDDEN_PARTS for part in path.parts) or path.suffix.lower() in FORBIDDEN_SUFFIXES:
            issues.append({'file': name, 'rule': 'private-or-runtime-file'})
        if len(path.parts) == 1 and name not in ALLOWED_ROOT_FILES:
            issues.append({'file': name, 'rule': 'unapproved-root-file'})
        if len(path.parts) > 1 and path.parts[0] not in ALLOWED_FOLDERS:
            issues.append({'file': name, 'rule': 'unapproved-directory'})
        raw = subprocess.check_output(['git', 'show', ':'+name], cwd=project)
        try:
            content = raw.decode('utf-8')
        except UnicodeDecodeError:
            issues.append({'file': name, 'rule': 'non-text-file'})
            continue
        for line_number, line in enumerate(content.splitlines(), 1):
            for rule, pattern in RULES.items():
                if pattern.search(line):
                    issues.append({'file': name, 'line': line_number, 'rule': rule})
            for address in EMAIL.findall(line):
                domain = address.rsplit('@', 1)[1].lower()
                if domain not in {'example.com', 'example.org', 'users.noreply.github.com'}:
                    issues.append({'file': name, 'line': line_number, 'rule': 'non-example-email'})
    return {'files_checked': len(names), 'passed': not issues, 'issues': issues}


if __name__ == '__main__':
    result = audit()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['passed'] else 1)
