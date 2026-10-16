"""Install the hash-pinned MSYS2 SpeexDSP DLL locally, without system changes."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import requests

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://mirror.msys2.org/mingw/ucrt64/mingw-w64-ucrt-x86_64-speexdsp-1.2.1-1-any.pkg.tar.zst'
SHA256 = '0e6997193ba7be890e2ccc80a3cfa8a7a46af53e76a8b355d474a0bb59d260e4'
MEMBERS = ('ucrt64/bin/libspeexdsp-1.dll', 'ucrt64/share/licenses/speexdsp/LICENSE')


def main():
    os.environ.pop('ALI_API_Key', None)
    target = ROOT / 'runtime/echo-tools'
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ronova-speex-') as folder:
        temporary = Path(folder)
        with requests.get(URL, timeout=(10, 60)) as response:
            response.raise_for_status()
            data = response.content
        if hashlib.sha256(data).hexdigest() != SHA256:
            raise RuntimeError('SpeexDSP archive hash mismatch')
        archive = temporary / 'speexdsp.tar.zst'
        archive.write_bytes(data)
        # Only these literal, relative members are extracted; no archive-wide extraction.
        subprocess.run(['tar', '-xf', str(archive), '-C', str(temporary), *MEMBERS], check=True)
        for member in MEMBERS:
            source = temporary / member
            if source.is_symlink() or not source.resolve().is_relative_to(temporary.resolve()):
                raise RuntimeError('Unexpected extracted library path')
            shutil.copyfile(source, target / source.name)
    dll = target / 'libspeexdsp-1.dll'
    date = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d')
    report = dict(source=URL, archive_sha256=SHA256, archive_bytes=len(data),
                  library_sha256=hashlib.sha256(dll.read_bytes()).hexdigest(),
                  library_bytes=dll.stat().st_size, version='1.2.1', license='BSD',
                  timezone='Asia/Shanghai', system_install=False, external_chat_requests=0)
    (ROOT / f'runtime/echo-library-{date}.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('SpeexDSP 本地安装及校验完成。')


if __name__ == '__main__':
    main()
