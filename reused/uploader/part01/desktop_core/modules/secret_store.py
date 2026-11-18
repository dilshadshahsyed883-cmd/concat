from __future__ import annotations

import base64
import ctypes
import getpass
import os
import subprocess
from ctypes import wintypes
from pathlib import Path

_DPAPI_PREFIX = "dpapi:"


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _protect_windows(payload: bytes) -> bytes:
    buffer = ctypes.create_string_buffer(payload)
    input_blob = _DataBlob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output_blob = _DataBlob()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def _unprotect_windows(payload: bytes) -> bytes:
    buffer = ctypes.create_string_buffer(payload)
    input_blob = _DataBlob(len(payload), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output_blob = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(input_blob), None, None, None, None, 0, ctypes.byref(output_blob)
    ):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(output_blob.pbData)


def protect_secret(value: str | None) -> str:
    """Protect a database secret for the current Windows user.

    Existing DPAPI values are preserved. Non-Windows builds keep plaintext for
    compatibility; this application is distributed for Windows and applies DPAPI
    there automatically.
    """
    text = str(value or "")
    if not text or text.startswith(_DPAPI_PREFIX):
        return text
    if os.name != "nt":
        return text
    protected = _protect_windows(text.encode("utf-8"))
    return _DPAPI_PREFIX + base64.b64encode(protected).decode("ascii")


def reveal_secret(value: str | None) -> str:
    text = str(value or "")
    if not text.startswith(_DPAPI_PREFIX):
        return text
    if os.name != "nt":
        # A Windows-protected value is intentionally unusable on another OS/user.
        return ""
    try:
        payload = base64.b64decode(text[len(_DPAPI_PREFIX):].encode("ascii"))
        return _unprotect_windows(payload).decode("utf-8")
    except Exception:
        return ""


def is_protected(value: str | None) -> bool:
    return str(value or "").startswith(_DPAPI_PREFIX)


def secure_file_for_current_user(path_value: str | Path) -> None:
    """Best-effort restriction of a sensitive file to the current OS user."""
    path = Path(path_value)
    if not path.exists() or not path.is_file():
        return
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    if os.name != "nt":
        return
    try:
        username = getpass.getuser()
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:(F)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        pass
