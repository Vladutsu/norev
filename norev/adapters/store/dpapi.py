"""Windows DPAPI through ctypes (#63): the store key, sealed to the User's Windows account.

CryptProtectData without CRYPTPROTECT_LOCAL_MACHINE: only the same Windows account can unseal
it, on this machine or after a roaming-profile move. A new Windows account or a reinstall
cannot, which is why the key backup (`norev.cmd export-key`) exists. No new dependency.
"""
import ctypes
import sys

_ENTROPY = b"norev-store-key-v1"  # binds the blob to norev; other DPAPI users cannot pass it off as ours
_UI_FORBIDDEN = 0x01


class DpapiError(Exception):
    """DPAPI is missing (not Windows) or refused: another account, or a damaged blob."""


def available() -> bool:
    return sys.platform == "win32"


def protect(data: bytes) -> bytes:
    return _call("CryptProtectData", data)


def unprotect(blob: bytes) -> bytes:
    return _call("CryptUnprotectData", blob)


def _call(name: str, data: bytes) -> bytes:
    if not available():
        raise DpapiError("DPAPI exists only on Windows")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(raw: bytes):
        buffer = ctypes.create_string_buffer(raw, len(raw))
        return Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = getattr(crypt32, name)
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p,
                         ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    source, _keep_source = blob(data)
    entropy, _keep_entropy = blob(_ENTROPY)
    out = Blob()
    if not function(ctypes.byref(source), None, ctypes.byref(entropy), None, None, _UI_FORBIDDEN, ctypes.byref(out)):
        raise DpapiError(f"{name} failed (Windows error {ctypes.get_last_error()})")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))
