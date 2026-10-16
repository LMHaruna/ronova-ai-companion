"""Offline, relocatable Windows developer environment; system Python is unused."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import venv

ROOT=Path(__file__).resolve().parents[1]
ENV=ROOT/'runtime/voice-venv'
PYTHON=ENV/'Scripts/python.exe'
MARKER=ENV/'.desktop-env.json'
LOCK=ROOT/'scripts/requirements-desktop-py311.txt'
WHEELS=ROOT/'runtime/wheelhouse'


def run(command):
    subprocess.run([str(arg) for arg in command],cwd=ROOT,check=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ensure',action='store_true')
    parser.add_argument('--reinstall',action='store_true')
    parser.add_argument('--no-pause',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if os.name!='nt' or sys.version_info[:2]!=(3,11) or struct.calcsize('P')!=8:
        raise RuntimeError('Please use the bundled Windows x64 Python 3.11 runtime.')
    if ROOT.drive and len(str(ROOT))>135:
        raise RuntimeError('Please extract closer to the drive root (for example D:/Ronova).')
    lock_hash=hashlib.sha256(LOCK.read_bytes()).hexdigest()
    expected={'root':str(ROOT),'base_python':str(Path(sys.executable).resolve()),'lock_sha256':lock_hash}
    try: current=json.loads(MARKER.read_text(encoding='utf-8'))
    except (OSError,ValueError): current={}
    if not args.reinstall and PYTHON.is_file() and all(current.get(k)==v for k,v in expected.items()):
        print('Ronova isolated Python 3.11 environment is ready.',flush=True)
        return 0
    # Never erase an existing environment. Retain it for recovery if the project moved.
    cfg=ENV/'pyvenv.cfg'
    cfg_text=cfg.read_text(encoding='utf-8') if cfg.is_file() else ''
    expected_home='home = '+str(Path(sys.executable).resolve().parent)
    reusable=PYTHON.is_file() and expected_home.casefold() in cfg_text.casefold() and not args.reinstall
    if ENV.exists() and not reusable:
        backup=ENV.with_name('voice-venv.previous-'+time.strftime('%Y%m%d-%H%M%S'))
        if backup.exists(): raise FileExistsError('Environment backup already exists.')
        ENV.rename(backup)
        print('Previous environment retained at '+str(backup),flush=True)
    if not WHEELS.is_dir(): raise FileNotFoundError('Offline wheelhouse is missing.')
    temporary=ROOT/'runtime/setup-tmp'
    temporary.mkdir(parents=True,exist_ok=True)
    os.environ.update(PYTHONNOUSERSITE='1',PYTHONDONTWRITEBYTECODE='1',PYTHONUTF8='1',
                      PIP_NO_INDEX='1',PIP_DISABLE_PIP_VERSION_CHECK='1',PIP_NO_CACHE_DIR='1',
                      TEMP=str(temporary),TMP=str(temporary))
    print('Creating isolated Python 3.11 environment. System Python is unchanged.',flush=True)
    if not reusable: venv.EnvBuilder(with_pip=True).create(ENV)
    run([PYTHON,'-m','pip','install','--no-index','--find-links',WHEELS,
         '--find-links',ROOT/'runtime/wheelhouse-cuda','--only-binary=:all:',
         '--no-deps','--no-compile','--require-hashes','-r',LOCK])
    run([PYTHON,'-m','pip','check'])
    # Native libraries and speech resources must load before marking setup complete.
    run([PYTHON,ROOT/'scripts/verify_desktop_migration.py','--quick'])
    MARKER.write_text(json.dumps(dict(expected,python=sys.version.split()[0],created=time.strftime('%Y-%m-%d %H:%M:%S')),
                                ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('Setup complete. Start-Client.cmd opens Ronova.',flush=True)
    return 0


if __name__=='__main__':
    try: raise SystemExit(main())
    except Exception as error:
        print('Setup failed: '+type(error).__name__+': '+str(error),file=sys.stderr)
        raise SystemExit(1)
