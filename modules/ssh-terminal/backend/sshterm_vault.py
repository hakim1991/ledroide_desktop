"""Secrets (passwords, private keys, passphrases) encrypted with Windows DPAPI:
only the current Windows user on this machine can decrypt them."""
import base64
import ctypes
import sys

PREFIX = "dpapi:"
_ENTROPY = b"ledroide-ssh-terminal:v1"
_UI_FORBIDDEN = 0x1


class VaultError(Exception):
    pass


def available() -> bool:
    return sys.platform == "win32"


if available():
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    _crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p

    def _blob(data: bytes):
        buf = ctypes.create_string_buffer(data, len(data))
        return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    def _call(fn, data: bytes) -> bytes:
        src, _keep = _blob(data)
        ent, _keep2 = _blob(_ENTROPY)
        out = _Blob()
        # CryptProtectData and CryptUnprotectData share the same signature.
        if not fn(ctypes.byref(src), None, ctypes.byref(ent), None, None, _UI_FORBIDDEN, ctypes.byref(out)):
            raise VaultError(f"DPAPI : erreur {ctypes.get_last_error()}")
        try:
            return ctypes.string_at(out.pbData, out.cbData)
        finally:
            _kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


def protect(text: str) -> str:
    if not text:
        return ""
    if not available():
        raise VaultError("Enregistrement des secrets disponible uniquement sous Windows")
    return PREFIX + base64.b64encode(_call(_crypt32.CryptProtectData, text.encode("utf-8"))).decode("ascii")


def unprotect(token: str) -> str:
    if not token:
        return ""
    if not token.startswith(PREFIX) or not available():
        raise VaultError("Secret enregistré illisible")
    try:
        raw = base64.b64decode(token[len(PREFIX):])
        return _call(_crypt32.CryptUnprotectData, raw).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise VaultError(f"Secret enregistré illisible : {exc}")
