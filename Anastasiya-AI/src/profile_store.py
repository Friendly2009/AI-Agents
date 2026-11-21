import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import tempfile


PROFILE = Path(__file__).resolve().parents[2] / "profile"


class Blob(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("data", ctypes.c_void_p)]


def transform(data, encrypt):
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.c_void_p))
    result = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    function = crypt.CryptProtectData if encrypt else crypt.CryptUnprotectData
    flags = 5 if encrypt else 1  # UI forbidden, machine scope for service credentials
    if not function(ctypes.byref(source), None, None, None, None, flags, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.length)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.c_void_p(result.data))


def read_settings():
    path = PROFILE / "settings.bin"
    if path.exists():
        return json.loads(transform(path.read_bytes(), False))
    return {}


def save_settings(values):
    data = transform(json.dumps(values, ensure_ascii=False).encode("utf-8"), True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=PROFILE, delete=False) as temp:
        name = Path(temp.name)
        try:
            temp.write(data)
            temp.flush()
            os.fsync(temp.fileno())
        except BaseException:
            temp.close()
            name.unlink(missing_ok=True)
            raise
    try:
        os.replace(name, PROFILE / "settings.bin")
    finally:
        name.unlink(missing_ok=True)
