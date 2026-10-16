"""Verify/download pinned CUDA wheels and prepare the single client runtime."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WHEELS = ROOT / 'runtime/wheelhouse-cuda'
LOCK = ROOT / 'scripts/requirements-voice-cuda-py311.txt'
INDEX = 'https://download.pytorch.org/whl/cu128'


def run(command):
    subprocess.run([str(arg) for arg in command], cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download', action='store_true', help='Explicitly download official CUDA wheels.')
    parser.add_argument('--reinstall', action='store_true')
    args = parser.parse_args()
    if os.name != 'nt' or sys.version_info[:2] != (3, 11) or struct.calcsize('P') != 8:
        raise RuntimeError('Please use the bundled Windows x64 Python 3.11 runtime.')
    os.environ.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
                      PIP_DISABLE_PIP_VERSION_CHECK='1')
    if args.download:
        WHEELS.mkdir(parents=True, exist_ok=True)
        run([sys.executable, '-m', 'pip', 'download', '--no-deps', '--no-cache-dir',
             '--dest', WHEELS, '--index-url', INDEX, '--require-hashes', '-r', LOCK])
    sources = []
    for line in LOCK.read_text(encoding='utf-8').splitlines():
        if not line or line.startswith('#'):
            continue
        package, version, expected_hash = re.fullmatch(r'(\w+)==(\S+) --hash=sha256:([0-9a-f]{64})', line).groups()
        name = f'{package}-{version}-cp311-cp311-win_amd64.whl'
        path = WHEELS / name
        if not path.is_file():
            raise FileNotFoundError(f'{name} is missing; include wheelhouse-cuda or run with --download.')
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != expected_hash:
            raise RuntimeError(f'CUDA wheel SHA256 differs: {name}')
        sources.append({'file': name, 'sha256': digest, 'bytes': path.stat().st_size,
                        'verified_against': INDEX, 'package': package, 'version': version})
    (WHEELS / 'sources.json').write_text(json.dumps(sources, indent=2)+'\n', encoding='utf-8')
    command = [sys.executable, ROOT / 'scripts/setup_desktop.py', '--ensure']
    if args.reinstall: command.append('--reinstall')
    run(command)
    print('CUDA setup complete. Restart Ronova; NVIDIA GPUs are selected automatically.', flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f'GPU setup failed: {type(error).__name__}: {error}', file=sys.stderr)
        raise SystemExit(1)
