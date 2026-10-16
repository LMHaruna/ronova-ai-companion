"""Install one hash-pinned pure-Python websocket dependency in its own directory."""
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if __name__ == '__main__':
    env = dict(os.environ)
    secret_names = {'ali_api_key', 'dashscope_api_key', 'qwen_realtime_api_key'}
    env = {name: value for name, value in env.items() if name.casefold() not in secret_names}
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check',
        '--index-url', 'https://pypi.org/simple', '--only-binary=:all:', '--no-deps',
        '--require-hashes', '--target', str(ROOT / 'runtime/realtime-tools'),
        '-r', str(ROOT / 'scripts/realtime-requirements.txt')], env=env, check=True)
