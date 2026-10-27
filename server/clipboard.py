"""Puts text on the PC clipboard, without extra packages.

Windows uses the clipboard API through ctypes; macOS uses pbcopy; Linux uses whichever
of wl-copy, xclip or xsel is installed.

On Windows the text is marked so it stays out of clipboard history (Win+V) and isn't synced
to other devices through the cloud clipboard: it may be a password sent from the phone.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
# Registered clipboard formats Windows checks before saving to history or syncing to the cloud.
# A DWORD 0 in each means "no".
PRIVACY_FORMATS = ("CanIncludeInClipboardHistory", "CanUploadToCloudClipboard")


def set_text(text: str) -> None:
    if sys.platform == "win32":
        _set_windows(text)
    elif sys.platform == "darwin":
        _pipe(["pbcopy"], text)
    else:
        for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"], ["xsel", "--clipboard", "--input"]):
            if shutil.which(cmd[0]):
                _pipe(cmd, text)
                return
        raise RuntimeError("No clipboard tool found. Install wl-clipboard, xclip or xsel.")


def _pipe(cmd: list[str], text: str) -> None:
    result = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, timeout=5)
    if result.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {result.stderr.decode(errors='replace').strip()}")


def _set_windows(text: str) -> None:
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]

    def put(fmt: int, data: bytes) -> None:
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not handle:
            raise RuntimeError("Not enough memory for the clipboard text.")
        ctypes.memmove(kernel32.GlobalLock(handle), data, len(data))
        kernel32.GlobalUnlock(handle)
        # On success Windows owns the memory; on failure it is still ours to free.
        if not user32.SetClipboardData(fmt, handle):
            kernel32.GlobalFree(handle)
            raise RuntimeError("Could not set the PC clipboard.")

    # Another program may have the clipboard open for a moment; retry briefly.
    for _ in range(20):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.025)
    else:
        raise RuntimeError("The PC clipboard is busy. Try again.")
    try:
        user32.EmptyClipboard()
        for name in PRIVACY_FORMATS:
            put(user32.RegisterClipboardFormatW(name), (0).to_bytes(4, "little"))
        put(CF_UNICODETEXT, text.encode("utf-16-le") + b"\0\0")
    finally:
        user32.CloseClipboard()
