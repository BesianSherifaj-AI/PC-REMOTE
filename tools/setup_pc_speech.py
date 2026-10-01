"""Install and verify fixed official Kokoro assets. Runtime never downloads."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import urllib.request
import venv

ROOT = Path(__file__).resolve().parents[1] / 'tools' / 'speech'
ASSETS = {
    'kokoro-v1.0.fp16.onnx': ('https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/kokoro-v1.0.fp16.onnx',
                            'f3a290d384fbb27966d462905c71a46cef9e5fd00516b40df32a0b4afe77ac96', 163527961),
    'voices-v1.0.bin': ('https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.1/voices-v1.0.bin',
                       'bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d', 28214398),
}


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    if sys.version_info < (3, 11):
        raise RuntimeError('Speech setup requires Python 3.11 or newer.')
    ROOT.mkdir(parents=True, exist_ok=True)
    runtime = ROOT / 'venv'
    python = runtime / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
    if not python.is_file():
        venv.EnvBuilder(with_pip=True, system_site_packages=True).create(runtime)
    requirements = Path(__file__).with_name('speech-requirements.txt')
    subprocess.run([str(python), '-m', 'pip', 'install', '--index-url', 'https://pypi.org/simple',
                    '--disable-pip-version-check', '-r', str(requirements)], check=True)
    records = []
    for name, (url, expected, size) in ASSETS.items():
        path = ROOT / name
        if not path.is_file() or path.stat().st_size != size or digest(path) != expected:
            partial = ROOT / (name + '.part')
            with urllib.request.urlopen(url, timeout=60) as source, partial.open('wb') as target:
                while chunk := source.read(1024 * 1024):
                    target.write(chunk)
            if partial.stat().st_size != size or digest(partial) != expected:
                partial.unlink(missing_ok=True)
                raise RuntimeError('Official speech asset checksum mismatch: ' + name)
            partial.replace(path)
        records.append({'name': name, 'url': url, 'sha256': expected, 'bytes': size})
        print('Verified ' + name + ' (' + str(size) + ' bytes)', flush=True)
    package = json.load(urllib.request.urlopen('https://pypi.org/pypi/kokoro-onnx/0.6.1/json', timeout=30))
    wheel = next(item for item in package['urls'] if item['filename'].endswith('.whl'))
    records.append({'name': wheel['filename'], 'url': wheel['url'], 'sha256': wheel['digests']['sha256']})
    (ROOT / 'provenance.json').write_text(json.dumps({'model': 'hexgrad/Kokoro-82M v1.0',
        'release': 'model-files-v1.1', 'modelLicense': 'Apache-2.0', 'runtime': 'kokoro-onnx==0.6.1',
        'runtimeLicense': 'MIT', 'assets': records}, indent=2), encoding='utf-8')
    print('PC speech runtime and verified assets are ready.', flush=True)


if __name__ == '__main__':
    main()
