"""ASCII paths for Windows native libraries that cannot open Unicode paths."""
import ctypes
import hashlib
import os
from pathlib import Path
import shutil
import tempfile

# Capture the system temp before a speech worker redirects TEMP into its package.
_SYSTEM_TEMP = Path(tempfile.gettempdir())
_ROOT = Path(__file__).resolve().parent


def _short_path(path):
    if os.name != 'nt':
        return None
    operation = ctypes.WinDLL('kernel32', use_last_error=True).GetShortPathNameW
    operation.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    operation.restype = ctypes.c_uint32
    length = operation(str(path), None, 0)
    if not length:
        return None
    buffer = ctypes.create_unicode_buffer(length)
    written = operation(str(path), buffer, length)
    if not written or written >= length or not buffer.value.isascii():
        return None
    return Path(buffer.value)


def native_resource_path(path, cache_root=None):
    """Return an existing ASCII filename, copying only when 8.3 is unavailable."""
    path = Path(path).absolute()
    if not path.is_file():
        raise FileNotFoundError(path)
    if os.name != 'nt' or str(path).isascii():
        return path
    short = _short_path(path)
    if short is not None:
        return short
    override = cache_root or os.environ.get('RONOVA_NATIVE_RESOURCE_CACHE')
    candidates = [Path(override)] if override else [
        _ROOT/'runtime/native-resource-cache', _SYSTEM_TEMP/'Ronova/native-resource-cache']
    if not override and os.environ.get('PUBLIC'):
        candidates.append(Path(os.environ['PUBLIC'])/'Ronova/native-resource-cache')
    info = path.stat()
    identity = f'{path}\0{info.st_size}\0{info.st_mtime_ns}'.encode('utf-8')
    key = hashlib.sha256(identity).hexdigest()
    suffix = path.suffix if path.suffix.isascii() else '.bin'
    for candidate in candidates:
        candidate = candidate.absolute()
        if not str(candidate).isascii():
            continue
        temporary = None
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            target = candidate/(key+suffix)
            if target.is_file() and target.stat().st_size == info.st_size:
                return target
            with tempfile.NamedTemporaryFile(dir=candidate, prefix=key+'-', suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                with path.open('rb') as source:
                    shutil.copyfileobj(source, stream)
            os.replace(temporary, target)
            return target
        except OSError:
            continue
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    raise RuntimeError('语音资源需要可写的英文缓存路径；请设置 RONOVA_NATIVE_RESOURCE_CACHE，或将项目移至 D:\\Ronova 等全英文目录。')
