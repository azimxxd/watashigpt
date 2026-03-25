from __future__ import annotations

import ctypes
import os
import subprocess
import time
from ctypes import wintypes

import keyboard

try:
    import pyperclip
except ImportError:
    pyperclip = None

try:
    from plyer import notification
except ImportError:
    notification = None

from actionflow.core.models import AppContext
from actionflow.platform.base import ClipboardManager, HotkeyManager, PlatformServices, SystemIntegration, WindowManager


class WindowsSystemIntegration(SystemIntegration):
    def run_process(self, cmd: list[str], **kwargs):
        return subprocess.run(cmd, **kwargs)

    def notify(self, title: str, message: str) -> None:
        if notification is None:
            raise RuntimeError("plyer not installed")
        notification.notify(title=title, message=message, timeout=5, app_name="ActionFlow")

    def open_path(self, path: str) -> None:
        os.startfile(path)


class WindowsClipboardManager(ClipboardManager):
    _CF_UNICODETEXT = 13
    _GMEM_MOVEABLE = 0x0002

    def __init__(self, backend: str = "auto"):
        self._backend = backend

    def get_sequence(self) -> int | None:
        user32 = ctypes.windll.user32
        user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
        try:
            return int(user32.GetClipboardSequenceNumber())
        except Exception:
            return None

    def _open_clipboard(self) -> None:
        user32 = ctypes.windll.user32
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        for _ in range(10):
            if user32.OpenClipboard(None):
                return
            time.sleep(0.02)
        raise RuntimeError("OpenClipboard failed")

    def _copy_via_winapi(self, text: str) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.EmptyClipboard.restype = wintypes.BOOL
        user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
        user32.SetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        data = text.replace("\r\n", "\n").replace("\n", "\r\n") + "\x00"
        encoded = data.encode("utf-16-le")

        self._open_clipboard()
        try:
            user32.EmptyClipboard()
            handle = kernel32.GlobalAlloc(self._GMEM_MOVEABLE, len(encoded))
            ptr = kernel32.GlobalLock(handle)
            try:
                ctypes.memmove(ptr, encoded, len(encoded))
            finally:
                kernel32.GlobalUnlock(handle)
            if not user32.SetClipboardData(self._CF_UNICODETEXT, handle):
                raise RuntimeError("SetClipboardData failed")
        finally:
            user32.CloseClipboard()

    def _paste_via_winapi(self) -> str:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        self._open_clipboard()
        try:
            handle = user32.GetClipboardData(self._CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = kernel32.GlobalLock(handle)
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def copy(self, text: str) -> None:
        if self._backend in ("auto", "pyperclip") and pyperclip is not None:
            try:
                pyperclip.copy(text)
                return
            except Exception:
                pass
        self._copy_via_winapi(text)

    def paste(self) -> str:
        if self._backend in ("auto", "pyperclip") and pyperclip is not None:
            try:
                return pyperclip.paste()
            except Exception:
                pass
        return self._paste_via_winapi()


class WindowsHotkeyManager(HotkeyManager):
    def add_hotkey(self, hotkey: str, callback) -> None:
        keyboard.add_hotkey(hotkey, callback)

    def clear_hotkeys(self) -> None:
        keyboard.clear_all_hotkeys()

    def release_modifiers(self) -> None:
        for key in ("ctrl", "alt", "shift"):
            try:
                keyboard.release(key)
            except Exception:
                continue

    def send_copy(self, alternate: bool = False) -> None:
        self.release_modifiers()
        combo = "ctrl+insert" if alternate else "ctrl+c"
        keyboard.send(combo)

    def send_paste(self) -> None:
        self.release_modifiers()
        keyboard.send("ctrl+v")


class WindowsWindowManager(WindowManager):
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SW_RESTORE = 9

    def __init__(self, backend: str = "auto"):
        self._backend = backend

    def _get_foreground_hwnd(self):
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        return user32.GetForegroundWindow()

    def _get_window_title(self, hwnd) -> str:
        user32 = ctypes.windll.user32
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def _get_process_name(self, hwnd) -> str:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        handle = kernel32.OpenProcess(self._PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(260)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return os.path.basename(buffer.value).lower()
        finally:
            kernel32.CloseHandle(handle)
        return ""

    def detect_active_window(self) -> AppContext:
        hwnd = self._get_foreground_hwnd()
        title = self._get_window_title(hwnd).strip().lower()
        app_name = self._get_process_name(hwnd) if self._backend in ("auto", "win32") else ""
        searchable = " ".join(part for part in [title, app_name] if part)
        for ctx_type, patterns in AppContext.APP_PATTERNS.items():
            for pattern in patterns:
                if pattern in searchable:
                    return AppContext(ctx_type, title, app_name or pattern)
        return AppContext(AppContext.UNKNOWN, title, app_name)

    def capture_focus_target(self):
        return self._get_foreground_hwnd()

    def restore_focus(self, target) -> None:
        if not target:
            return
        user32 = ctypes.windll.user32
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL
        user32.ShowWindow(target, self._SW_RESTORE)
        user32.SetForegroundWindow(target)


def build_windows_services(clipboard_backend: str = "auto", window_backend: str = "auto") -> PlatformServices:
    return PlatformServices(
        clipboard=WindowsClipboardManager(backend=clipboard_backend),
        hotkeys=WindowsHotkeyManager(),
        windows=WindowsWindowManager(backend=window_backend),
        system=WindowsSystemIntegration(),
    )
