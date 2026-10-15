"""Windows クリップボード(ctypes で Win32 API を直接呼ぶ。標準ライブラリのみ)"""

import ctypes
import time
from ctypes import wintypes

_libs = None


def _load():
    """Win32 API を初回利用時に読み込む(Windows 以外でも import だけはできるようにする)"""
    global _libs
    if _libs is None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalFree.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL
        _libs = (user32, kernel32)
    return _libs


CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def _open():
    user32, _ = _load()
    for _ in range(40):
        if user32.OpenClipboard(None):
            return
        time.sleep(0.05)
    raise OSError("クリップボードを開けません(他のアプリが使用中)")


def get():
    user32, kernel32 = _load()
    _open()
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        p = kernel32.GlobalLock(h)
        if not p:
            return ""
        try:
            return ctypes.wstring_at(p)
        finally:
            kernel32.GlobalUnlock(h)
    finally:
        user32.CloseClipboard()


def set(text):
    user32, kernel32 = _load()
    data = text.encode("utf-16-le") + b"\x00\x00"
    h = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not h:
        raise OSError("クリップボード用のメモリを確保できません")
    p = kernel32.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    kernel32.GlobalUnlock(h)
    try:
        _open()
    except OSError:
        kernel32.GlobalFree(h)
        raise
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            kernel32.GlobalFree(h)
            raise OSError("クリップボードへの書き込みに失敗")
    finally:
        user32.CloseClipboard()


def seq():
    """クリップボードが更新されるたびに増える番号(ポーリングが軽い)"""
    user32, _ = _load()
    return user32.GetClipboardSequenceNumber()
