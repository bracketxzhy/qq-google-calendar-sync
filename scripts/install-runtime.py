"""Fetch only the pinned official release, verify SHA-256, and safely unpack."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import urllib.request
import zipfile

PROJECT = Path(__file__).resolve().parent.parent


def install(project=PROJECT, archive=None):
    pin = json.loads((project/'runtime.lock.json').read_text(encoding='utf-8'))
    destination = project/pin['directory']
    if destination.exists():
        raise ValueError('Runtime already exists; refusing to overwrite configuration or login state')
    (project/'runtime').mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=project/'runtime', prefix='install-') as temp:
        root = Path(temp)
        if archive is None:
            archive = root/'runtime.zip'
            request = urllib.request.Request(pin['url'], headers={'User-Agent': 'qq-google-calendar-sync'})
            with urllib.request.urlopen(request, timeout=120) as response, archive.open('wb') as output:
                shutil.copyfileobj(response, output)
        archive = Path(archive)
        with archive.open('rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        if checksum != pin['sha256']:
            raise ValueError('Runtime SHA-256 mismatch; nothing was installed')
        extracted = root/'extracted'
        extracted.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            if bundle.testzip() is not None:
                raise ValueError('Damaged runtime ZIP')
            for member in bundle.infolist():
                target = (extracted/member.filename).resolve()
                if not target.is_relative_to(extracted.resolve()) or (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError('Unsafe runtime archive member')
            bundle.extractall(extracted)
        candidates = [path.parent for path in extracted.rglob('index.mjs') if (path.parent/'node.exe').is_file()]
        if len(candidates) != 1:
            raise ValueError('Unexpected official archive layout')
        source = candidates[0]
        package = json.loads((source/'package.json').read_text(encoding='utf-8'))
        if package.get('name') != '@snowluma/runtime' or package.get('version') != pin['version']:
            raise ValueError('Runtime manifest mismatch')
        if any((source/name).exists() for name in ('config', 'data', 'logs')):
            raise ValueError('Archive unexpectedly contains runtime state')
        source.rename(destination)
    print('Installed verified official runtime. Review its EULA.md and PRIVACY.md before setup.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='An unmodified official ZIP for offline installation')
    args = parser.parse_args()
    install(archive=args.archive)
