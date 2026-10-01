"""Explicit opt-in setup of pinned local dictation; --check is read-only.

Package installation is permitted only inside the invoking project virtualenv.
The fixed model files reuse the standard user cache and are SHA256 verified.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import urllib.request

REVISION = '536b0662742c02347bc0e980a01041f333bce120'
REPOSITORY = 'Systran/faster-whisper-small'
ASSETS = {
    'model.bin': ('3e305921506d8872816023e4c273e75d2419fb89b24da97b4fe7bce14170d671', 483546902),
    'config.json': ('b55496ac7940a7ae47d2c01eab40edfd8701feec1229d9cce3b40014383fb828', 2370),
    'tokenizer.json': ('fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab', 2203239),
    'vocabulary.txt': ('34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913', 459861),
}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def snapshot(cache):
    return cache / 'models--Systran--faster-whisper-small' / 'snapshots' / REVISION


def valid(path, expected, size):
    return path.is_file() and path.stat().st_size == size and digest(path) == expected


def check(cache):
    ready = all(valid(snapshot(cache) / name, sha, size) for name, (sha, size) in ASSETS.items())
    package = importlib.util.find_spec('faster_whisper') is not None
    return {'ok': True, 'available': ready and package, 'modelAvailable': ready,
            'packageAvailable': package, 'model': 'Whisper small', 'revision': REVISION,
            'modelBytes': sum(size for _, size in ASSETS.values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Report local readiness without downloads or installation.')
    parser.add_argument('--cache', type=Path, default=Path.home() / '.cache' / 'huggingface' / 'hub')
    args = parser.parse_args()
    if args.check:
        result = check(args.cache)
        print(json.dumps(result))
        return 0 if result['available'] else 1
    if sys.prefix == sys.base_prefix:
        raise RuntimeError('Run dictation setup using the project .venv Python. Global Python is left unchanged.')
    if sys.version_info < (3, 11):
        raise RuntimeError('Dictation setup requires Python 3.11 or newer.')
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple',
                    '--disable-pip-version-check', '-r', str(Path(__file__).with_name('dictation-requirements.txt'))], check=True)
    directory = snapshot(args.cache)
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    for name, (sha, size) in ASSETS.items():
        path = directory / name
        url = f'https://huggingface.co/{REPOSITORY}/resolve/{REVISION}/{name}'
        if not valid(path, sha, size):
            temporary = directory / (name + '.part')
            with urllib.request.urlopen(url, timeout=60) as source, temporary.open('wb') as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
            if not valid(temporary, sha, size):
                temporary.unlink(missing_ok=True)
                raise RuntimeError('Official dictation asset checksum mismatch: ' + name)
            temporary.replace(path)
        records.append({'name': name, 'url': url, 'sha256': sha, 'bytes': size})
        print('Verified dictation ' + name + ' (' + str(size) + ' bytes)', flush=True)
    runtime = Path(__file__).resolve().parents[1] / '.runtime'
    runtime.mkdir(exist_ok=True)
    (runtime / 'dictation-provenance.json').write_text(json.dumps({'model': REPOSITORY,
        'revision': REVISION, 'license': 'MIT', 'assets': records}, indent=2), encoding='utf-8')
    print('Local dictation is ready. Runtime never downloads models.', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
