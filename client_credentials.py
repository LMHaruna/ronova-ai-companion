"""Windows user-bound API credentials; plaintext never reaches disk."""
import ctypes as C
from ctypes import wintypes as W
import os
from pathlib import Path
import sys


class Blob(C.Structure):
    _fields_ = [('size', W.DWORD), ('data', C.POINTER(C.c_ubyte))]


def crypt(data, decrypt=False):
    if sys.platform != 'win32':
        raise RuntimeError('API Key 加密保存需要 Windows')
    api = C.WinDLL('crypt32', use_last_error=True)
    kernel = C.WinDLL('kernel32', use_last_error=True)
    operation = api.CryptUnprotectData if decrypt else api.CryptProtectData
    operation.argtypes = [C.POINTER(Blob), C.c_void_p if decrypt else W.LPCWSTR,
                          C.POINTER(Blob), C.c_void_p, C.c_void_p, W.DWORD, C.POINTER(Blob)]
    operation.restype = W.BOOL
    kernel.LocalFree.argtypes = [C.c_void_p]
    kernel.LocalFree.restype = C.c_void_p
    buffer = C.create_string_buffer(data)
    source = Blob(len(data), C.cast(buffer, C.POINTER(C.c_ubyte)))
    output = Blob()
    description = None if decrypt else 'Ronova client model credentials'
    # UI_FORBIDDEN only: do not enable LOCAL_MACHINE / sharing with other users.
    if not operation(C.byref(source), description, None, None, None, 1, C.byref(output)):
        raise RuntimeError('API Key 解密失败，请重新填写' if decrypt else 'API Key 加密失败，配置未保存')
    try:
        return C.string_at(output.data, output.size)
    finally:
        if decrypt:
            C.memset(output.data, 0, output.size)
        kernel.LocalFree(output.data)


def user_credentials_path():
    directory = Path(os.environ.get('LOCALAPPDATA') or Path.home()/'AppData/Local')
    return directory/'Ronova/client-models.bin'


def write_encrypted(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.tmp')
    try:
        temporary.write_bytes(data)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
