# ActionFlow — OS-level background assistant
#
# Run on Linux with:   sudo -E python main.py
#   -E preserves DISPLAY, WAYLAND_DISPLAY, DBUS_SESSION_BUS_ADDRESS
# Run on Windows with: python main.py
#
# Config: edit config.yaml to add commands, set hotkeys, configure LLM

__version__ = "1.0.0"

import os
import sys
import time
import threading
import subprocess
import platform
import keyboard
import shutil
import ctypes
import json
import base64
import hashlib
import yaml
import argparse
import urllib.request
import urllib.parse
import urllib.error
import queue
from pathlib import Path
from datetime import datetime, timedelta
import re as _re
import math
import ast
import secrets
import string
import operator
import tempfile
from dataclasses import dataclass

from actionflow.app.hotkeys import register_hotkeys
from actionflow.app.notifications import notifications
from actionflow.app.startup_logging import get_startup_logger
from actionflow.core.clipboard_flow import ClipboardCaptureConfig, capture_selection_via_clipboard, paste_with_clipboard_restore
from actionflow.app.tray import start_tray_thread
from actionflow.core.clipboard_stack import ClipboardStack, NamedClipsStore
from actionflow.core.command_runner import format_command_result, prepare_command_for_execution
from actionflow.core.command_registry import CommandRegistry
from actionflow.core.config import DEFAULT_CONFIG as CORE_DEFAULT_CONFIG
from actionflow.core.config import load_config as load_core_config
from actionflow.core.history import load_history_entries as load_history_entries_safe
from actionflow.core.llm_ops import (
    LLMResolution,
    LLMSetupChoice,
    LLM_STATE_MOCK,
    LLM_STATE_NEEDS_SETUP,
    LLM_STATE_READY,
    PROVIDER_BASE_URLS,
    PROVIDER_DEFAULT_MODELS,
    build_rewrite_prompt,
    clean_rewrite_output,
    load_llm_secrets,
    maybe_prompt_llm_setup,
    redact_llm_settings,
    resolve_llm_state,
    save_llm_secrets,
    strip_matching_prefix,
)
from actionflow.core.llm import COMMAND_SPECS
from actionflow.core.llm.transform_executor import TransformExecutionError, execute_transform_command
from actionflow.core.lookup_ops import (
    build_wiki_result,
    choose_preferred_wiki_title,
    detect_query_language,
    parse_russian_wiktionary_definitions,
    select_safe_definitions,
)
from actionflow.core.text_ops import escape_text, format_reading_time, text_stats
from actionflow.platform.linux import build_linux_services
from actionflow.platform.windows import build_windows_services

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

if platform.system() == "Windows":
    import msvcrt
    from ctypes import wintypes
else:
    import tty
    import termios
    import select

try:
    import tkinter as tk
    from tkinter import font as tkfont
    _TKINTER_AVAILABLE = True
except ImportError:
    _TKINTER_AVAILABLE = False

# Persistent hidden tk root — tkinter only allows one Tk() instance per process.
# All popups must use Toplevel(). This root is created lazily on first use.
_tk_root: "tk.Tk | None" = None

def _get_tk_root() -> "tk.Tk":
    """Return the persistent hidden Tk root, creating it on first call."""
    global _tk_root
    if _tk_root is None or not _tk_root.winfo_exists():
        _tk_root = tk.Tk()
        _tk_root.withdraw()
    return _tk_root

try:
    import pyperclip
except ImportError:
    pyperclip = None

try:
    from plyer import notification
except ImportError:
    notification = None

# ============================================================
# Config Loading
# ============================================================

_APP_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _APP_DIR.parents[1]
_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"

_DEFAULT_CONFIG = CORE_DEFAULT_CONFIG


def load_config() -> dict:
    """Load config.yaml, falling back to defaults if missing or invalid."""
    return load_core_config(_CONFIG_PATH)


CONFIG = load_config()
notifications.configure_from_config(CONFIG)

# ============================================================
# Constants (from config)
# ============================================================

HOTKEY: str = CONFIG["hotkeys"]["intercept"]
UNDO_HOTKEY: str = CONFIG["hotkeys"]["undo"]
CLIPBOARD_DELAY: float = 0.15
APP_NAME: str = "ActionFlow"
IS_WINDOWS: bool = platform.system() == "Windows"
IS_LINUX: bool = platform.system() == "Linux"

_SESSION_TYPE: str = "windows" if IS_WINDOWS else os.environ.get("XDG_SESSION_TYPE", "x11")
_IS_WAYLAND: bool = IS_LINUX and _SESSION_TYPE == "wayland"
_SUDO_USER: str = os.environ.get("SUDO_USER", "") if IS_LINUX else ""
_DISPLAY: str = os.environ.get("DISPLAY", ":0") if IS_LINUX else ""
_WAYLAND_DISPLAY: str = os.environ.get("WAYLAND_DISPLAY", "") if IS_LINUX else ""
_HAS_WTYPE: bool = IS_LINUX and _IS_WAYLAND and shutil.which("wtype") is not None
_DBUS_SESSION: str = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "") if IS_LINUX else ""

_PLATFORM_CFG: dict = CONFIG.get("platform", {})
_CLIPBOARD_BACKEND: str = _PLATFORM_CFG.get("clipboard_backend", "auto")
_WINDOW_BACKEND: str = _PLATFORM_CFG.get("window_backend", "auto")

_undo_stack: list[dict] = []
_undo_lock = threading.Lock()
_exit_event = threading.Event()

_current_notify_level: str = "always"  # Set per-dispatch from cmd config

_usage_counts: dict[str, int] = {}
_usage_lock = threading.Lock()

_activity_log: list[dict] = []
_activity_lock = threading.Lock()
_ACTIVITY_MAX = 5

_micro_log: list[str] = []
_micro_log_lock = threading.Lock()
_MICRO_LOG_MAX = 3

_start_time: float = time.time()

_last_command: dict | None = None  # For REPEAT: stores {"name": ..., "config": ...}
_CLIPS_PATH = Path.home() / ".actionflow_clips.json"
_clip_store = NamedClipsStore(_CLIPS_PATH, max_clips=100)
_clipboard_stack = ClipboardStack(max_items=50)
_pending_undo_entry: dict | None = None
_last_command_output: str = ""
_current_dispatch_result: dict | None = None

_popup_queue: queue.Queue = queue.Queue()  # Hotkey thread → main thread for popup
_popup_trigger: str = "prefix"  # Set per-dispatch: "prefix" or "popup"
_dispatch_busy: bool = False  # True while a command is being dispatched

_current_app_context = None   # AppContext instance, set per-intercept
_current_text_analysis = None  # TextAnalysis instance, set per-intercept
_pattern_learner = None       # PatternLearner instance, initialized in main()
_last_focus_target = None     # Platform-specific handle for restoring focus after popup

_silent_mode: bool = notifications.mode == "silent"
_silent_mode_lock = threading.Lock()


_tray_icon = None  # pystray icon, set in _start_tray()
_runtime_initialized: bool = False
_hotkeys_registered: bool = False
_hotkey_registration_error: str | None = None
_hotkey_callback_count: int = 0
_last_hotkey_callback_at: float | None = None
_last_hotkey_error: str | None = None
_last_hotkey_error_at: float | None = None
_runtime_polling_active: bool = False
_config_watcher_started: bool = False
_llm_setup_handler = None
_image_setup_handler = None
_startup_logger = get_startup_logger()


def _reset_dispatch_result() -> None:
    global _current_dispatch_result
    _current_dispatch_result = {
        "output": "",
        "status": "pending",
        "replaced": False,
    }


def _record_command_result(output: str, *, replaced: bool = False) -> None:
    global _current_dispatch_result
    if _current_dispatch_result is None:
        _reset_dispatch_result()
    _current_dispatch_result["output"] = output
    _current_dispatch_result["status"] = "success"
    _current_dispatch_result["replaced"] = replaced


def _current_dispatch_output() -> str:
    if not _current_dispatch_result:
        return "(done)"
    output = str(_current_dispatch_result.get("output", "") or "").strip()
    return output or "(done)"


def _format_runtime_ts(value: float | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value).isoformat(timespec="seconds")
    except Exception:
        return None


def _runtime_health_snapshot() -> tuple[str, str]:
    if _hotkey_registration_error:
        return "partial", f"hotkeys unavailable: {_hotkey_registration_error}"
    if _runtime_initialized and _hotkeys_registered and _runtime_polling_active:
        return "ready", "background runtime active"
    if _runtime_initialized and _hotkeys_registered:
        return "partial", "hotkeys active but runtime polling inactive"
    if _runtime_initialized:
        return "partial", "runtime initialized but hotkeys inactive"
    return "starting", "runtime starting"


def set_runtime_polling_active(active: bool) -> None:
    global _runtime_polling_active
    if _runtime_polling_active == active:
        return
    _runtime_polling_active = active
    message = "Runtime polling active" if active else "Runtime polling stopped"
    notifications.log_info(message)
    _startup_logger.info(message)


def _update_tray_color(color: str) -> None:
    """Update tray icon color. No-op if tray not running."""
    if _tray_icon is None:
        return
    try:
        _tray_icon.icon = _create_tray_icon_image(color)
    except Exception:
        pass


def _sync_ui_mode_state() -> None:
    global _silent_mode
    with _silent_mode_lock:
        _silent_mode = notifications.mode == "silent"


# ============================================================
# Context — Active Window Detection
# ============================================================

class AppContext:
    """Detected context of the active application window."""
    TERMINAL = "terminal"
    BROWSER  = "browser"
    IDE      = "ide"
    CHAT     = "chat"
    DOCS     = "docs"
    UNKNOWN  = "unknown"

    APP_PATTERNS = {
        "terminal": ["terminal", "konsole", "alacritty", "kitty", "wezterm",
                      "gnome-terminal", "xterm", "foot", "tilix", "tmux",
                      "powershell", "pwsh", "cmd.exe", "windows terminal"],
        "browser":  ["firefox", "chrome", "chromium", "brave", "vivaldi",
                      "edge", "safari", "opera", "zen browser"],
        "ide":      ["code", "vscode", "jetbrains", "intellij", "pycharm",
                      "webstorm", "clion", "rider", "neovim", "nvim", "vim",
                      "emacs", "sublime", "zed", "cursor", "lapce"],
        "chat":     ["slack", "discord", "telegram", "teams", "signal",
                      "whatsapp", "element"],
        "docs":     ["libreoffice", "google docs", "notion", "obsidian",
                      "logseq", "typora", "marktext", "writer", "word"],
    }

    def __init__(self, context_type: str = "unknown", window_title: str = "",
                 app_name: str = ""):
        self.context_type = context_type
        self.window_title = window_title
        self.app_name = app_name

    def __repr__(self) -> str:
        return f"AppContext({self.context_type}, app={self.app_name})"


def _find_focused_sway(node: dict) -> dict | None:
    """Recursively find the focused node in a sway tree."""
    if node.get("focused"):
        return node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        result = _find_focused_sway(child)
        if result:
            return result
    return None


class ClipboardManager:
    def copy(self, text: str) -> None:
        raise NotImplementedError

    def paste(self) -> str:
        raise NotImplementedError

    def get_primary_selection(self) -> str:
        return ""

    def copy_image(self, image_path: str) -> bool:
        return False


class HotkeyManager:
    def add_hotkey(self, hotkey: str, callback) -> None:
        keyboard.add_hotkey(hotkey, callback)

    def release_modifiers(self) -> None:
        for key in ("ctrl", "alt", "shift"):
            try:
                keyboard.release(key)
            except Exception:
                pass

    def send_copy(self, alternate: bool = False) -> None:
        self.release_modifiers()
        keyboard.send("ctrl+insert" if alternate else "ctrl+c")

    def send_paste(self) -> None:
        self.release_modifiers()
        keyboard.send("ctrl+v")


class WindowManager:
    def detect_active_window(self) -> AppContext:
        return AppContext()

    def capture_focus_target(self):
        return None

    def restore_focus(self, target) -> None:
        return None


class SystemIntegration:
    def run_process(self, cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, **kwargs)

    def notify(self, title: str, message: str) -> None:
        raise NotImplementedError

    def open_path(self, path: str) -> None:
        raise NotImplementedError


class LinuxSystemIntegration(SystemIntegration):
    def run_process(self, cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
        env = {**os.environ, "DISPLAY": _DISPLAY}
        if _DBUS_SESSION:
            env["DBUS_SESSION_BUS_ADDRESS"] = _DBUS_SESSION
        if _WAYLAND_DISPLAY:
            env["WAYLAND_DISPLAY"] = _WAYLAND_DISPLAY
            env["XDG_SESSION_TYPE"] = "wayland"

        if _SUDO_USER and os.geteuid() == 0:
            preserve = "DISPLAY,DBUS_SESSION_BUS_ADDRESS"
            if _IS_WAYLAND:
                preserve += ",WAYLAND_DISPLAY,XDG_RUNTIME_DIR,XDG_SESSION_TYPE"
            full_cmd = [
                "sudo", "-u", _SUDO_USER, f"--preserve-env={preserve}",
            ] + cmd
        else:
            full_cmd = cmd

        return subprocess.run(full_cmd, env=env, **kwargs)

    def notify(self, title: str, message: str) -> None:
        self.run_process(
            ["notify-send", "-t", "5000",
             _sanitize_for_notify(title), _sanitize_for_notify(message)],
            capture_output=True,
            timeout=3,
        )

    def open_path(self, path: str) -> None:
        self.run_process(["xdg-open", path], capture_output=True, timeout=5)


class WindowsSystemIntegration(SystemIntegration):
    def notify(self, title: str, message: str) -> None:
        if notification is None:
            raise RuntimeError("plyer not installed")
        notification.notify(
            title=title,
            message=message,
            timeout=5,
            app_name=APP_NAME,
        )

    def open_path(self, path: str) -> None:
        os.startfile(path)


class LinuxClipboardManager(ClipboardManager):
    def copy(self, text: str) -> None:
        if _CLIPBOARD_BACKEND == "pyperclip" and pyperclip is not None:
            pyperclip.copy(text)
            return
        if _IS_WAYLAND:
            proc = _system_integration.run_process(
                ["wl-copy", "--", text],
                capture_output=True,
            )
        else:
            proc = _system_integration.run_process(
                ["xclip", "-selection", "clipboard"],
                input=text.encode(),
                capture_output=True,
            )
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="ignore").strip() if proc.stderr else "unknown error"
            TUI.error(f"Clipboard copy failed: {stderr}")

    def paste(self) -> str:
        if _CLIPBOARD_BACKEND == "pyperclip" and pyperclip is not None:
            return pyperclip.paste()
        try:
            if _IS_WAYLAND:
                cmd = ["wl-paste", "--no-newline"]
            else:
                cmd = ["xclip", "-selection", "clipboard", "-o"]
            proc = _system_integration.run_process(cmd, capture_output=True, text=True)
            return proc.stdout if proc.returncode == 0 else ""
        except Exception as exc:
            TUI.error(f"Clipboard paste failed: {exc}")
            return ""

    def get_primary_selection(self) -> str:
        try:
            proc = _system_integration.run_process(
                ["wl-paste", "--primary", "--no-newline"],
                capture_output=True,
                text=True,
            )
            return proc.stdout if proc.returncode == 0 else ""
        except Exception as exc:
            TUI.error(f"Primary selection read failed: {exc}")
            return ""

    def copy_image(self, image_path: str) -> bool:
        try:
            if _IS_WAYLAND:
                with open(image_path, "rb") as f:
                    proc = _system_integration.run_process(
                        ["wl-copy", "--type", "image/png"],
                        input=f.read(),
                        capture_output=True,
                    )
            else:
                proc = _system_integration.run_process(
                    ["xclip", "-selection", "clipboard", "-t", "image/png", "-i", image_path],
                    capture_output=True,
                )
            return proc.returncode == 0
        except Exception as exc:
            TUI.error(f"Image clipboard copy failed: {exc}")
            return False


class WindowsClipboardManager(ClipboardManager):
    _CF_UNICODETEXT = 13
    _GMEM_MOVEABLE = 0x0002

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
            if not handle:
                raise MemoryError("GlobalAlloc failed")
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                raise MemoryError("GlobalLock failed")
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
            if not ptr:
                return ""
            try:
                return ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()

    def copy(self, text: str) -> None:
        prefer_pyperclip = _CLIPBOARD_BACKEND in ("auto", "pyperclip")
        if prefer_pyperclip and pyperclip is not None:
            try:
                pyperclip.copy(text)
                return
            except Exception:
                pass
        self._copy_via_winapi(text)

    def paste(self) -> str:
        prefer_pyperclip = _CLIPBOARD_BACKEND in ("auto", "pyperclip")
        if prefer_pyperclip and pyperclip is not None:
            try:
                return pyperclip.paste()
            except Exception:
                pass
        return self._paste_via_winapi()


class LinuxHotkeyManager(HotkeyManager):
    def send_paste(self) -> None:
        self.release_modifiers()
        time.sleep(0.05)

        if _HAS_WTYPE:
            try:
                result = _system_integration.run_process(
                    ["wtype", "-d", "50", "-M", "ctrl", "-k", "v", "-m", "ctrl"],
                    capture_output=True,
                    timeout=3,
                )
                if result.returncode == 0:
                    return
            except Exception:
                pass

        keyboard.send("ctrl+v")


class WindowsHotkeyManager(HotkeyManager):
    pass


class LinuxWindowManager(WindowManager):
    def detect_active_window(self) -> AppContext:
        title = ""
        app_name = ""
        use_kdotool = _WINDOW_BACKEND in ("auto", "kdotool")
        use_swaymsg = _WINDOW_BACKEND in ("auto", "swaymsg")
        use_xdotool = _WINDOW_BACKEND in ("auto", "xdotool")
        try:
            if _IS_WAYLAND:
                if use_kdotool:
                    try:
                        proc = _system_integration.run_process(
                            ["kdotool", "getactivewindow", "getwindowname"],
                            capture_output=True,
                            text=True,
                            timeout=2,
                        )
                        if proc.returncode == 0:
                            title = proc.stdout.strip().lower()
                    except (FileNotFoundError, subprocess.TimeoutExpired):
                        pass
                if not title and use_swaymsg:
                    try:
                        proc = _system_integration.run_process(
                            ["swaymsg", "-t", "get_tree"],
                            capture_output=True,
                            text=True,
                            timeout=2,
                        )
                        if proc.returncode == 0:
                            tree = json.loads(proc.stdout)
                            focused = _find_focused_sway(tree)
                            if focused:
                                title = (focused.get("name", "") or "").lower()
                                app_name = (focused.get("app_id", "") or "").lower()
                    except Exception:
                        pass
            elif use_xdotool:
                try:
                    proc = _system_integration.run_process(
                        ["xdotool", "getactivewindow", "getwindowname"],
                        capture_output=True,
                        text=True,
                        timeout=2,
                    )
                    if proc.returncode == 0:
                        title = proc.stdout.strip().lower()
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass
        except Exception:
            pass

        searchable = " ".join(part for part in [title, app_name] if part)
        for ctx_type, patterns in AppContext.APP_PATTERNS.items():
            for pattern in patterns:
                if pattern in searchable:
                    return AppContext(ctx_type, title, app_name or pattern)

        return AppContext(AppContext.UNKNOWN, title, app_name)


class WindowsWindowManager(WindowManager):
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _SW_RESTORE = 9

    def _get_foreground_hwnd(self):
        try:
            user32 = ctypes.windll.user32
            user32.GetForegroundWindow.restype = wintypes.HWND
            return user32.GetForegroundWindow()
        except Exception:
            return 0

    def _get_window_title(self, hwnd) -> str:
        if not hwnd:
            return ""
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
        if not hwnd:
            return ""
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
        except Exception:
            return ""
        finally:
            kernel32.CloseHandle(handle)
        return ""

    def detect_active_window(self) -> AppContext:
        hwnd = self._get_foreground_hwnd()
        title = self._get_window_title(hwnd).strip().lower()
        app_name = ""
        if _WINDOW_BACKEND in ("auto", "win32"):
            app_name = self._get_process_name(hwnd)
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
        try:
            user32 = ctypes.windll.user32
            user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.ShowWindow.restype = wintypes.BOOL
            user32.SetForegroundWindow.argtypes = [wintypes.HWND]
            user32.SetForegroundWindow.restype = wintypes.BOOL
            user32.ShowWindow(target, self._SW_RESTORE)
            user32.SetForegroundWindow(target)
        except Exception:
            pass


if IS_WINDOWS:
    _platform_services = build_windows_services(_CLIPBOARD_BACKEND, _WINDOW_BACKEND)
else:
    _platform_services = build_linux_services(_CLIPBOARD_BACKEND, _WINDOW_BACKEND)

_system_integration = _platform_services.system
_clipboard_manager = _platform_services.clipboard
_hotkey_manager = _platform_services.hotkeys
_window_manager = _platform_services.windows
notifications.set_sender(_system_integration.notify)


def detect_active_window() -> AppContext:
    return _window_manager.detect_active_window()


# ============================================================
# Text Analysis — Heuristic Classification
# ============================================================

try:
    from langdetect import detect as _langdetect_detect
    from langdetect import DetectorFactory
    DetectorFactory.seed = 0
    _LANGDETECT_AVAILABLE = True
except ImportError:
    _LANGDETECT_AVAILABLE = False


@dataclass
class TextAnalysis:
    language: str = "en"
    is_code: bool = False
    is_formal: bool = True
    length: int = 0
    has_errors: bool = False
    looks_like: str = "prose"
    code_language: str = ""


_CODE_INDICATORS = [
    r'^\s*(def |class |import |from \w+ import|function |const |let |var )',
    r'[{};]\s*$',
    r'^\s*(public |private |protected |static |async |await )',
    r'^\s*#include|^\s*package |^\s*using ',
    r'[!=]=',
    r'->\s*\w+',
    r'^\s*<\w+[\s/>]',
]

_INFORMAL_MARKERS = frozenset([
    "lol", "omg", "wtf", "bruh", "nah", "gonna", "wanna", "gotta",
    "idk", "imo", "tbh", "lmao", "smh", "fr", "ngl", "asap", "pls",
    "plz", "thx", "ty", "np",
])


def analyze_text(text: str) -> TextAnalysis:
    """Analyze text using heuristics (no LLM). Fast, runs on every intercept."""
    result = TextAnalysis()
    stripped = text.strip()
    result.length = len(stripped)

    # --- Language detection ---
    if _LANGDETECT_AVAILABLE:
        try:
            result.language = _langdetect_detect(stripped[:500])
        except Exception:
            result.language = "en"

    # --- Code detection ---
    code_line_count = 0
    lines = stripped.split('\n')
    sample = lines[:30]
    for line in sample:
        for pattern in _CODE_INDICATORS:
            if _re.search(pattern, line):
                code_line_count += 1
                break
    code_ratio = code_line_count / max(len(sample), 1)
    result.is_code = code_ratio > 0.3

    # Code language heuristic
    if result.is_code:
        if _re.search(r'\bdef\b.*:\s*$|^\s*import\s+\w+|from\s+\w+\s+import', stripped, _re.MULTILINE):
            result.code_language = "python"
        elif _re.search(r'\bfunction\b|\bconst\b|\blet\b|\bconsole\.', stripped):
            result.code_language = "javascript"
        elif _re.search(r'\bfn\b|\blet\s+mut\b|\bimpl\b', stripped):
            result.code_language = "rust"
        elif _re.search(r'\bfunc\b.*\{|package\s+\w+|:=', stripped):
            result.code_language = "go"

    # --- Formality ---
    words_lower = stripped.lower().split()
    informal_count = sum(1 for w in words_lower if w.strip('.,!?') in _INFORMAL_MARKERS)
    result.is_formal = informal_count < 2

    # --- "Looks like" classification ---
    if result.is_code:
        result.looks_like = "code"
    elif stripped.startswith('{') and stripped.endswith('}'):
        result.looks_like = "json"
    elif _re.match(r'https?://', stripped):
        result.looks_like = "url"
    elif _re.search(r'^(diff --git|@@\s)', stripped, _re.MULTILINE):
        result.looks_like = "commit_diff"
    elif _re.search(r'^\s*[-*]\s', stripped, _re.MULTILINE) and stripped.count('\n') > 2:
        result.looks_like = "list"
    elif _re.search(r'(action items|next steps|attendees|agenda)', stripped.lower()):
        result.looks_like = "meeting_notes"
    elif _re.search(r'(traceback|error|exception|stack trace)', stripped.lower()):
        result.looks_like = "error"
    elif _re.search(r'^\d{4}-\d{2}-\d{2}.*\[', stripped, _re.MULTILINE):
        result.looks_like = "log"
    elif _re.search(r'(dear |hi |hello |subject:|re:)', stripped.lower()[:100]):
        result.looks_like = "email_draft"

    # --- Basic error detection ---
    if not result.is_code and result.language == "en":
        if '  ' in stripped or _re.search(r'\.\s+[a-z]', stripped):
            result.has_errors = True

    return result


# ============================================================
# Smart Command Suggestions
# ============================================================

_DEFAULT_CONTEXT_PRIORITIES: dict[str, list[str]] = {
    "terminal":  ["command", "explain", "regex", "docstring", "review"],
    "browser":   ["summarize", "translate", "rewrite", "bullets", "title"],
    "ide":       ["docstring", "review", "explain", "gitcommit", "regex", "fmt"],
    "chat":      ["rewrite", "tone", "translate", "tweet"],
    "docs":      ["rewrite", "summarize", "bullets", "title", "meeting"],
    "unknown":   ["summarize", "rewrite", "explain", "fmt", "translate"],
}

_TEXT_TYPE_PRIORITIES: dict[str, list[str]] = {
    "code":          ["docstring", "review", "explain", "fmt", "gitcommit"],
    "json":          ["fmt", "explain", "redact"],
    "commit_diff":   ["gitcommit", "review", "summarize"],
    "list":          ["bullets", "todo", "summarize"],
    "meeting_notes": ["meeting", "todo", "summarize", "bullets"],
    "error":         ["explain", "review"],
    "log":           ["explain", "summarize", "redact"],
    "email_draft":   ["email", "rewrite", "tone"],
    "url":           ["wiki", "summarize"],
    "prose":         ["summarize", "rewrite", "translate", "bullets", "title"],
}


def get_smart_suggestions(
    app_ctx: AppContext,
    text_analysis: TextAnalysis,
    commands: dict,
    pattern_scores: dict[str, float] | None = None,
    max_starred: int = 3,
) -> list[tuple[str, dict, bool]]:
    """Return ordered list of (cmd_name, cmd_config, is_starred).
    First `max_starred` entries have is_starred=True."""

    scores: dict[str, float] = {}

    # 1. Context-type base score
    ctx_cmds = CONFIG.get("context_priorities", {}).get(
        app_ctx.context_type,
        _DEFAULT_CONTEXT_PRIORITIES.get(app_ctx.context_type, [])
    )
    for i, cmd_name in enumerate(ctx_cmds):
        if cmd_name in commands:
            scores[cmd_name] = scores.get(cmd_name, 0) + max(0, 10 - i)

    # 2. Text-type score
    text_cmds = _TEXT_TYPE_PRIORITIES.get(text_analysis.looks_like, [])
    for i, cmd_name in enumerate(text_cmds):
        if cmd_name in commands:
            scores[cmd_name] = scores.get(cmd_name, 0) + max(0, 8 - i)

    # 3. Language-specific boost
    if text_analysis.language != "en":
        if "trans" in commands:
            scores["trans"] = scores.get("trans", 0) + 5

    # 4. Code-specific boost
    if text_analysis.is_code:
        for cmd in ["docstring", "review", "explain", "fmt"]:
            if cmd in commands:
                scores[cmd] = scores.get(cmd, 0) + 3

    # 5. Informality boost
    if not text_analysis.is_formal:
        if "translate" in commands:
            scores["translate"] = scores.get("translate", 0) + 4
        if "rewrite" in commands:
            scores["rewrite"] = scores.get("rewrite", 0) + 3

    # 6. PatternLearner scores
    if pattern_scores:
        for cmd_name, learned_score in pattern_scores.items():
            if cmd_name in commands:
                scores[cmd_name] = scores.get(cmd_name, 0) + learned_score

    # Sort by score descending
    sorted_cmds = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    result: list[tuple[str, dict, bool]] = []
    starred_count = 0
    seen: set[str] = set()
    for cmd_name, score in sorted_cmds:
        if cmd_name not in commands:
            continue
        is_starred = starred_count < max_starred and score > 0
        if is_starred:
            starred_count += 1
        result.append((cmd_name, commands[cmd_name], is_starred))
        seen.add(cmd_name)

    # Append remaining commands alphabetically
    for cmd_name in sorted(commands):
        if cmd_name not in seen:
            result.append((cmd_name, commands[cmd_name], False))

    return result


# ============================================================
# Pattern Learner
# ============================================================

class PatternLearner:
    """Learns command preferences from history. Reads JSONL, computes
    per-context usage-frequency weights."""

    MIN_SAMPLES = 20
    DOMINATE_SAMPLES = 100

    def __init__(self, history_path: Path):
        self._history_path = history_path
        self._samples: int = 0
        self._context_counts: dict[str, dict[str, int]] = {}  # app_context → {cmd: count}
        self._total_counts: dict[str, int] = {}

    def load(self) -> None:
        """Read history file and compute frequency tables."""
        self._context_counts.clear()
        self._total_counts.clear()
        self._samples = 0
        for entry in load_history_entries_safe(self._history_path, limit=None):
            cmd = entry.get("command", "")
            if not cmd:
                continue
            ctx = entry.get("app_context", "unknown")
            self._samples += 1
            self._total_counts[cmd] = self._total_counts.get(cmd, 0) + 1
            if ctx not in self._context_counts:
                self._context_counts[ctx] = {}
            self._context_counts[ctx][cmd] = self._context_counts[ctx].get(cmd, 0) + 1

    def get_scores(self, app_context: str) -> dict[str, float]:
        """Return command → score dict based on learned patterns."""
        if self._samples < self.MIN_SAMPLES:
            return {}

        blend = min(1.0, (self._samples - self.MIN_SAMPLES) /
                    max(1, self.DOMINATE_SAMPLES - self.MIN_SAMPLES))

        counts = self._context_counts.get(app_context, self._total_counts)
        if not counts:
            counts = self._total_counts

        total = sum(counts.values()) or 1
        scores: dict[str, float] = {}
        for cmd, count in counts.items():
            scores[cmd] = (count / total) * blend * 15
        return scores

    @property
    def sample_count(self) -> int:
        return self._samples


# ============================================================
# Config Hot-Reload
# ============================================================

def _reload_config() -> None:
    """Reload config.yaml and update CONFIG commands in place."""
    global _PLATFORM_CFG, _CLIPBOARD_BACKEND, _WINDOW_BACKEND, _CMD_ALLOWED_COMMANDS
    try:
        new_cfg = load_config()
        CONFIG.clear()
        CONFIG.update(new_cfg)
        notifications.configure_from_config(CONFIG)
        notifications.set_sender(_system_integration.notify)
        _sync_ui_mode_state()
        _register_personal_commands()
        _PLATFORM_CFG = CONFIG.get("platform", {})
        _CLIPBOARD_BACKEND = _PLATFORM_CFG.get("clipboard_backend", "auto")
        _WINDOW_BACKEND = _PLATFORM_CFG.get("window_backend", "auto")
        _CMD_ALLOWED_COMMANDS = CONFIG.get("command_security", {}).get(
            "allowed_commands",
            _CMD_ALLOWED_COMMANDS,
        )
        # Initialize usage counters for any new commands
        for cmd_name in CONFIG["commands"]:
            if cmd_name not in _usage_counts:
                _usage_counts[cmd_name] = 0
        _init_llm()
        cmd_count = len(CONFIG["commands"])
        TUI.micro_log(f"{TUI.GREEN}✓{TUI.RESET} Config reloaded — {cmd_count} commands loaded")
    except Exception as exc:
        notifications.log_error("Config reload failed: %s", exc, exc_info=exc)
        TUI.warn(f"Config reload failed: {exc}")


def _start_config_watcher() -> None:
    """Watch config.yaml for changes using watchdog."""
    global _config_watcher_started
    if _config_watcher_started:
        return
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler

        class ConfigHandler(FileSystemEventHandler):
            def __init__(self):
                self._last_reload = 0.0

            def on_modified(self, event):
                if event.src_path.endswith("config.yaml"):
                    # Debounce: ignore events within 1 second of last reload
                    now = time.time()
                    if now - self._last_reload < 1.0:
                        return
                    self._last_reload = now
                    _reload_config()

        observer = Observer()
        observer.schedule(ConfigHandler(), str(_PROJECT_ROOT), recursive=False)
        observer.daemon = True
        observer.start()
        _config_watcher_started = True
    except ImportError:
        TUI.warn("watchdog not installed — config hot-reload disabled")
    except Exception as exc:
        TUI.warn(f"Config watcher failed to start: {exc}")


# ============================================================
# LLM Integration
# ============================================================

_llm_client = None
_llm_ready = False
_llm_provider = ""
_llm_model = ""

_llm_fallback_client = None
_llm_fallback_ready = False
_llm_fallback_provider = ""
_llm_fallback_model = ""

LLM_MODE = "needs_setup"  # "live" | "mock" | "needs_setup"
LLM_STATE = LLM_STATE_NEEDS_SETUP
_llm_explicit_mock = False
_force_mock_llm = False


def _save_llm_config(provider: str, model: str, mode: str = "auto") -> None:
    """Persist provider/model/mode to config.yaml without storing API keys."""
    try:
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}
        if "llm" not in data or not isinstance(data["llm"], dict):
            data["llm"] = {}
        data["llm"]["provider"] = provider
        data["llm"]["model"] = model
        data["llm"]["mode"] = mode
        data["llm"].pop("api_key", None)
        with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception as exc:
        TUI.warn(f"Could not save LLM config: {exc}")


def _prompt_llm_setup(command_name: str = "") -> LLMSetupChoice:
    if _llm_setup_handler is not None:
        try:
            return _llm_setup_handler(command_name)
        except Exception as exc:
            notifications.log_error("GUI LLM setup handler failed: %s", exc, exc_info=exc)
    c = TUI.CYAN
    r = TUI.RESET
    b = TUI.BOLD
    d = TUI.DIM

    print()
    lines = [
        f"  {d}This command needs a real LLM backend before it can run.{r}",
    ]
    if command_name:
        lines.append(f"  {d}Requested command:{r} {command_name}")
    lines.extend([
        f"",
        f"  {b}Choose what to do:{r}",
    ])
    TUI.box("LLM Setup Required", lines, TUI.YELLOW)

    options = ["configure now", "use mock mode", "cancel"]
    if sys.stdin.isatty():
        choice = TUI.selector(options)
    else:
        labels = "  ".join(f"[{i+1}] {o}" for i, o in enumerate(options))
        print(f"  {d}{labels}{r}")
        try:
            raw = input(f"  {c}Choice (1-{len(options)}):{r} ").strip()
        except (EOFError, KeyboardInterrupt):
            raw = str(len(options))
        choice = {str(i + 1): i for i in range(len(options))}.get(raw)

    if choice is None or choice == 2:
        return LLMSetupChoice(action="cancel")
    if choice == 1:
        return LLMSetupChoice(action="mock")

    provider_options = list(PROVIDER_DEFAULT_MODELS.keys())
    if sys.stdin.isatty():
        provider_index = TUI.selector(provider_options)
    else:
        labels = "  ".join(f"[{i+1}] {o}" for i, o in enumerate(provider_options))
        print(f"  {d}{labels}{r}")
        try:
            raw = input(f"  {c}Provider (1-{len(provider_options)}):{r} ").strip()
        except (EOFError, KeyboardInterrupt):
            return LLMSetupChoice(action="cancel")
        provider_index = {str(i + 1): i for i in range(len(provider_options))}.get(raw)
    if provider_index is None:
        return LLMSetupChoice(action="cancel")

    provider = provider_options[provider_index]
    default_model = PROVIDER_DEFAULT_MODELS.get(provider, "gpt-4o-mini")
    print(f"  {d}Enter your {provider} API key:{r}")
    try:
        api_key = input(f"  {c}{b}API Key:{r} ").strip()
    except (EOFError, KeyboardInterrupt):
        return LLMSetupChoice(action="cancel")
    if not api_key:
        return LLMSetupChoice(action="cancel")
    try:
        model = input(f"  {c}{b}Model{r} {d}[{default_model}]{r}{c}{b}:{r} ").strip()
    except (EOFError, KeyboardInterrupt):
        model = ""
    return LLMSetupChoice(
        action="configure",
        provider=provider,
        api_key=api_key,
        model=model or default_model,
    )


def _init_llm_client(provider: str, api_key: str, model: str):
    """Create an OpenAI client for the given provider. Returns (client, model) or (None, "")."""
    try:
        from openai import OpenAI

        default_model = PROVIDER_DEFAULT_MODELS.get(provider, "gpt-4o-mini")
        resolved_model = model or default_model

        if provider == "openai":
            client = OpenAI(api_key=api_key)
        elif provider in PROVIDER_BASE_URLS:
            client = OpenAI(api_key=api_key, base_url=PROVIDER_BASE_URLS[provider])
        else:
            TUI.warn(f"Unknown LLM provider: '{provider}'.")
            return None, ""

        return client, resolved_model
    except ImportError:
        TUI.warn("openai package not installed. Run: pip install openai")
        return None, ""
    except Exception as exc:
        TUI.error(f"LLM client init failed for {provider}: {exc}")
        return None, ""


def _init_llm() -> None:
    """Initialize LLM client from config/secrets/env and set the honest runtime state."""
    global _llm_client, _llm_ready, _llm_provider, _llm_model
    global _llm_fallback_client, _llm_fallback_ready, _llm_fallback_provider, _llm_fallback_model
    global LLM_MODE, LLM_STATE, _llm_explicit_mock

    _llm_client = None
    _llm_ready = False
    _llm_provider = ""
    _llm_model = ""
    _llm_fallback_client = None
    _llm_fallback_ready = False
    _llm_fallback_provider = ""
    _llm_fallback_model = ""

    resolution = resolve_llm_state(
        CONFIG,
        env=os.environ,
        secrets=load_llm_secrets(),
        force_mock=_force_mock_llm,
    )

    LLM_STATE = resolution.state
    _llm_explicit_mock = resolution.explicit_mock
    if resolution.state == LLM_STATE_MOCK:
        LLM_MODE = "mock"
        return
    if resolution.state != LLM_STATE_READY:
        LLM_MODE = "needs_setup"
        return

    client, resolved_model = _init_llm_client(resolution.provider, resolution.api_key, resolution.model)
    if client:
        _llm_client = client
        _llm_model = resolved_model
        _llm_provider = resolution.provider
        _llm_ready = True
        LLM_MODE = "live"
        LLM_STATE = LLM_STATE_READY
    else:
        LLM_MODE = "needs_setup"
        LLM_STATE = LLM_STATE_NEEDS_SETUP
        return

    # Initialize fallback provider if configured
    llm_cfg = CONFIG.get("llm", {})
    fb_cfg = llm_cfg.get("fallback", {})
    fb_provider = fb_cfg.get("provider", "").strip().lower() if isinstance(fb_cfg, dict) else ""
    fb_api_key = fb_cfg.get("api_key", "").strip() if isinstance(fb_cfg, dict) else ""
    fb_model = fb_cfg.get("model", "").strip() if isinstance(fb_cfg, dict) else ""

    if not fb_api_key:
        fb_api_key = resolution.api_key  # reuse primary key if not specified

    if fb_provider and fb_provider != resolution.provider:
        fb_client, fb_resolved = _init_llm_client(fb_provider, fb_api_key, fb_model)
        if fb_client:
            _llm_fallback_client = fb_client
            _llm_fallback_model = fb_resolved
            _llm_fallback_provider = fb_provider
            _llm_fallback_ready = True


_last_llm_provider_used = ""


def _llm_call(prompt: str, model: str = "") -> str:
    """Send prompt to configured LLM with auto-fallback. Optional model overrides the global default."""
    global _last_llm_provider_used

    if LLM_STATE == LLM_STATE_MOCK:
        _last_llm_provider_used = "mock"
        return _mock_llm_call(prompt)
    if not _llm_ready or not _llm_client:
        raise RuntimeError("LLM backend is not configured")

    use_model = model or _llm_model
    try:
        response = _llm_client.chat.completions.create(
            model=use_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.7,
        )
        _last_llm_provider_used = _llm_provider
        return response.choices[0].message.content.strip()
    except Exception as exc:
        TUI.warn(f"Primary LLM ({_llm_provider}) failed: {exc}")

        # Auto-fallback to secondary provider
        if _llm_fallback_ready and _llm_fallback_client:
            fb_model = model or _llm_fallback_model
            try:
                TUI.status("🔄", f"Retrying with fallback ({_llm_fallback_provider})...", TUI.YELLOW)
                response = _llm_fallback_client.chat.completions.create(
                    model=fb_model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=500,
                    temperature=0.7,
                )
                _last_llm_provider_used = f"{_llm_fallback_provider} (fallback)"
                return response.choices[0].message.content.strip()
            except Exception as fb_exc:
                TUI.error(f"Fallback LLM ({_llm_fallback_provider}) also failed: {fb_exc}")

        raise RuntimeError(f"LLM request failed: {exc}")


def _mock_llm_call(prompt: str) -> str:
    """Mock fallback — returns placeholder text without any API call."""
    # Extract the actual user text from the prompt template
    lines = prompt.strip().split("\n")
    user_text = lines[-1] if lines else prompt
    return f"[Mock Mode] {user_text[:120]}"


def _ensure_llm_backend(command_name: str = "") -> bool:
    global LLM_MODE, LLM_STATE, _llm_explicit_mock

    if LLM_STATE == LLM_STATE_READY and _llm_ready:
        return True
    if LLM_STATE == LLM_STATE_MOCK:
        return True

    def _prompt_user() -> LLMSetupChoice:
        return _prompt_llm_setup(command_name)

    def _persist_config(provider: str, model: str) -> None:
        mode = "mock" if provider == "mock" else "auto"
        if provider != "mock":
            CONFIG.setdefault("llm", {})["provider"] = provider
        CONFIG.setdefault("llm", {})["model"] = model
        CONFIG.setdefault("llm", {})["mode"] = mode
        _save_llm_config(CONFIG["llm"].get("provider", provider), model, mode=mode)

    def _persist_secret(api_key: str) -> None:
        save_llm_secrets(api_key)

    def _reinit() -> bool:
        _init_llm()
        return LLM_STATE == LLM_STATE_READY and _llm_ready

    resolved = resolve_llm_state(
        CONFIG,
        env=os.environ,
        secrets=load_llm_secrets(),
        force_mock=_force_mock_llm,
    )
    next_state = maybe_prompt_llm_setup(
        resolved,
        _prompt_user,
        _persist_config,
        _persist_secret,
        _reinit,
    )
    _llm_explicit_mock = next_state == LLM_STATE_MOCK
    if next_state == LLM_STATE_MOCK:
        CONFIG.setdefault("llm", {})["mode"] = "mock"
        LLM_MODE = "mock"
        LLM_STATE = LLM_STATE_MOCK
        return True
    if next_state == LLM_STATE_READY:
        LLM_MODE = "live"
        LLM_STATE = LLM_STATE_READY
        return True
    LLM_MODE = "needs_setup"
    LLM_STATE = LLM_STATE_NEEDS_SETUP
    notify(
        "LLM Setup Required",
        "This command needs a configured LLM provider and API key.",
        is_error=True,
        critical=True,
        dedupe_key="llm_setup_required",
    )
    return False


def _llm_classify(text: str, commands: dict) -> dict | None:
    """Ask LLM to classify text intent. Returns {"name": ..., "payload": ..., "confidence": float} or None."""
    if LLM_STATE != LLM_STATE_READY or not _llm_ready:
        return None

    cmd_list = "\n".join(
        f"- {name}: {cmd.get('description', '')}"
        for name, cmd in commands.items()
    )

    prompt = (
        f"Classify the following text into one of these commands:\n{cmd_list}\n\n"
        f"Text: \"{text}\"\n\n"
        f"Reply with ONLY the command name and your confidence score (0.0-1.0), "
        f"separated by a colon. Example: summarize:0.85\n"
        f"If none match, reply \"unknown:0.0\"."
    )

    try:
        result = _llm_call(prompt).strip().lower()
        # Parse "command_name:confidence" format
        if ":" in result:
            parts = result.split(":", 1)
            cmd_name = parts[0].strip()
            try:
                confidence = float(parts[1].strip())
            except (ValueError, IndexError):
                confidence = 0.5
        else:
            cmd_name = result.strip()
            confidence = 0.5
        if cmd_name in commands and cmd_name != "unknown":
            return {"name": cmd_name, "payload": text, "confidence": confidence}
    except Exception:
        pass
    return None


# ============================================================
# TUI — Styled Terminal Output
# ============================================================

class TUI:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"

    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    BG_BLACK = "\033[40m"
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"
    BG_MAGENTA = "\033[45m"
    BG_CYAN = "\033[46m"
    BG_WHITE = "\033[47m"

    TOP_LEFT = "╭"
    TOP_RIGHT = "╮"
    BOT_LEFT = "╰"
    BOT_RIGHT = "╯"
    HORIZ = "─"
    VERT = "│"

    _print_lock = threading.Lock()

    @staticmethod
    def _width() -> int:
        return shutil.get_terminal_size((60, 20)).columns

    @classmethod
    def _strip_ansi(cls, text: str) -> str:
        import re
        return re.sub(r"\033\[[0-9;]*m", "", text)

    @classmethod
    def _timestamp(cls) -> str:
        return f"{cls.DIM}{datetime.now().strftime('%H:%M:%S')}{cls.RESET}"

    @classmethod
    def _read_key(cls) -> str:
        """Read a single keypress in raw mode. Returns 'left', 'right', 'enter', etc."""
        if IS_WINDOWS:
            while True:
                ch = msvcrt.getwch()
                if ch in ('\x00', '\xe0'):
                    ext = msvcrt.getwch()
                    if ext == 'K':
                        return 'left'
                    if ext == 'M':
                        return 'right'
                    if ext == 'H':
                        return 'up'
                    if ext == 'P':
                        return 'down'
                    continue
                if ch in ('\r', '\n'):
                    return 'enter'
                if ch == '\x1b':
                    return 'escape'
                if ch == '\x03':
                    return 'ctrl_c'
                return ch

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == '\x1b':
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    ch2 = sys.stdin.read(1)
                    if ch2 == '[':
                        ch3 = sys.stdin.read(1)
                        if ch3 == 'D':
                            return 'left'
                        elif ch3 == 'C':
                            return 'right'
                return 'escape'
            elif ch in ('\r', '\n'):
                return 'enter'
            elif ch == '\x03':
                return 'ctrl_c'
            else:
                return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    @classmethod
    def selector(cls, options: list[str]) -> int | None:
        """Arrow-key horizontal selector. Returns index or None if cancelled."""
        current = 0

        while True:
            parts = []
            for i, opt in enumerate(options):
                if i == current:
                    parts.append(f"{cls.BG_CYAN}{cls.BLACK}{cls.BOLD} {opt} {cls.RESET}")
                else:
                    parts.append(f"{cls.DIM} {opt} {cls.RESET}")
            line = "  ".join(parts)
            hint = f"{cls.DIM}(← → to move, enter to select){cls.RESET}"
            sys.stdout.write(f"\r  {line}   {hint}\033[K")
            sys.stdout.flush()

            key = cls._read_key()
            if key == 'left':
                current = (current - 1) % len(options)
            elif key == 'right':
                current = (current + 1) % len(options)
            elif key == 'enter':
                parts = []
                for i, opt in enumerate(options):
                    if i == current:
                        parts.append(f"{cls.GREEN}{cls.BOLD} {opt} {cls.RESET}")
                    else:
                        parts.append(f"{cls.DIM} {opt} {cls.RESET}")
                sys.stdout.write(f"\r  {'  '.join(parts)}\033[K\n")
                sys.stdout.flush()
                return current
            elif key in ('ctrl_c', 'escape'):
                sys.stdout.write(f"\r\033[K\n")
                sys.stdout.flush()
                return None

    @classmethod
    def _print(cls, *args, **kwargs) -> None:
        with cls._print_lock:
            stream = getattr(sys, "stdout", None)
            if stream is None or not getattr(stream, "write", None):
                return
            print(*args, **kwargs)
            flush = getattr(stream, "flush", None)
            if flush is not None:
                flush()

    @classmethod
    def _emit_terminal(cls, kind: str, text: str, *, log_level: str = "debug", exc_info=None) -> None:
        plain = cls._strip_ansi(text)
        if log_level == "error":
            notifications.log_error("%s", plain, exc_info=exc_info)
        elif log_level == "warning":
            notifications.log_warning("%s", plain)
        elif log_level == "info":
            notifications.log_info("%s", plain)
        else:
            notifications.log_debug("%s", plain)

        if notifications.should_print_terminal(kind):
            cls._print(text)

    @classmethod
    def box(cls, title: str, lines: list[str], color: str = "") -> None:
        if not notifications.should_print_terminal("box"):
            return
        c = color or cls.CYAN
        w = cls._width() - 2
        inner = w - 2

        title_text = f" {title} "
        pad = inner - len(title_text)
        left_pad = pad // 2
        right_pad = pad - left_pad

        with cls._print_lock:
            print(f"{c}{cls.TOP_LEFT}{cls.HORIZ * left_pad}{cls.BOLD}{title_text}{cls.RESET}{c}{cls.HORIZ * right_pad}{cls.TOP_RIGHT}{cls.RESET}")
            for line in lines:
                visible_len = len(cls._strip_ansi(line))
                spacing = max(0, inner - visible_len)
                print(f"{c}{cls.VERT}{cls.RESET} {line}{' ' * spacing}{c}{cls.VERT}{cls.RESET}")
            print(f"{c}{cls.BOT_LEFT}{cls.HORIZ * inner}{cls.HORIZ * 2}{cls.BOT_RIGHT}{cls.RESET}")
            sys.stdout.flush()

    @classmethod
    def banner(cls) -> None:
        if not notifications.should_print_terminal("banner"):
            return
        w = cls._width() - 2
        inner = w - 2
        c = cls.MAGENTA

        logo = [
            "  ▄▀█ █▀▀ ▀█▀ █ █▀█ █▄░█",
            "  █▀█ █▄▄ ░█░ █ █▄█ █░▀█",
            "",
            "  █▀▀ █░░ █▀█ █░█░█",
            "  █▀░ █▄▄ █▄█ ▀▄▀▄▀",
        ]

        with cls._print_lock:
            print(f"\n{c}{cls.TOP_LEFT}{cls.HORIZ * inner}{cls.HORIZ * 2}{cls.TOP_RIGHT}{cls.RESET}")
            for line in logo:
                visible_len = len(line)
                spacing = max(0, inner - visible_len)
                print(f"{c}{cls.VERT}{cls.RESET} {cls.BOLD}{cls.MAGENTA}{line}{cls.RESET}{' ' * spacing}{c}{cls.VERT}{cls.RESET}")
            print(f"{c}{cls.BOT_LEFT}{cls.HORIZ * inner}{cls.HORIZ * 2}{cls.BOT_RIGHT}{cls.RESET}\n")
            sys.stdout.flush()

    @classmethod
    def header_line(cls) -> None:
        """Compact single-line header shown after banner collapses."""
        if not notifications.should_print_terminal("header"):
            return
        elapsed = int(time.time() - _start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        uptime = f"{h}:{m:02d}:{s:02d}"

        if LLM_STATE == LLM_STATE_READY:
            mode_str = f"{cls.GREEN}llm ready{cls.RESET} {cls.DIM}· {_llm_provider}/{_llm_model}{cls.RESET}"
        elif LLM_STATE == LLM_STATE_MOCK:
            mode_str = f"{cls.YELLOW}llm mock{cls.RESET} {cls.DIM}· explicit{cls.RESET}"
        else:
            mode_str = f"{cls.YELLOW}setup required{cls.RESET}"

        cmd_count = len(CONFIG.get("commands", {}))
        line = (
            f"  {cls.MAGENTA}{cls.BOLD}▶ ACTIONFLOW{cls.RESET}  "
            f"{cls.DIM}|{cls.RESET}  {mode_str}  "
            f"{cls.DIM}|{cls.RESET}  {cls.DIM}{cmd_count} commands{cls.RESET}  "
            f"{cls.DIM}|{cls.RESET}  {cls.DIM}uptime: {uptime}{cls.RESET}"
        )
        cls._emit_terminal("header", line, log_level="debug")

    @classmethod
    def status(cls, label: str, value: str, color: str = "") -> None:
        c = color or cls.WHITE
        cls._emit_terminal(
            "status",
            f"  {cls._timestamp()}  {c}{cls.BOLD}{label}{cls.RESET} {cls.DIM}{value}{cls.RESET}",
            log_level="debug",
        )

    @classmethod
    def success(cls, message: str) -> None:
        cls._emit_terminal(
            "success",
            f"  {cls._timestamp()}  {cls.GREEN}✓{cls.RESET} {message}",
            log_level="info",
        )

    @classmethod
    def warn(cls, message: str) -> None:
        cls._emit_terminal(
            "warn",
            f"  {cls._timestamp()}  {cls.YELLOW}⚠{cls.RESET} {message}",
            log_level="warning",
        )

    @classmethod
    def error(cls, message: str) -> None:
        cls._emit_terminal(
            "error",
            f"  {cls._timestamp()}  {cls.RED}✗{cls.RESET} {message}",
            log_level="error",
        )

    @classmethod
    def action(cls, icon: str, label: str, detail: str) -> None:
        cls._emit_terminal(
            "action",
            f"  {cls._timestamp()}  {cls.CYAN}{icon}{cls.RESET} {cls.BOLD}{label}{cls.RESET} {cls.DIM}→{cls.RESET} {detail}",
            log_level="info",
        )

    @classmethod
    def separator(cls) -> None:
        w = cls._width() - 4
        cls._emit_terminal("separator", f"  {cls.DIM}{cls.HORIZ * w}{cls.RESET}", log_level="debug")

    @classmethod
    def keybind_table(cls) -> None:
        if not notifications.should_print_terminal("box"):
            return
        with _undo_lock:
            undo_count = len(_undo_stack)

        if undo_count > 0:
            undo_suffix = f"  {cls.GREEN}(×{undo_count} available){cls.RESET}"
            undo_color = cls.YELLOW
        else:
            undo_suffix = f"  {cls.DIM}· empty{cls.RESET}"
            undo_color = cls.DIM

        rows = [
            (HOTKEY.upper(), "Intercept selected text", cls.CYAN, ""),
            (UNDO_HOTKEY.upper(), "Undo last replacement", undo_color, undo_suffix),
            ("CTRL+C", "Exit application", cls.RED, ""),
        ]
        lines = []
        for key, desc, color, suffix in rows:
            lines.append(f"  {color}{cls.BOLD}{key:<16}{cls.RESET} {cls.DIM}{desc}{cls.RESET}{suffix}")
        cls.box("Keybindings", lines, cls.CYAN)

    @classmethod
    def commands_table(cls) -> None:
        if not notifications.should_print_terminal("box"):
            return
        commands = CONFIG.get("commands", {})
        lines = []
        for name, cmd in commands.items():
            prefixes = ", ".join(cmd.get("prefixes", []))
            keywords = ", ".join(cmd.get("keywords", [])[:3])
            is_llm_cmd = cmd.get("llm_required", False)
            dimmed = is_llm_cmd and LLM_STATE != LLM_STATE_READY

            if is_llm_cmd:
                badge = f" {cls.MAGENTA}{cls.BOLD}[LLM]{cls.RESET}"
                if LLM_STATE == LLM_STATE_MOCK:
                    badge += f" {cls.YELLOW}[MOCK]{cls.RESET}"
                elif LLM_STATE == LLM_STATE_NEEDS_SETUP:
                    badge += f" {cls.YELLOW}[SETUP]{cls.RESET}"
            else:
                badge = f" {cls.CYAN}{cls.BOLD}[FAST]{cls.RESET}"

            count = _usage_counts.get(name, 0)
            counter = f" {cls.DIM}×{count}{cls.RESET}"

            if dimmed:
                lines.append(
                    f"  {cls.DIM}{name:<12} "
                    f"{prefixes:<20} "
                    f"{keywords}{cls.RESET}"
                    f"{badge}{counter}"
                )
            else:
                lines.append(
                    f"  {cls.CYAN}{cls.BOLD}{name:<12}{cls.RESET} "
                    f"{cls.DIM}{prefixes:<20}{cls.RESET} "
                    f"{cls.DIM}{keywords}{cls.RESET}"
                    f"{badge}{counter}"
                )
        cls.box("Commands", lines, cls.CYAN)

    @classmethod
    def llm_status_box(cls) -> None:
        if not notifications.should_print_terminal("box"):
            return
        if LLM_STATE == LLM_STATE_READY:
            lines = [
                f"  {cls.GREEN}{cls.BOLD}LIVE{cls.RESET}    {cls.DIM}Provider: {_llm_provider}{cls.RESET}",
                f"          {cls.DIM}Model: {_llm_model}{cls.RESET}",
            ]
            if _llm_fallback_ready:
                lines.append(
                    f"          {cls.DIM}Fallback: {_llm_fallback_provider} / {_llm_fallback_model}{cls.RESET}"
                )
            cls.box("LLM", lines, cls.GREEN)
        elif LLM_STATE == LLM_STATE_MOCK:
            lines = [
                f"  {cls.YELLOW}{cls.BOLD}MOCK MODE{cls.RESET}",
                f"  {cls.DIM}Mock mode is enabled explicitly via config, env, or CLI.{cls.RESET}",
            ]
            cls.box("LLM", lines, cls.YELLOW)
        else:
            lines = [
                f"  {cls.YELLOW}{cls.BOLD}SETUP REQUIRED{cls.RESET}",
                f"  {cls.DIM}Run any LLM command to configure provider, model, and API key.{cls.RESET}",
            ]
            cls.box("LLM", lines, cls.YELLOW)

    @classmethod
    def activity_entry(cls, cmd_name: str, input_text: str, output_text: str,
                       duration: float, is_llm: bool = False, is_error: bool = False,
                       trigger: str = "") -> None:
        """Print a single activity feed line."""
        ts = datetime.now().strftime('%H:%M:%S')

        if is_error:
            color = cls.RED
        elif is_llm:
            color = cls.MAGENTA
        else:
            color = cls.CYAN

        max_len: int = max(20, (cls._width() - 50) // 2)
        inp = input_text[:max_len] + ("..." if len(input_text) > max_len else "")
        out = output_text[:max_len] + ("..." if len(output_text) > max_len else "")

        check = f"{cls.GREEN}✓{cls.RESET}" if not is_error else f"{cls.RED}✗{cls.RESET}"
        trigger_tag = f"  {cls.DIM}[{trigger}]{cls.RESET}" if trigger else ""

        line = (
            f"  {cls.DIM}{ts}{cls.RESET}  "
            f"{color}{cls.BOLD}{cmd_name.upper():<10}{cls.RESET}  "
            f"{cls.DIM}\"{inp}\" → \"{out}\"{cls.RESET}   "
            f"{check} {cls.DIM}{duration:.1f}s{cls.RESET}{trigger_tag}"
        )
        cls._emit_terminal("activity", line, log_level="error" if is_error else "debug")

    @classmethod
    def activity_placeholder(cls) -> None:
        """Show empty activity feed at startup."""
        if not notifications.should_print_terminal("box"):
            return
        cls.box("Activity", [
            f"  {cls.DIM}No activity yet. Select text and press {HOTKEY.upper()}{cls.RESET}",
        ], cls.CYAN)

    @classmethod
    def micro_log(cls, message: str) -> None:
        """Append a timestamped message to the rolling 3-line micro-log."""
        ts = datetime.now().strftime('%H:%M:%S')
        entry = f"  {cls.DIM}{ts}{cls.RESET}  {message}"
        with _micro_log_lock:
            _micro_log.append(entry)
            if len(_micro_log) > _MICRO_LOG_MAX:
                _micro_log.pop(0)
        cls._emit_terminal("micro", entry, log_level="debug")


# ============================================================
# Command Picker — Tkinter Popup
# ============================================================

_TONE_STYLES = ["casual", "formal", "aggressive", "empathetic", "confident", "sarcastic", "diplomatic",
               "gen-z", "academic", "professional email", "encouraging"]
_TRANS_LANGS = [
    ("Japanese", "JP", "\U0001f1ef\U0001f1f5"), ("Spanish", "ES", "\U0001f1ea\U0001f1f8"),
    ("French", "FR", "\U0001f1eb\U0001f1f7"), ("German", "DE", "\U0001f1e9\U0001f1ea"),
    ("Chinese", "ZH", "\U0001f1e8\U0001f1f3"), ("Arabic", "AR", "\U0001f1f8\U0001f1e6"),
    ("Russian", "RU", "\U0001f1f7\U0001f1fa"), ("Korean", "KO", "\U0001f1f0\U0001f1f7"),
    ("Portuguese", "PT", "\U0001f1e7\U0001f1f7"), ("Italian", "IT", "\U0001f1ee\U0001f1f9"),
    ("Turkish", "TR", "\U0001f1f9\U0001f1f7"), ("Hindi", "HI", "\U0001f1ee\U0001f1f3"),
    ("Polish", "PL", "\U0001f1f5\U0001f1f1"), ("Dutch", "NL", "\U0001f1f3\U0001f1f1"),
]

if _TKINTER_AVAILABLE:

    class CommandPicker:
        """Frameless tkinter popup for picking a command to apply to selected text."""

        BG = "#1a0a2e"
        BG_ROW = "#1a0a2e"
        BG_HOVER = "#2a1a4e"
        BG_SELECTED = "#3a2a6e"
        FG = "#e0e0e0"
        FG_DIM = "#888888"
        BORDER_COLOR = "#00d4aa"
        BADGE_FAST = "#00d4aa"
        BADGE_LLM = "#d45cff"
        BADGE_MOCK = "#d4aa00"
        SEARCH_BG = "#0e0620"
        PREVIEW_FG = "#777777"
        MIN_WIDTH = 360
        MAX_HEIGHT = 400
        ROW_HEIGHT = 28

        BADGE_STAR = "#ffd700"
        BADGE_PERSONAL = "#ff8c00"

        def __init__(self, selected_text: str, commands: dict,
                     suggestions: list[tuple[str, dict, bool]] | None = None,
                     text_analysis: "TextAnalysis | None" = None,
                     app_context: "AppContext | None" = None):
            self._text = selected_text
            self._commands = commands
            self._suggestions = suggestions
            self._text_analysis = text_analysis
            self._app_context = app_context
            self._cmd_list: list[tuple[str, dict]] = list(commands.items())
            self._filtered: list[tuple[str, dict]] = list(self._cmd_list)
            self._selected_idx = 0
            self._result: tuple | None = None  # (cmd_name, cmd_config) or None
            self._submenu: str | None = None  # "tone" or "trans" or None
            self._sub_items: list = []
            self._sub_selected = 0
            self._custom_entry = None
            self._row_widgets: list = []
            self._is_searching = False

            _get_tk_root()
            self._root = tk.Toplevel()
            self._root.withdraw()
            self._root.overrideredirect(True)
            self._root.attributes("-topmost", True)
            self._root.configure(bg=self.BG, highlightbackground=self.BORDER_COLOR,
                                 highlightthickness=1)

            # Font
            try:
                self._font = tkfont.Font(family="DejaVu Sans Mono", size=10)
                self._font_bold = tkfont.Font(family="DejaVu Sans Mono", size=10, weight="bold")
                self._font_small = tkfont.Font(family="DejaVu Sans Mono", size=9)
            except Exception:
                self._font = tkfont.Font(family="Courier", size=10)
                self._font_bold = tkfont.Font(family="Courier", size=10, weight="bold")
                self._font_small = tkfont.Font(family="Courier", size=9)

            self._build_ui()
            self._position_window()
            self._root.deiconify()
            self._root.focus_force()
            self._search_var.set("")
            self._search_entry.focus_set()

        def _position_window(self) -> None:
            """Position popup at mouse cursor, clamped to screen edges."""
            self._root.update_idletasks()
            mx = self._root.winfo_pointerx()
            my = self._root.winfo_pointery()
            w = max(self.MIN_WIDTH, self._root.winfo_reqwidth())
            h = min(self.MAX_HEIGHT, self._root.winfo_reqheight())
            sw = self._root.winfo_screenwidth()
            sh = self._root.winfo_screenheight()

            x = mx + 10
            y = my + 10
            if x + w > sw:
                x = mx - w - 10
            if y + h > sh:
                y = my - h - 10
            x = max(0, x)
            y = max(0, y)
            self._root.geometry(f"{w}x{h}+{x}+{y}")

        def _build_ui(self) -> None:
            """Build the main popup layout."""
            # Header with close button
            header_frame = tk.Frame(self._root, bg=self.BG)
            header_frame.pack(fill="x")

            # Preview
            preview = self._text[:60] + ("..." if len(self._text) > 60 else "")
            tk.Label(header_frame, text=f'"{preview}"', bg=self.BG, fg=self.PREVIEW_FG,
                     font=self._font_small, anchor="w", padx=8, pady=4
                     ).pack(side="left", fill="x", expand=True)

            # Close button (✕)
            close_btn = tk.Label(header_frame, text="✕", bg=self.BG, fg=self.FG_DIM,
                                font=self._font_bold, cursor="hand2", padx=8, pady=4)
            close_btn.pack(side="right")
            close_btn.bind("<Button-1>", lambda e: self._on_escape())
            close_btn.bind("<Enter>", lambda e: close_btn.configure(fg="#ff4444"))
            close_btn.bind("<Leave>", lambda e: close_btn.configure(fg=self.FG_DIM))

            # Analysis summary line
            if self._text_analysis:
                ta = self._text_analysis
                parts = []
                if self._app_context and self._app_context.context_type != "unknown":
                    parts.append(self._app_context.context_type)
                parts.append(ta.language)
                if ta.is_code:
                    parts.append(f"code" + (f"({ta.code_language})" if ta.code_language else ""))
                elif not ta.is_formal:
                    parts.append("informal")
                parts.append(f"{ta.length} chars")
                analysis_line = " · ".join(parts)
                tk.Label(self._root, text=analysis_line, bg=self.BG, fg="#555555",
                         font=self._font_small, anchor="w", padx=8, pady=1
                         ).pack(fill="x")

            # Separator
            tk.Frame(self._root, bg=self.BORDER_COLOR, height=1).pack(fill="x")

            # Search
            search_frame = tk.Frame(self._root, bg=self.SEARCH_BG)
            search_frame.pack(fill="x")
            tk.Label(search_frame, text="\U0001f50d", bg=self.SEARCH_BG, fg=self.FG_DIM,
                     font=self._font_small).pack(side="left", padx=(8, 2))
            self._search_var = tk.StringVar()
            self._search_var.trace_add("write", lambda *_: self._on_search())
            self._search_entry = tk.Entry(
                search_frame, textvariable=self._search_var,
                bg=self.SEARCH_BG, fg=self.FG, insertbackground=self.FG,
                font=self._font, relief="flat", bd=0,
            )
            self._search_entry.pack(fill="x", padx=(0, 8), pady=4, expand=True, side="left")

            # Separator
            tk.Frame(self._root, bg=self.BORDER_COLOR, height=1).pack(fill="x")

            # Scrollable command list
            self._canvas_frame = tk.Frame(self._root, bg=self.BG)
            self._canvas_frame.pack(fill="both", expand=True)

            self._canvas = tk.Canvas(self._canvas_frame, bg=self.BG, highlightthickness=0,
                                     bd=0)
            self._scrollbar = tk.Scrollbar(self._canvas_frame, orient="vertical",
                                           command=self._canvas.yview)
            self._inner_frame = tk.Frame(self._canvas, bg=self.BG)

            self._inner_frame.bind("<Configure>",
                                   lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
            self._canvas.create_window((0, 0), window=self._inner_frame, anchor="nw")
            self._canvas.configure(yscrollcommand=self._scrollbar.set)

            self._canvas.pack(side="left", fill="both", expand=True)
            self._scrollbar.pack(side="right", fill="y")

            self._populate_rows()

            # Bindings
            self._root.bind("<Escape>", self._on_escape)
            self._root.bind("<Return>", self._on_enter)
            self._root.bind("<Up>", self._on_up)
            self._root.bind("<Down>", self._on_down)
            self._root.bind("<FocusOut>", self._on_focus_out)
            self._root.bind("<MouseWheel>", self._on_mousewheel)
            self._root.bind("<Button-4>", lambda e: self._canvas.yview_scroll(-3, "units"))
            self._root.bind("<Button-5>", lambda e: self._canvas.yview_scroll(3, "units"))
            for i in range(1, 10):
                self._root.bind(f"<Key-{i}>", self._on_number_key)

        def _populate_rows(self) -> None:
            """Fill the command list rows."""
            for w in self._row_widgets:
                w.destroy()
            self._row_widgets.clear()

            if self._submenu == "tone":
                self._populate_tone_submenu()
                return
            elif self._submenu == "trans":
                self._populate_trans_submenu()
                return

            # Use smart suggestions if available and not actively searching
            if self._suggestions and not self._is_searching:
                starred = [(n, c) for n, c, s in self._suggestions if s]
                rest = [(n, c) for n, c, s in self._suggestions if not s]

                if starred:
                    # "For You" header
                    header = tk.Frame(self._inner_frame, bg=self.SEARCH_BG)
                    header.pack(fill="x", padx=2, pady=(2, 0))
                    self._row_widgets.append(header)
                    tk.Label(header, text="  \u2605 For You", bg=self.SEARCH_BG,
                             fg=self.BADGE_STAR, font=self._font_bold, anchor="w",
                             padx=6, pady=3).pack(fill="x")

                    for i, (name, cmd) in enumerate(starred):
                        self._add_command_row(name, cmd, i, starred=True)

                    # "All Commands" header
                    header2 = tk.Frame(self._inner_frame, bg=self.SEARCH_BG)
                    header2.pack(fill="x", padx=2, pady=(4, 0))
                    self._row_widgets.append(header2)
                    tk.Label(header2, text="  All Commands", bg=self.SEARCH_BG,
                             fg=self.FG_DIM, font=self._font_bold, anchor="w",
                             padx=6, pady=3).pack(fill="x")

                    items = rest
                    offset = len(starred)
                else:
                    items = starred + rest
                    offset = 0
            else:
                items = self._filtered
                offset = 0

            for i, (name, cmd) in enumerate(items):
                self._add_command_row(name, cmd, i + offset)

            self._update_scroll_height()

        def _add_command_row(self, name: str, cmd: dict, idx: int,
                             starred: bool = False) -> None:
            """Add a single command row to the popup."""
            row = tk.Frame(self._inner_frame, bg=self.BG_ROW, cursor="hand2")
            row.pack(fill="x", padx=2, pady=1)
            self._row_widgets.append(row)

            is_llm = cmd.get("llm_required", False)
            is_mock_llm = is_llm and LLM_STATE == LLM_STATE_MOCK
            is_setup_llm = is_llm and LLM_STATE == LLM_STATE_NEEDS_SETUP
            is_personal = cmd.get("_personal", False)

            # Number
            num_label = str(idx + 1) if idx < 9 else " "
            fg_main = self.FG_DIM if (is_mock_llm or is_setup_llm) else self.FG
            tk.Label(row, text=num_label, bg=self.BG_ROW, fg=self.FG_DIM,
                     font=self._font_small, width=2).pack(side="left", padx=(6, 2))

            # Star indicator
            if starred:
                tk.Label(row, text="\u2605", bg=self.BG_ROW, fg=self.BADGE_STAR,
                         font=self._font_small).pack(side="left", padx=(0, 2))

            # Name
            display_name = name.replace("_", " ").title()
            if is_personal:
                display_name = name.replace("personal_", "").replace("_", " ").title()
            tk.Label(row, text=display_name, bg=self.BG_ROW, fg=fg_main,
                     font=self._font_bold, anchor="w", width=16).pack(side="left")

            # Description
            desc = cmd.get("description", "")[:30]
            tk.Label(row, text=desc, bg=self.BG_ROW, fg=self.FG_DIM,
                     font=self._font_small, anchor="w").pack(side="left", fill="x", expand=True)

            # Badge
            if is_personal:
                badge_text, badge_fg = "[ME]", self.BADGE_PERSONAL
            elif is_mock_llm:
                badge_text, badge_fg = "[MOCK]", self.BADGE_MOCK
            elif is_setup_llm:
                badge_text, badge_fg = "[SETUP]", self.BADGE_MOCK
            elif is_llm:
                badge_text, badge_fg = "[LLM]", self.BADGE_LLM
            else:
                badge_text, badge_fg = "[FAST]", self.BADGE_FAST
            tk.Label(row, text=badge_text, bg=self.BG_ROW, fg=badge_fg,
                     font=self._font_small).pack(side="right", padx=(4, 8))

            # Highlight
            if idx == self._selected_idx:
                self._set_row_bg(row, self.BG_SELECTED)

            # Mouse bindings
            row.bind("<Enter>", lambda e, r=row, j=idx: self._on_row_hover(r, j))
            row.bind("<Leave>", lambda e, r=row, j=idx: self._on_row_leave(r, j))
            row.bind("<Button-1>", lambda e, j=idx: self._on_row_click(j))
            for child in row.winfo_children():
                child.bind("<Enter>", lambda e, r=row, j=idx: self._on_row_hover(r, j))
                child.bind("<Leave>", lambda e, r=row, j=idx: self._on_row_leave(r, j))
                child.bind("<Button-1>", lambda e, j=idx: self._on_row_click(j))

        def _populate_tone_submenu(self) -> None:
            """Show the tone style picker."""
            self._sub_items = _TONE_STYLES
            self._sub_selected = 0

            # Back header
            back = tk.Frame(self._inner_frame, bg=self.SEARCH_BG, cursor="hand2")
            back.pack(fill="x", padx=2, pady=1)
            self._row_widgets.append(back)
            tk.Label(back, text="\u2190 back   Choose tone style", bg=self.SEARCH_BG,
                     fg=self.FG, font=self._font_bold, anchor="w", padx=8, pady=4
                     ).pack(fill="x")
            back.bind("<Button-1>", lambda e: self._back_to_main())
            for child in back.winfo_children():
                child.bind("<Button-1>", lambda e: self._back_to_main())

            for i, style in enumerate(self._sub_items):
                row = tk.Frame(self._inner_frame, bg=self.BG_ROW, cursor="hand2")
                row.pack(fill="x", padx=2, pady=1)
                self._row_widgets.append(row)

                fg = self.FG
                tk.Label(row, text=f"  {style.title()}", bg=self.BG_ROW, fg=fg,
                         font=self._font, anchor="w", padx=8, pady=3).pack(fill="x")

                if i == self._sub_selected:
                    self._set_row_bg(row, self.BG_SELECTED)

                idx = i
                row.bind("<Enter>", lambda e, r=row, j=idx: self._on_sub_hover(r, j))
                row.bind("<Leave>", lambda e, r=row, j=idx: self._on_sub_leave(r, j))
                row.bind("<Button-1>", lambda e, j=idx: self._on_sub_click(j))
                for child in row.winfo_children():
                    child.bind("<Enter>", lambda e, r=row, j=idx: self._on_sub_hover(r, j))
                    child.bind("<Leave>", lambda e, r=row, j=idx: self._on_sub_leave(r, j))
                    child.bind("<Button-1>", lambda e, j=idx: self._on_sub_click(j))

            self._update_scroll_height()

        def _populate_trans_submenu(self) -> None:
            """Show the language picker."""
            self._sub_items = _TRANS_LANGS
            self._sub_selected = 0

            # Back header
            back = tk.Frame(self._inner_frame, bg=self.SEARCH_BG, cursor="hand2")
            back.pack(fill="x", padx=2, pady=1)
            self._row_widgets.append(back)
            tk.Label(back, text="\u2190 back   Translate to...", bg=self.SEARCH_BG,
                     fg=self.FG, font=self._font_bold, anchor="w", padx=8, pady=4
                     ).pack(fill="x")
            back.bind("<Button-1>", lambda e: self._back_to_main())
            for child in back.winfo_children():
                child.bind("<Button-1>", lambda e: self._back_to_main())

            for i, (lang_name, code, flag) in enumerate(self._sub_items):
                row = tk.Frame(self._inner_frame, bg=self.BG_ROW, cursor="hand2")
                row.pack(fill="x", padx=2, pady=1)
                self._row_widgets.append(row)

                tk.Label(row, text=f"  {lang_name}", bg=self.BG_ROW, fg=self.FG,
                         font=self._font, anchor="w", padx=8, pady=3).pack(side="left", fill="x", expand=True)
                tk.Label(row, text=flag, bg=self.BG_ROW, font=self._font,
                         padx=8).pack(side="right")

                if i == self._sub_selected:
                    self._set_row_bg(row, self.BG_SELECTED)

                idx = i
                row.bind("<Enter>", lambda e, r=row, j=idx: self._on_sub_hover(r, j))
                row.bind("<Leave>", lambda e, r=row, j=idx: self._on_sub_leave(r, j))
                row.bind("<Button-1>", lambda e, j=idx: self._on_sub_click(j))
                for child in row.winfo_children():
                    child.bind("<Enter>", lambda e, r=row, j=idx: self._on_sub_hover(r, j))
                    child.bind("<Leave>", lambda e, r=row, j=idx: self._on_sub_leave(r, j))
                    child.bind("<Button-1>", lambda e, j=idx: self._on_sub_click(j))

            # Custom entry row
            custom_row = tk.Frame(self._inner_frame, bg=self.BG_ROW)
            custom_row.pack(fill="x", padx=2, pady=1)
            self._row_widgets.append(custom_row)
            tk.Label(custom_row, text="  + custom:", bg=self.BG_ROW, fg=self.FG_DIM,
                     font=self._font_small, padx=8).pack(side="left")
            self._custom_entry = tk.Entry(custom_row, bg=self.SEARCH_BG, fg=self.FG,
                                          insertbackground=self.FG, font=self._font_small,
                                          relief="flat", width=10)
            self._custom_entry.pack(side="left", padx=4, pady=2)
            self._custom_entry.bind("<Return>", self._on_custom_lang)

            self._update_scroll_height()

        def _update_scroll_height(self) -> None:
            """Update canvas scroll region and window height."""
            self._root.update_idletasks()
            content_h = self._inner_frame.winfo_reqheight()
            canvas_h = min(content_h, self.MAX_HEIGHT - 80)  # Leave room for preview+search
            self._canvas.configure(height=canvas_h)
            self._root.update_idletasks()
            # Reposition if needed
            w = max(self.MIN_WIDTH, self._root.winfo_reqwidth())
            h = min(self.MAX_HEIGHT, self._root.winfo_reqheight())
            self._root.geometry(f"{w}x{h}")

        def _set_row_bg(self, row: tk.Frame, bg: str) -> None:
            """Set background for a row and all its children."""
            row.configure(bg=bg)
            for child in row.winfo_children():
                try:
                    child.configure(bg=bg)
                except tk.TclError:
                    pass

        # ── Search ──
        def _on_search(self) -> None:
            q = self._search_var.get().lower()
            self._is_searching = bool(q)
            if not q:
                self._filtered = list(self._cmd_list)
            else:
                self._filtered = [
                    (name, cmd) for name, cmd in self._cmd_list
                    if q in name.lower()
                    or q in cmd.get("description", "").lower()
                    or any(q in kw.lower() for kw in cmd.get("keywords", []))
                    or any(q in p.lower() for p in cmd.get("prefixes", []))
                ]
            self._selected_idx = 0
            self._populate_rows()

        # ── Keyboard ──
        def _on_escape(self, event=None) -> None:
            if self._submenu:
                self._back_to_main()
            else:
                self._result = None
                self._root.destroy()

        def _on_enter(self, event=None) -> None:
            if self._submenu:
                self._on_sub_click(self._sub_selected)
            elif self._filtered:
                self._select_command(self._selected_idx)

        def _on_up(self, event=None) -> None:
            if self._submenu:
                count = len(self._sub_items)
                if count > 0:
                    self._sub_selected = (self._sub_selected - 1) % count
                    self._populate_rows()
            else:
                if self._filtered:
                    self._selected_idx = (self._selected_idx - 1) % len(self._filtered)
                    self._populate_rows()
                    self._ensure_visible()

        def _on_down(self, event=None) -> None:
            if self._submenu:
                count = len(self._sub_items)
                if count > 0:
                    self._sub_selected = (self._sub_selected + 1) % count
                    self._populate_rows()
            else:
                if self._filtered:
                    self._selected_idx = (self._selected_idx + 1) % len(self._filtered)
                    self._populate_rows()
                    self._ensure_visible()

        def _on_number_key(self, event) -> None:
            if self._submenu:
                return
            # Only act if search entry is not focused with text
            idx = int(event.char) - 1
            if 0 <= idx < len(self._filtered):
                self._select_command(idx)

        def _on_mousewheel(self, event) -> None:
            self._canvas.yview_scroll(-1 * (event.delta // 120), "units")

        def _on_focus_out(self, event) -> None:
            # Only close if focus left the root entirely
            try:
                if not self._root.focus_get():
                    self._root.after(100, self._check_focus)
            except Exception:
                pass

        def _check_focus(self) -> None:
            try:
                if not self._root.focus_get():
                    self._result = None
                    self._root.destroy()
            except Exception:
                pass

        def _ensure_visible(self) -> None:
            """Scroll to keep the selected row visible."""
            if not self._row_widgets or self._selected_idx >= len(self._row_widgets):
                return
            widget = self._row_widgets[self._selected_idx]
            self._canvas.update_idletasks()
            y = widget.winfo_y()
            h = widget.winfo_height()
            canvas_h = self._canvas.winfo_height()
            visible_top = self._canvas.canvasy(0)
            visible_bot = visible_top + canvas_h
            if y < visible_top:
                self._canvas.yview_moveto(y / self._inner_frame.winfo_height())
            elif y + h > visible_bot:
                self._canvas.yview_moveto((y + h - canvas_h) / self._inner_frame.winfo_height())

        # ── Mouse ──
        def _on_row_hover(self, row, idx) -> None:
            if idx != self._selected_idx:
                self._set_row_bg(row, self.BG_HOVER)

        def _on_row_leave(self, row, idx) -> None:
            if idx != self._selected_idx:
                self._set_row_bg(row, self.BG_ROW)

        def _on_row_click(self, idx) -> None:
            self._select_command(idx)

        # ── Sub-menu mouse ──
        def _on_sub_hover(self, row, idx) -> None:
            if idx != self._sub_selected:
                self._set_row_bg(row, self.BG_HOVER)

        def _on_sub_leave(self, row, idx) -> None:
            if idx != self._sub_selected:
                self._set_row_bg(row, self.BG_ROW)

        def _on_sub_click(self, idx) -> None:
            if self._submenu == "tone":
                style = self._sub_items[idx]
                cmd_config = self._commands.get("tone", {})
                # Store result with style prepended to payload
                self._result = ("tone", cmd_config, f"{style}: {self._text}")
                self._root.destroy()
            elif self._submenu == "trans":
                _, code, _ = self._sub_items[idx]
                cmd_config = self._commands.get("trans", {})
                self._result = ("trans", cmd_config, f"{code}: {self._text}")
                self._root.destroy()

        def _on_custom_lang(self, event=None) -> None:
            lang = self._custom_entry.get().strip()
            if lang:
                cmd_config = self._commands.get("trans", {})
                self._result = ("trans", cmd_config, f"{lang.upper()}: {self._text}")
                self._root.destroy()

        def _back_to_main(self) -> None:
            self._submenu = None
            self._sub_items = []
            self._sub_selected = 0
            self._custom_entry = None
            self._populate_rows()
            self._search_entry.focus_set()

        # ── Selection ──
        def _select_command(self, idx: int) -> None:
            # Resolve name/cmd from suggestions or filtered list
            if self._suggestions and not self._is_searching:
                all_items = [(n, c) for n, c, _s in self._suggestions]
                if idx >= len(all_items):
                    return
                name, cmd = all_items[idx]
            else:
                if idx >= len(self._filtered):
                    return
                name, cmd = self._filtered[idx]

            # Tone submenu
            if name == "tone":
                self._submenu = "tone"
                self._populate_rows()
                return

            # Trans submenu
            if name == "trans":
                self._submenu = "trans"
                self._populate_rows()
                return

            self._result = (name, cmd, self._text)
            self._root.destroy()

        def run(self) -> tuple | None:
            """Show the popup and block until a choice is made. Returns (cmd_name, cmd_config, payload) or None."""
            try:
                self._root.wait_window(self._root)
            except Exception:
                return None
            return self._result


def _handle_popup(text: str) -> None:
    """Show the command picker popup and dispatch the chosen command."""
    global _popup_trigger

    if not _TKINTER_AVAILABLE:
        TUI.warn("tkinter not available — cannot show popup")
        _popup_trigger = "prefix"
        route(text)
        return

    commands = CONFIG.get("commands", {})

    # Compute smart suggestions
    suggestions = None
    if _current_app_context and _current_text_analysis:
        pattern_scores = _pattern_learner.get_scores(
            _current_app_context.context_type
        ) if _pattern_learner else {}
        suggestions = get_smart_suggestions(
            _current_app_context, _current_text_analysis, commands,
            pattern_scores=pattern_scores
        )

    picker = CommandPicker(text, commands, suggestions=suggestions,
                           text_analysis=_current_text_analysis,
                           app_context=_current_app_context)
    result = picker.run()

    if result is None:
        TUI.micro_log(f"Command picker cancelled")
        return

    cmd_name, cmd_config, payload = result
    payload = strip_matching_prefix(payload, cmd_config.get("prefixes", []))

    _popup_trigger = "popup"
    TUI.status("\U0001f3af", f"Popup \u2192 {cmd_name}", TUI.GREEN)

    # Wait for compositor to restore focus to the original app after
    # the XWayland tkinter popup closes (GNOME Wayland needs ~300-500ms).
    time.sleep(0.6)
    _window_manager.restore_focus(_last_focus_target)

    global _dispatch_busy
    _dispatch_busy = True
    try:
        dispatch(cmd_name, payload, text, cmd_config)
    except Exception as exc:
        TUI.error(f"Popup dispatch error: {exc}")
    finally:
        _dispatch_busy = False


# ============================================================
# Subprocess — Run as Original User
# ============================================================

def _run_as_user(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return _system_integration.run_process(cmd, **kwargs)


# ============================================================
# Clipboard Helpers
# ============================================================

def clipboard_copy(text: str) -> None:
    _clipboard_manager.copy(text)


def clipboard_paste() -> str:
    return _clipboard_manager.paste()


def _get_primary_selection() -> str:
    return _clipboard_manager.get_primary_selection()


def _capture_selection_via_copy(timeout: float = 1.2) -> tuple[str, str]:
    """Capture the current selection via Ctrl+C with clipboard polling.

    Returns (captured_text, previous_clipboard).
    """
    config = ClipboardCaptureConfig(
        timeout=1.8 if IS_WINDOWS else timeout,
        retries=4 if IS_WINDOWS else 2,
        initial_delay=0.10 if IS_WINDOWS else CLIPBOARD_DELAY,
        retry_delay=0.12 if IS_WINDOWS else 0.05,
        poll_interval=0.03 if IS_WINDOWS else 0.05,
        settle_delay=0.08 if IS_WINDOWS else 0.03,
        paste_settle_delay=0.12 if IS_WINDOWS else 0.08,
        arm_clipboard=IS_WINDOWS,
        arm_settle_delay=0.04 if IS_WINDOWS else 0.0,
    )
    captured, snapshot = capture_selection_via_clipboard(
        _clipboard_manager,
        _hotkey_manager,
        windows=_window_manager if IS_WINDOWS else None,
        focus_target=_last_focus_target,
        config=config,
        logger=lambda message: TUI.micro_log(f"Capture: {message}"),
    )
    return captured, snapshot.text


def _send_paste_keys() -> None:
    _window_manager.restore_focus(_last_focus_target)
    _hotkey_manager.send_paste()


# ============================================================
# Result Popup — Display-only command output
# ============================================================

_result_queue: queue.Queue = queue.Queue()  # Worker thread → main thread for result popup

# Commands that show output in a popup instead of replacing text
_DISPLAY_ONLY_COMMANDS = frozenset([
    "explain", "review", "eli5", "roast", "haiku",
    "count", "define", "wiki",
])


def _queue_result_popup(title: str, text: str, *, critical: bool = False, special_ui: bool = False) -> None:
    notifications.log_info("Queued result view: %s", title)
    _result_queue.put((title, text, critical, special_ui))


if _TKINTER_AVAILABLE:

    class ResultPopup:
        """Popup window to display command output (for commands that don't edit text).
        Has a scrollable text area, copy button, and close button."""
        BG = "#1a0a2e"
        FG = "#e0e0e0"
        FG_DIM = "#888888"
        BORDER_COLOR = "#d45cff"
        SEARCH_BG = "#0e0620"
        BTN_BG = "#3a2a6e"
        BTN_COPY_BG = "#00d4aa"
        BTN_COPY_FG = "#000000"

        def __init__(self, title: str, result_text: str):
            self._title = title
            self._text = result_text

            _get_tk_root()
            self._root = tk.Toplevel()
            self._root.withdraw()
            self._root.overrideredirect(True)
            self._root.attributes("-topmost", True)
            self._root.configure(bg=self.BG, highlightbackground=self.BORDER_COLOR,
                                 highlightthickness=1)

            try:
                self._font = tkfont.Font(family="DejaVu Sans Mono", size=10)
                self._font_bold = tkfont.Font(family="DejaVu Sans Mono", size=10, weight="bold")
                self._font_small = tkfont.Font(family="DejaVu Sans Mono", size=9)
            except Exception:
                self._font = tkfont.Font(family="Courier", size=10)
                self._font_bold = tkfont.Font(family="Courier", size=10, weight="bold")
                self._font_small = tkfont.Font(family="Courier", size=9)

            self._build_ui()

            # Position at center of screen
            self._root.update_idletasks()
            w = 500
            h = 350
            sw = self._root.winfo_screenwidth()
            sh = self._root.winfo_screenheight()
            x = (sw - w) // 2
            y = (sh - h) // 2
            self._root.geometry(f"{w}x{h}+{x}+{y}")
            self._root.deiconify()
            self._root.focus_force()

        def _build_ui(self) -> None:
            # Header
            header_frame = tk.Frame(self._root, bg=self.BG)
            header_frame.pack(fill="x")

            tk.Label(header_frame, text=f"\U0001f4ac {self._title}",
                     bg=self.BG, fg=self.BORDER_COLOR,
                     font=self._font_bold, anchor="w", padx=8, pady=6).pack(side="left")

            # Close button
            close_btn = tk.Label(header_frame, text="\u2715", bg=self.BG, fg=self.FG_DIM,
                                font=self._font_bold, cursor="hand2", padx=8, pady=6)
            close_btn.pack(side="right")
            close_btn.bind("<Button-1>", lambda e: self._close())
            close_btn.bind("<Enter>", lambda e: close_btn.configure(fg="#ff4444"))
            close_btn.bind("<Leave>", lambda e: close_btn.configure(fg=self.FG_DIM))

            tk.Frame(self._root, bg=self.BORDER_COLOR, height=1).pack(fill="x")

            # Scrollable text area
            text_frame = tk.Frame(self._root, bg=self.SEARCH_BG)
            text_frame.pack(fill="both", expand=True, padx=6, pady=6)

            scrollbar = tk.Scrollbar(text_frame)
            scrollbar.pack(side="right", fill="y")

            self._text_widget = tk.Text(
                text_frame, bg=self.SEARCH_BG, fg=self.FG,
                font=self._font_small, wrap="word",
                relief="flat", bd=0,
                yscrollcommand=scrollbar.set,
                padx=8, pady=6,
            )
            self._text_widget.pack(fill="both", expand=True)
            self._text_widget.insert("1.0", self._text)
            self._text_widget.config(state="disabled")
            scrollbar.config(command=self._text_widget.yview)

            tk.Frame(self._root, bg=self.BORDER_COLOR, height=1).pack(fill="x")

            # Bottom bar with copy + close buttons
            btn_frame = tk.Frame(self._root, bg=self.BG)
            btn_frame.pack(fill="x", padx=8, pady=6)

            # Copy button
            copy_btn = tk.Label(btn_frame, text="  \U0001f4cb Copy  ", bg=self.BTN_COPY_BG,
                                fg=self.BTN_COPY_FG, font=self._font_bold,
                                cursor="hand2", padx=6, pady=3)
            copy_btn.pack(side="left", padx=(0, 4))
            copy_btn.bind("<Button-1>", lambda e: self._on_copy(copy_btn))
            copy_btn.bind("<Enter>", lambda e: copy_btn.configure(bg="#00eebb"))
            copy_btn.bind("<Leave>", lambda e: copy_btn.configure(bg=self.BTN_COPY_BG))

            # Close button
            close_btn2 = tk.Label(btn_frame, text="  Close (Esc)  ", bg=self.BTN_BG,
                                  fg=self.FG, font=self._font_bold,
                                  cursor="hand2", padx=6, pady=3)
            close_btn2.pack(side="right")
            close_btn2.bind("<Button-1>", lambda e: self._close())
            close_btn2.bind("<Enter>", lambda e: close_btn2.configure(bg="#4a3a7e"))
            close_btn2.bind("<Leave>", lambda e: close_btn2.configure(bg=self.BTN_BG))

            # Key bindings
            self._root.bind("<Escape>", lambda e: self._close())
            self._root.bind("<Control-c>", lambda e: self._on_copy(copy_btn))

        def _on_copy(self, btn) -> None:
            """Copy result text to clipboard."""
            clipboard_copy(self._text)
            btn.configure(text="  \u2713 Copied!  ")
            self._root.after(1500, lambda: btn.configure(text="  \U0001f4cb Copy  "))

        def _close(self) -> None:
            try:
                self._root.destroy()
            except Exception:
                pass

        def run(self) -> None:
            """Show popup and block until user closes it."""
            try:
                self._root.wait_window(self._root)
            except Exception:
                pass


# ============================================================
# Auto-Replace
# ============================================================

def _commit_pending_undo() -> None:
    global _pending_undo_entry
    if _pending_undo_entry is None:
        return
    with _undo_lock:
        _undo_stack.append(_pending_undo_entry)
        if len(_undo_stack) > 20:
            _undo_stack.pop(0)
        count = len(_undo_stack)
    _pending_undo_entry = None
    TUI.micro_log(f"Undo stack: {TUI.YELLOW}×{count}{TUI.RESET} available")


def _replace_selection(new_text: str) -> None:
    global _last_command_output
    if _chain_suppress_paste:
        _last_command_output = new_text
        _record_command_result(new_text, replaced=False)
        # Intermediate chain step — store result but don't paste
        TUI.success("Chain step complete (output passed to next step)")
        notifications.log_info("Command result chained to next step: chars=%s", len(new_text))
        return
    paste_with_clipboard_restore(
        _clipboard_manager,
        _hotkey_manager,
        _window_manager,
        _last_focus_target,
        new_text,
        ClipboardCaptureConfig(
            settle_delay=0.08 if IS_WINDOWS else 0.05,
            paste_settle_delay=0.14 if IS_WINDOWS else 0.1,
        ),
    )
    _last_command_output = new_text
    _record_command_result(new_text, replaced=True)
    _commit_pending_undo()
    TUI.success("Text replaced in-place")
    notifications.log_info("Result applied in-place: chars=%s", len(new_text))
    truncated = new_text[:60] + ("..." if len(new_text) > 60 else "")
    notify(APP_NAME, f"Applied: \"{truncated}\"")


# ============================================================
# Notification Helper
# ============================================================

def _should_notify(is_error: bool = False) -> bool:
    """Check if a notification should be sent based on _current_notify_level."""
    level = _current_notify_level
    if level == "never":
        return False
    if level == "errors_only" and not is_error:
        return False
    return True


def _sanitize_for_notify(s: str) -> str:
    """Strip markup characters and truncate for safe use in notify-send."""
    s = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    s = s.replace("\x00", "")  # strip null bytes
    return s[:300]


def notify(
    title: str,
    message: str,
    is_error: bool = False,
    *,
    critical: bool = False,
    image_saved: bool = False,
    dedupe_key: str | None = None,
) -> None:
    if not _should_notify(is_error=is_error):
        return
    try:
        if is_error:
            notifications.notify_error(title, message, critical=critical, dedupe_key=dedupe_key)
        else:
            notifications.notify_info(
                title,
                message,
                critical=critical,
                success=not image_saved and not critical,
                image_saved=image_saved,
                dedupe_key=dedupe_key,
            )
    except subprocess.TimeoutExpired:
        notifications.log_warning("notify-send timed out")
        TUI.warn("notify-send timed out")
    except FileNotFoundError:
        notifications.log_warning("notify-send not found")
        TUI.warn("notify-send not found — install libnotify-bin")
    except Exception as exc:
        notifications.log_error("Notification failed: %s", exc, exc_info=exc)
        TUI.error("Notification failed")


# ============================================================
# Undo System
# ============================================================

def _push_undo(original: str, replacement: str) -> None:
    global _pending_undo_entry
    _pending_undo_entry = {"original": original, "replacement": replacement}


def _do_undo() -> None:
    try:
        time.sleep(0.2)
        # Release modifier keys from the undo hotkey combo
        _hotkey_manager.release_modifiers()

        with _undo_lock:
            if not _undo_stack:
                TUI.warn("Nothing to undo")
                notify(APP_NAME, "Nothing to undo.")
                return
            entry = _undo_stack.pop()

        TUI.separator()
        TUI.action("↩", "UNDO", "Restoring previous clipboard")
        paste_with_clipboard_restore(
            _clipboard_manager,
            _hotkey_manager,
            _window_manager,
            _last_focus_target,
            entry["original"],
            ClipboardCaptureConfig(
                settle_delay=0.08 if IS_WINDOWS else 0.05,
                paste_settle_delay=0.14 if IS_WINDOWS else 0.1,
            ),
        )

        truncated = entry["original"][:50] + ("..." if len(entry["original"]) > 50 else "")
        TUI.success(f"Undone — restored: \"{truncated}\"")
        with _undo_lock:
            remaining = len(_undo_stack)
        if remaining == 0:
            TUI.micro_log(f"Undo applied — stack {TUI.DIM}empty{TUI.RESET}")
        else:
            TUI.micro_log(f"Undo applied — stack {TUI.YELLOW}×{remaining}{TUI.RESET} remaining")
        notify("Undo", "Undone · restored previous text")

    except Exception as exc:
        TUI.error(f"Undo error: {exc}")
        TUI.micro_log(f"{TUI.RED}Undo error: {exc}{TUI.RESET}")


def on_undo_triggered() -> None:
    threading.Thread(target=_do_undo, daemon=True).start()


# ============================================================
# Silent Mode Toggle
# ============================================================

def _toggle_silent_mode() -> None:
    """Toggle between silent and minimal UI modes at runtime."""
    next_mode = "minimal" if notifications.mode == "silent" else "silent"
    notifications.set_runtime_mode(next_mode)
    _sync_ui_mode_state()
    if notifications.mode == "silent":
        TUI.micro_log(f"{TUI.DIM}Silent mode ON — notifications suppressed{TUI.RESET}")
    else:
        TUI.micro_log(f"{TUI.GREEN}Minimal mode ON — important alerts only{TUI.RESET}")
    _update_tray_color("grey" if _silent_mode else ("green" if LLM_STATE == LLM_STATE_READY else "yellow"))


def on_silent_triggered() -> None:
    threading.Thread(target=_toggle_silent_mode, daemon=True).start()


# ============================================================
# Built-in Handlers
# ============================================================

def handle_translate(text: str, full_text: str, cmd_config: dict) -> None:
    """Translate text using strict transform-only semantics."""
    if not _ensure_llm_backend("translate"):
        raise RuntimeError("LLM setup was cancelled")

    cmd_model = cmd_config.get("model", "")
    try:
        result = execute_transform_command(
            "translate",
            text,
            lambda prompt: _llm_call(prompt, model=cmd_model),
            extra_args=_transform_extra_args("translate", cmd_config),
        ).output
    except TransformExecutionError as exc:
        raise RuntimeError(f"TR failed: {exc}") from exc

    _push_undo(full_text, result)
    _replace_selection(result)

    TUI.action("📝", "TRANSLATE", f"\"{text[:40]}\" → \"{result[:60]}\"")
    notify("Translate", f"Done: \"{result[:80]}\"")


_CMD_ALLOWED_COMMANDS: list[str] = CONFIG.get("command_security", {}).get(
    "allowed_commands",
    ["ls", "cat", "grep", "find", "git", "echo", "date", "python", "node",
     "curl", "wc", "head", "tail", "sort", "uniq", "diff", "file", "stat",
     "whoami", "hostname", "uname", "env", "printenv", "which", "type"],
)


def handle_command(text: str, full_text: str, cmd_config: dict) -> None:
    """Run an allowlisted system command and report the real output."""
    command = text.strip()

    try:
        prepared = prepare_command_for_execution(command, _CMD_ALLOWED_COMMANDS, is_windows=IS_WINDOWS)
    except PermissionError as exc:
        TUI.error(f"CMD blocked: {exc}")
        notify("Command Blocked", str(exc), is_error=True)
        raise RuntimeError(str(exc)) from exc
    except ValueError as exc:
        TUI.error(f"CMD syntax error: {exc}")
        notify("Command Error", str(exc), is_error=True)
        raise RuntimeError(str(exc)) from exc

    try:
        result = _run_as_user(prepared.argv, capture_output=True, text=True, timeout=30)
        output = format_command_result(result.stdout, result.stderr, result.returncode)
        _record_command_result(output, replaced=False)

        TUI.action("⚡", "COMMAND", f"`{prepared.display_command}`")
        if result.returncode == 0:
            TUI.success("Exit code 0")
        else:
            TUI.warn(f"Exit code {result.returncode}")

        preview_lines = output.splitlines()[:5]
        for line in preview_lines:
            color = TUI.DIM if result.returncode == 0 else TUI.RED
            TUI._print(f"    {color}{line}{TUI.RESET}")

        notify("Command", f"Exit {result.returncode}: {output[:100]}", is_error=result.returncode != 0)
    except subprocess.TimeoutExpired as exc:
        message = f"Command timed out after 30 seconds: {prepared.display_command}"
        TUI.error(message)
        notify("Command Error", message, is_error=True)
        raise RuntimeError(message) from exc
    except Exception as exc:
        message = f"Command failed: {exc}"
        TUI.error(message)
        notify("Command Error", message[:120], is_error=True)
        raise RuntimeError(message) from exc


def handle_test(text: str, full_text: str, cmd_config: dict) -> None:
    """Pipeline test — verifies capture → process → replace."""
    content = text.strip()
    result = f"[TEST OK] \"{content}\" | session={_SESSION_TYPE} | wayland={_IS_WAYLAND} | llm={LLM_MODE}"

    _push_undo(full_text, result)
    _replace_selection(result)

    TUI.action("🧪", "TEST", f"Input: \"{content}\"")
    TUI.success(f"Output: \"{result}\"")
    notify("Test", f"Pipeline OK: \"{content[:60]}\"")


def _transform_extra_args(cmd_key: str, cmd_config: dict) -> dict[str, str]:
    extra_args: dict[str, str] = {}
    if _current_text_analysis:
        extra_args["source_language"] = _current_text_analysis.language
    if _current_app_context:
        extra_args["app_context"] = _current_app_context.context_type
    if cmd_key == "rewrite":
        extra_args["strength"] = str(cmd_config.get("strength", "strong"))
    return extra_args


def handle_llm_command(text: str, full_text: str, cmd_config: dict,
                       cmd_key: str = "") -> None:
    """Generic handler for LLM-backed commands defined in config.yaml.

    Injects context variables: {context}, {code_language}, {app_context}
    """
    cmd_name = cmd_config.get("description", "LLM")
    is_display_only = cmd_config.get("display_only", False) or cmd_key in _DISPLAY_ONLY_COMMANDS

    if not _ensure_llm_backend(cmd_name):
        raise RuntimeError(f"LLM setup was cancelled for '{cmd_name}'")

    if cmd_key in COMMAND_SPECS:
        cmd_model = cmd_config.get("model", "")
        TUI.status("\U0001f916", f"Processing with LLM...", TUI.CYAN)
        notify(APP_NAME, "Processing...")
        try:
            result = execute_transform_command(
                cmd_key,
                text,
                lambda prompt: _llm_call(prompt, model=cmd_model),
                extra_args=_transform_extra_args(cmd_key, cmd_config),
            ).output
        except TransformExecutionError as exc:
            raise RuntimeError(f"{cmd_key} failed: {exc}") from exc

        if is_display_only:
            _record_command_result(result, replaced=False)
            _queue_result_popup(cmd_name, result)
        else:
            _push_undo(full_text, result)
            _replace_selection(result)

        truncated = result[:80] + ("..." if len(result) > 80 else "")
        provider_tag = f" [{_last_llm_provider_used}]" if _last_llm_provider_used else ""
        TUI.action("\U0001f916", cmd_name.upper(), f"\"{truncated}\"{provider_tag}")
        notify(cmd_name, f"Done: \"{truncated}\"")
        return

    prompt_template = cmd_config.get("llm_prompt", "Process this text: {text}")

    # Build context variables for smart prompt injection
    source_text = text.strip()
    fmt_vars = {"text": source_text}
    if _current_text_analysis:
        fmt_vars["code_language"] = _current_text_analysis.code_language or "unknown"
        fmt_vars["looks_like"] = _current_text_analysis.looks_like
        fmt_vars["language"] = _current_text_analysis.language
        fmt_vars["is_code"] = str(_current_text_analysis.is_code)
        # Build a context hint string
        ctx_parts = []
        if _current_text_analysis.is_code:
            ctx_parts.append(f"code ({_current_text_analysis.code_language or 'unknown language'})")
        if not _current_text_analysis.is_formal:
            ctx_parts.append("informal tone")
        ctx_parts.append(f"looks like: {_current_text_analysis.looks_like}")
        fmt_vars["context"] = ", ".join(ctx_parts) if ctx_parts else "general text"
    else:
        fmt_vars["context"] = "general text"
        fmt_vars["code_language"] = "unknown"
        fmt_vars["looks_like"] = "prose"
        fmt_vars["language"] = "en"
        fmt_vars["is_code"] = "False"

    if _current_app_context:
        fmt_vars["app_context"] = _current_app_context.context_type
    else:
        fmt_vars["app_context"] = "unknown"

    # Safely format the prompt — ignore missing keys
    try:
        prompt = prompt_template.format(**fmt_vars)
    except KeyError:
        # Fallback: only inject {text} if other vars are missing from template
        prompt = prompt_template.format(text=source_text)

    if cmd_key == "rewrite":
        prompt = build_rewrite_prompt(
            source_text,
            strength=str(cmd_config.get("strength", "strong")),
            source_language=fmt_vars.get("language", ""),
            app_context=fmt_vars.get("app_context", ""),
        )

    cmd_model = cmd_config.get("model", "")

    TUI.status("\U0001f916", f"Processing with LLM...", TUI.CYAN)
    notify(APP_NAME, "Processing...")

    result = _llm_call(prompt, model=cmd_model)
    if cmd_key == "rewrite":
        try:
            result = clean_rewrite_output(result, source_text)
        except ValueError:
            correction_prompt = (
                build_rewrite_prompt(
                    source_text,
                    strength=str(cmd_config.get("strength", "strong")),
                    source_language=fmt_vars.get("language", ""),
                    app_context=fmt_vars.get("app_context", ""),
                )
                + "\n\nYour previous answer was invalid because it changed the language, acted like a chat reply, "
                  "included extra framing, or was too weak. Return only the corrected rewritten text and make the rewrite noticeably stronger."
            )
            result = clean_rewrite_output(_llm_call(correction_prompt, model=cmd_model), source_text)

    if is_display_only:
        # Display-only: show in a popup, don't replace text
        _record_command_result(result, replaced=False)
        _queue_result_popup(cmd_name, result)
    else:
        _push_undo(full_text, result)
        _replace_selection(result)

    truncated = result[:80] + ("..." if len(result) > 80 else "")
    provider_tag = f" [{_last_llm_provider_used}]" if _last_llm_provider_used else ""
    TUI.action("\U0001f916", cmd_name.upper(), f"\"{truncated}\"{provider_tag}")
    notify(cmd_name, f"Done: \"{truncated}\"")


def handle_fmt(text: str, full_text: str, cmd_config: dict) -> None:
    """Auto-format JSON, XML, or YAML text with indentation.

    Supports modes:
    - FMT: / FORMAT: — prettify (default)
    - MIN: / MINIFY: — minify (compress to one line)
    - SORT: — prettify with sorted keys (JSON only)
    """
    content = text.strip()

    # Detect mode from prefix (set by router, may be embedded in payload)
    minify = False
    sort_keys = False
    content_lower = content.lower()
    if content_lower.startswith("min:") or content_lower.startswith("minify:"):
        minify = True
        content = _re.sub(r'^(?:min|minify):\s*', '', content, flags=_re.IGNORECASE).strip()
    elif content_lower.startswith("sort:"):
        sort_keys = True
        content = _re.sub(r'^sort:\s*', '', content, flags=_re.IGNORECASE).strip()

    # Try JSON first
    try:
        parsed = json.loads(content)
        if minify:
            result = json.dumps(parsed, separators=(',', ':'), ensure_ascii=False)
            label = "Minified"
        else:
            result = json.dumps(parsed, indent=2, ensure_ascii=False, sort_keys=sort_keys)
            label = "Formatted" + (" (sorted)" if sort_keys else "")

        _push_undo(full_text, result)
        _replace_selection(result)

        TUI.action("🔧", "FMT", f"{label} JSON ({len(content)} → {len(result)} chars)")
        notify("Format", f"JSON {label.lower()} successfully")
        return
    except json.JSONDecodeError as exc:
        json_error = exc  # Save for fallback error reporting

    # Try YAML
    try:
        parsed_yaml = yaml.safe_load(content)
        if isinstance(parsed_yaml, (dict, list)):
            if minify:
                # YAML minify = dump as JSON compact
                result = json.dumps(parsed_yaml, separators=(',', ':'), ensure_ascii=False)
                label = "YAML → minified JSON"
            else:
                result = yaml.dump(parsed_yaml, default_flow_style=False,
                                   allow_unicode=True, sort_keys=sort_keys).strip()
                label = "Formatted YAML" + (" (sorted)" if sort_keys else "")

            _push_undo(full_text, result)
            _replace_selection(result)

            TUI.action("🔧", "FMT", f"{label} ({len(content)} → {len(result)} chars)")
            notify("Format", f"{label} successfully")
            return
    except Exception:
        pass

    # Try XML
    try:
        import xml.dom.minidom
        dom = xml.dom.minidom.parseString(content)
        if minify:
            result = dom.toxml()
            # Remove XML declaration for minified output
            if not content.strip().startswith("<?xml"):
                result = _re.sub(r'^<\?xml[^?]*\?>\s*', '', result)
            label = "Minified"
        else:
            result = dom.toprettyxml(indent="  ")
            # Remove the XML declaration if it wasn't in the original
            if not content.strip().startswith("<?xml"):
                result = "\n".join(result.split("\n")[1:])
            result = result.strip()
            label = "Formatted"

        _push_undo(full_text, result)
        _replace_selection(result)

        TUI.action("🔧", "FMT", f"{label} XML ({len(content)} → {len(result)} chars)")
        notify("Format", f"XML {label.lower()} successfully")
        return
    except Exception:
        pass

    # Report error with position hint from JSON parser
    err_msg = f"Could not parse as JSON, YAML, or XML"
    if json_error:
        err_msg += f" (JSON error at line {json_error.lineno}, col {json_error.colno}: {json_error.msg})"
    TUI.error(f"FMT: {err_msg}")
    notify("Format Error", err_msg[:200])


def handle_count(text: str, full_text: str, cmd_config: dict) -> None:
    """Word/char/line stats — notification only, no clipboard replacement."""
    content = text.strip()
    stats_data = text_stats(content)
    reading_time = format_reading_time(stats_data["reading_seconds"])
    stats = (
        f"Words: {stats_data['words']} | Chars: {stats_data['chars']} | "
        f"Lines: {stats_data['lines']} | Reading time: {reading_time}"
    )
    _record_command_result(stats, replaced=False)
    TUI.action("📊", "COUNT", stats)
    _queue_result_popup("Text Stats", stats)


def handle_mock(text: str, full_text: str, cmd_config: dict) -> None:
    """Spongebob alternating caps."""
    result = "".join(
        ch.upper() if i % 2 == 0 else ch.lower()
        for i, ch in enumerate(text.strip())
    )

    _push_undo(full_text, result)
    _replace_selection(result)

    TUI.action("🧽", "MOCK", f"\"{text.strip()[:40]}\" → \"{result[:40]}\"")
    notify("Spongebob", f"{result[:80]}")


def handle_b64(text: str, full_text: str, cmd_config: dict) -> None:
    """Base64 encode selected text."""
    content = text.strip()
    result = base64.b64encode(content.encode()).decode()

    _push_undo(full_text, result)
    _replace_selection(result)

    TUI.action("🔐", "B64", f"Encoded {len(content)} chars → {len(result)} chars")
    notify("Base64 Encode", f"{result[:80]}")


def handle_decode(text: str, full_text: str, cmd_config: dict) -> None:
    """Base64 decode selected text."""
    content = text.strip()
    try:
        result = base64.b64decode(content, validate=True).decode("utf-8")

        _push_undo(full_text, result)
        _replace_selection(result)

        TUI.action("🔓", "DECODE", f"Decoded {len(content)} chars → {len(result)} chars")
        notify("Base64 Decode", f"{result[:80]}")
    except Exception as exc:
        TUI.error(f"DECODE: invalid base64 — {exc}")
        notify("Decode Error", f"Invalid base64 input: {exc}")
        raise RuntimeError(f"Invalid base64 input: {exc}") from exc


def handle_hash(text: str, full_text: str, cmd_config: dict) -> None:
    """SHA256 digest of selected text."""
    content = text.strip()
    digest = hashlib.sha256(content.encode()).hexdigest()

    _push_undo(full_text, digest)
    _replace_selection(digest)

    TUI.action("🔑", "HASH", f"SHA256: {digest[:32]}...")
    notify("SHA256", digest)


# ── PII patterns for REDACT ──
_PII_PATTERNS = [
    (_re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"), "[EMAIL]"),
    (_re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"), "[CARD]"),
    (_re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{2,4}\)?[-.\s]?)?\d{3,4}[-.\s]?\d{4}\b"), "[PHONE]"),
    (_re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
    # Date patterns (DD/MM/YYYY, MM-DD-YYYY, YYYY.MM.DD)
    (_re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b"), "[DATE]"),
    # SSN-like (XXX-XX-XXXX)
    (_re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    # Passport-like (2 letters + 7 digits)
    (_re.compile(r"\b[A-Z]{2}\d{7}\b"), "[PASSPORT]"),
    # Bearer tokens / API keys (long hex/base64 strings)
    (_re.compile(r"(?:Bearer\s+|api[_-]?key[=:]\s*)[A-Za-z0-9_\-./+=]{20,}"), "[API_KEY]"),
    # Generic long tokens (40+ hex chars, e.g. SHA hashes, API keys)
    (_re.compile(r"\b[a-fA-F0-9]{40,}\b"), "[TOKEN]"),
]


def handle_redact(text: str, full_text: str, cmd_config: dict) -> None:
    """PII masking — regex-based, replaces emails, phones, cards, IPs with placeholders."""
    result = text.strip()
    count = 0
    for pattern, placeholder in _PII_PATTERNS:
        matches = pattern.findall(result)
        count += len(matches)
        result = pattern.sub(placeholder, result)

    _push_undo(full_text, result)
    _replace_selection(result)

    TUI.action("🔒", "REDACT", f"Masked {count} PII item(s)")
    notify("Redact", f"Masked {count} PII item(s)")


# ── Safe math patterns for CALC ──
_CALC_NATURAL = [
    (_re.compile(r"(\d+(?:\.\d+)?)\s*%\s*of\s*(\d+(?:\.\d+)?)"), lambda m: str(float(m.group(1)) / 100 * float(m.group(2)))),
    (_re.compile(r"sqrt\((\d+(?:\.\d+)?)\)"), lambda m: str(math.sqrt(float(m.group(1))))),
    (_re.compile(r"(\d+(?:\.\d+)?)\s*\*\*\s*(\d+(?:\.\d+)?)"), lambda m: str(float(m.group(1)) ** float(m.group(2)))),
]

# Math function substitutions — pre-process before AST parse
_MATH_FUNCS = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "log": math.log10, "ln": math.log, "log2": math.log2,
    "abs": abs, "ceil": math.ceil, "floor": math.floor,
    "sqrt": math.sqrt, "exp": math.exp,
    "radians": math.radians, "degrees": math.degrees,
}


def _safe_eval_math(expr: str) -> str | None:
    """Safely evaluate a math expression using ast. Returns result string or None."""
    # First try natural language patterns
    for pattern, fn in _CALC_NATURAL:
        m = pattern.search(expr)
        if m:
            try:
                result = fn(m)
                # Format: strip trailing zeros
                f = float(result)
                return str(int(f)) if f == int(f) else str(f)
            except Exception:
                pass

    # Pre-process math functions: sin(45) → _RESULT_
    func_expr = expr
    for func_name, func_fn in _MATH_FUNCS.items():
        pattern = _re.compile(rf'{func_name}\(([^)]+)\)', _re.IGNORECASE)
        while pattern.search(func_expr):
            m = pattern.search(func_expr)
            try:
                inner_val = _safe_eval_math(m.group(1))
                if inner_val is not None:
                    func_result = func_fn(float(inner_val))
                    func_expr = func_expr[:m.start()] + str(func_result) + func_expr[m.end():]
                else:
                    break
            except (ValueError, OverflowError):
                break

    # Clean the expression: keep only math chars
    cleaned = _re.sub(r"[^0-9+\-*/().%^ e]", "", func_expr)
    cleaned = cleaned.replace("^", "**")
    if not cleaned.strip():
        return None

    try:
        tree = ast.parse(cleaned, mode='eval')
        result = _ast_eval(tree.body)
        f = float(result)
        return str(int(f)) if f == int(f) else str(round(f, 10))
    except (ValueError, TypeError, ZeroDivisionError, OverflowError):
        return None
    except Exception:
        return None


# Operator map for safe AST evaluation (no eval() used)
_AST_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}


def _ast_eval(node: ast.AST):
    """Recursively evaluate an AST math expression. No eval() — only safe operations."""
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)):
            raise ValueError(f"Only numeric constants allowed, got {type(node.value)}")
        if isinstance(node.value, int) and abs(node.value) > 10**15:
            raise ValueError("Integer constant too large")
        return node.value
    elif isinstance(node, ast.BinOp):
        left = _ast_eval(node.left)
        right = _ast_eval(node.right)
        if isinstance(node.op, ast.Pow) and isinstance(right, (int, float)) and right > 100:
            raise ValueError("Exponent too large (max 100)")
        op_fn = _AST_OPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"Unsupported operator: {type(node.op).__name__}")
        return op_fn(left, right)
    elif isinstance(node, ast.UnaryOp):
        operand = _ast_eval(node.operand)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        raise ValueError(f"Unsupported unary op: {type(node.op).__name__}")
    raise ValueError(f"Unsupported AST node: {type(node).__name__}")


def handle_calc(text: str, full_text: str, cmd_config: dict) -> None:
    """Safe math expression evaluator with trig, log, and formatting."""
    content = text.strip()
    result = _safe_eval_math(content)

    if result is None:
        TUI.error(f"CALC: could not evaluate \"{content[:60]}\"")
        notify("Calc Error", f"Could not evaluate: {content[:60]}")
        return

    # Format output as "expression = result"
    display = f"{content} = {result}"
    _push_undo(full_text, display)
    _replace_selection(display)

    TUI.action("🧮", "CALC", display[:80])
    notify("Calculator", display[:120])


def handle_date(text: str, full_text: str, cmd_config: dict) -> None:
    """Natural language date parser → ISO format."""
    content = text.strip()
    try:
        import dateparser
        parsed = dateparser.parse(content)
        if parsed is None:
            TUI.error(f"DATE: could not parse \"{content[:60]}\"")
            notify("Date Error", f"Could not parse: {content[:60]}")
            return

        result = parsed.strftime("%Y-%m-%d")
        _push_undo(full_text, result)
        _replace_selection(result)

        TUI.action("📅", "DATE", f"\"{content}\" → {result}")
        notify("Date", f"{content} → {result}")
    except ImportError:
        TUI.error("DATE: dateparser not installed — run: pip install dateparser")
        notify("Date Error", "dateparser library not installed")


def handle_escape(text: str, full_text: str, cmd_config: dict) -> None:
    """Escape special characters with explicit, predictable modes."""
    content = text.strip()
    mode = "json"

    for candidate in ("html", "regex", "json", "python", "shell", "sql", "code", "string"):
        prefix = f"{candidate}:"
        if content.lower().startswith(prefix):
            mode = candidate
            content = content[len(prefix):].lstrip()
            break

    try:
        result = escape_text(content, mode=mode)
    except ValueError as exc:
        TUI.error(f"ESCAPE: {exc}")
        notify("Escape Error", str(exc), is_error=True)
        raise RuntimeError(str(exc)) from exc

    _push_undo(full_text, result)
    _replace_selection(result)

    TUI.action("🛡", "ESCAPE", f"[{mode}] {len(content)} chars escaped")
    notify("Escape", f"Escaped as {mode}: {result[:80]}")


def handle_sanitize(text: str, full_text: str, cmd_config: dict) -> None:
    """Strip unwanted formatting: HTML tags, markdown syntax, ANSI codes."""
    content = text.strip()

    # Strip ANSI escape codes
    result = _re.sub(r"\033\[[0-9;]*m", "", content)

    # Strip HTML tags
    if _re.search(r"<[a-zA-Z/][^>]*>", result):
        result = _re.sub(r"<[^>]+>", "", result)
        # Decode HTML entities
        import html
        result = html.unescape(result)

    # Strip markdown syntax
    result = _re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", result)  # images
    result = _re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", result)   # links
    result = _re.sub(r"#{1,6}\s*", "", result)                   # headings
    result = _re.sub(r"\*\*(.+?)\*\*", r"\1", result)           # bold
    result = _re.sub(r"\*(.+?)\*", r"\1", result)               # italic
    result = _re.sub(r"`(.+?)`", r"\1", result)                 # inline code
    result = _re.sub(r"^[-*+]\s+", "", result, flags=_re.MULTILINE)  # list markers
    result = _re.sub(r"^\d+\.\s+", "", result, flags=_re.MULTILINE)  # numbered lists
    result = _re.sub(r"^>\s*", "", result, flags=_re.MULTILINE)      # blockquotes

    result = result.strip()

    _push_undo(full_text, result)
    _replace_selection(result)

    TUI.action("🧹", "SANITIZE", f"Stripped formatting ({len(content)} → {len(result)} chars)")
    notify("Sanitize", f"Stripped to plain text ({len(result)} chars)")


def handle_password(text: str, full_text: str, cmd_config: dict) -> None:
    """Generate a strong random password."""
    pw_cfg = cmd_config.get("password_config", {})
    length = pw_cfg.get("length", 20)
    charset = string.ascii_letters + string.digits + string.punctuation

    password = "".join(secrets.choice(charset) for _ in range(length))

    _push_undo(full_text, password)
    _replace_selection(password)

    preview = password[:4] + "..."
    TUI.action("🔑", "PASSWORD", f"Generated {length}-char password ({preview})")
    notify("Password", f"Generated: {preview} ({length} chars)")


def handle_repeat(text: str, full_text: str, cmd_config: dict) -> None:
    """Re-run the last command on the current selection."""
    if _last_command is None:
        TUI.error("REPEAT: no previous command to repeat")
        notify("Repeat Error", "No previous command to repeat")
        return

    cmd_name = _last_command["name"]
    last_config = _last_command["config"]
    TUI.status("🔁", f"Repeating: {cmd_name}", TUI.CYAN)
    dispatch(cmd_name, text.strip(), full_text, last_config)


def handle_clip(text: str, full_text: str, cmd_config: dict) -> None:
    """Manage internal named text snippets."""
    content = text.strip()
    lowered = content.lower()

    def _clip_error(message: str) -> None:
        TUI.error(message)
        notify("Clip Error", message, is_error=True)
        raise RuntimeError(message)

    if lowered == "list":
        clips = _clip_store.list_clips()
        if not clips:
            result = "No saved clips."
        else:
            result = "\n".join(f"- {name} ({len(value)} chars)" for name, value in clips)
        _record_command_result(result, replaced=False)
        TUI.action("📋", "CLIP:LIST", result.replace("\n", " | ")[:120])
        _queue_result_popup("Saved Clips", result)
        return

    if lowered == "clear":
        removed = _clip_store.clear()
        result = f"Cleared {removed} saved clip(s)."
        _record_command_result(result, replaced=False)
        TUI.action("🧹", "CLIP:CLEAR", result)
        notify("Clip", result)
        return

    save_match = _re.match(r"^save:([a-zA-Z0-9_-]{1,64}):(.*)$", content, flags=_re.DOTALL)
    if save_match:
        name = save_match.group(1)
        payload = save_match.group(2).lstrip()
        try:
            total = _clip_store.save_clip(name, payload)
        except Exception as exc:
            _clip_error(f"CLIP: {exc}")
        result = f"Saved clip '{name}' ({len(payload)} chars, total: {total})"
        _record_command_result(result, replaced=False)
        TUI.action("📌", "CLIP:SAVE", result)
        notify("Clip Save", result)
        return

    load_match = _re.match(r"^load:([a-zA-Z0-9_-]{1,64})$", content)
    if load_match:
        name = load_match.group(1)
        try:
            clip_text = _clip_store.get_clip(name)
        except KeyError as exc:
            _clip_error(f"CLIP: slot '{name}' not found")
        except Exception as exc:
            _clip_error(f"CLIP: {exc}")
        _push_undo(full_text, clip_text)
        _replace_selection(clip_text)
        TUI.action("📋", "CLIP:LOAD", f"Loaded '{name}' ({len(clip_text)} chars)")
        notify("Clip Load", f"Loaded '{name}'")
        return

    delete_match = _re.match(r"^delete:([a-zA-Z0-9_-]{1,64})$", content)
    if delete_match:
        name = delete_match.group(1)
        try:
            deleted = _clip_store.delete_clip(name)
        except Exception as exc:
            _clip_error(f"CLIP: {exc}")
        if not deleted:
            _clip_error(f"CLIP: slot '{name}' not found")
        result = f"Deleted clip '{name}'."
        _record_command_result(result, replaced=False)
        TUI.action("🗑", "CLIP:DELETE", result)
        notify("Clip Delete", result)
        return

    _clip_error("CLIP usage: save:<name>: <text> | load:<name> | list | delete:<name> | clear")


def handle_stack(text: str, full_text: str, cmd_config: dict) -> None:
    """Push text into the internal LIFO stack."""
    content = text.strip()
    payload = content or clipboard_paste()
    if not payload:
        raise RuntimeError("STACK: nothing to push")
    try:
        depth = _clipboard_stack.push(payload)
    except OverflowError as exc:
        TUI.warn(f"STACK: {exc}")
        notify("Stack Full", str(exc), is_error=True)
        raise RuntimeError(f"STACK: {exc}") from exc

    result = f"Pushed {len(payload)} chars · stack depth: {depth}"
    _record_command_result(result, replaced=False)
    TUI.action("📥", "STACK", result)
    notify("Stack", result)


def handle_pop(text: str, full_text: str, cmd_config: dict) -> None:
    """Pop the most recent stack item and insert it back into the app."""
    try:
        item, depth = _clipboard_stack.pop()
    except IndexError as exc:
        TUI.error("POP: clipboard stack is empty")
        notify("Pop Error", "Clipboard stack is empty", is_error=True)
        raise RuntimeError("POP: clipboard stack is empty") from exc

    _push_undo(full_text, item)
    _replace_selection(item)

    preview = item[:60] + ("..." if len(item) > 60 else "")
    TUI.action("📤", "POP", f"Restored \"{preview}\" · stack depth: {depth}")
    notify("Pop", f"Restored · stack depth: {depth}")


_TONE_STYLE_RE = _re.compile(r"^([a-zA-Z]+):\s*")

def handle_tone(text: str, full_text: str, cmd_config: dict) -> None:
    """Dynamic tone rewriting. Expects payload like 'casual: some text here'."""
    m = _TONE_STYLE_RE.match(text)
    if not m:
        TUI.error("TONE requires a style, e.g. TONE:casual: hello world")
        notify("Tone Error", "Missing style — use TONE:<style>: text")
        return

    style = m.group(1).lower()
    body = text[m.end():].strip()
    if not body:
        TUI.error("TONE: no text to rewrite")
        notify("Tone Error", "No text provided after style")
        return

    if not _ensure_llm_backend(f"TONE:{style}"):
        raise RuntimeError(f"LLM setup was cancelled for TONE:{style}")

    cmd_model = cmd_config.get("model", "")

    TUI.status("🎨", f"Rewriting in {style} tone...", TUI.CYAN)
    notify(APP_NAME, f"Rewriting in {style} tone...")

    try:
        result = execute_transform_command(
            "tone",
            f"{style}: {body}",
            lambda prompt: _llm_call(prompt, model=cmd_model),
            extra_args=_transform_extra_args("tone", cmd_config),
        ).output
    except TransformExecutionError as exc:
        raise RuntimeError(f"TONE failed: {exc}") from exc

    _push_undo(full_text, result)
    _replace_selection(result)

    truncated = result[:80] + ("..." if len(result) > 80 else "")
    provider_tag = f" [{_last_llm_provider_used}]" if _last_llm_provider_used else ""
    TUI.action("🎨", f"TONE→{style}", f"\"{truncated}\"{provider_tag}")
    notify(f"Tone ({style})", truncated)


_TRANS_LANG_RE = _re.compile(r"^([A-Za-z]{2,10}):\s*")

def handle_trans(text: str, full_text: str, cmd_config: dict) -> None:
    """Translate text to a target language via LLM. Expects payload like 'JP: hello world'."""
    m = _TRANS_LANG_RE.match(text)
    if not m:
        TUI.error("TRANS requires a language code, e.g. TRANS:JP: hello world")
        notify("Translation Error", "Missing language code — use TRANS:<LANG>: text")
        return

    lang_code = m.group(1).upper()
    body = text[m.end():].strip()
    if not body:
        TUI.error("TRANS: no text to translate")
        notify("Translation Error", "No text provided after language code")
        return

    if not _ensure_llm_backend(f"TRANS:{lang_code}"):
        raise RuntimeError(f"LLM setup was cancelled for TRANS:{lang_code}")

    cmd_model = cmd_config.get("model", "")

    TUI.status("🌐", f"Translating to {lang_code} with LLM...", TUI.CYAN)
    notify(APP_NAME, f"Translating to {lang_code}...")

    try:
        result = execute_transform_command(
            "trans",
            f"{lang_code}: {body}",
            lambda prompt: _llm_call(prompt, model=cmd_model),
            extra_args=_transform_extra_args("trans", cmd_config),
        ).output
    except TransformExecutionError as exc:
        raise RuntimeError(f"TRANS failed: {exc}") from exc

    _push_undo(full_text, result)
    _replace_selection(result)

    truncated = result[:80] + ("..." if len(result) > 80 else "")
    provider_tag = f" [{_last_llm_provider_used}]" if _last_llm_provider_used else ""
    TUI.action("🌐", f"TRANS→{lang_code}", f"\"{truncated}\"{provider_tag}")
    notify(f"Translated ({lang_code})", truncated)


# ============================================================
# IMAGE — AI Image Generation
# ============================================================

_IMAGE_DIR = Path(tempfile.gettempdir()) / "actionflow_images"
_SECRETS_PATH = Path.home() / ".actionflow_secrets.yaml"


def _clipboard_copy_image(image_path: str) -> bool:
    """Copy an image file to the clipboard so Ctrl+V pastes the image."""
    return _clipboard_manager.copy_image(image_path)


def _pollinations_generate(prompt: str, max_retries: int = 3) -> bytes | None:
    """Try to generate an image via Pollinations.ai with retries and seed rotation.

    Returns raw image bytes on success, None on failure.
    """
    encoded_prompt = urllib.parse.quote(prompt)
    for attempt in range(max_retries):
        seed = int(time.time()) + attempt * 7
        url = (f"https://image.pollinations.ai/prompt/{encoded_prompt}"
               f"?width=1024&height=1024&seed={seed}&nologo=true")
        req = urllib.request.Request(url, headers={"User-Agent": "ActionFlow/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = resp.read(10 * 1024 * 1024 + 1)
                if len(data) > 10 * 1024 * 1024:
                    continue
                content_type = resp.headers.get("Content-Type", "")
                if "image" in content_type:
                    return data
        except urllib.error.HTTPError as exc:
            TUI.warn(f"IMAGE: attempt {attempt + 1}/{max_retries} failed (HTTP {exc.code})")
            if exc.code == 429:
                time.sleep(3)  # rate limit — wait before retry
            else:
                time.sleep(1)
        except Exception:
            time.sleep(1)
    return None


def _load_image_generation_settings() -> dict:
    cfg = CONFIG.get("image_generation", {}) if isinstance(CONFIG.get("image_generation", {}), dict) else {}
    secrets_store = load_llm_secrets()
    image_secrets = secrets_store.get("image", {}) if isinstance(secrets_store.get("image", {}), dict) else {}
    api_key = (
        os.environ.get("ACTIONFLOW_IMAGE_API_KEY", "").strip()
        or os.environ.get("POLLINATIONSAI_API_KEY", "").strip()
        or str(cfg.get("api_key", "")).strip()
        or str(image_secrets.get("api_key", "")).strip()
    )
    return {
        "provider": str(cfg.get("provider", "pollinations")).strip().lower() or "pollinations",
        "base_url": str(cfg.get("base_url", "https://gen.pollinations.ai/image")).strip().rstrip("/"),
        "model": str(cfg.get("model", "flux")).strip() or "flux",
        "width": int(cfg.get("width", 1024) or 1024),
        "height": int(cfg.get("height", 1024) or 1024),
        "timeout": int(cfg.get("timeout", 60) or 60),
        "api_key": api_key,
    }


def _save_image_api_key(api_key: str) -> None:
    secrets_store = load_llm_secrets()
    secrets_store["image"] = {"api_key": api_key}
    with open(_SECRETS_PATH, "w", encoding="utf-8") as handle:
        yaml.safe_dump(secrets_store, handle, default_flow_style=False, allow_unicode=True)
    try:
        os.chmod(_SECRETS_PATH, 0o600)
    except Exception:
        pass


def _prompt_image_setup(provider: str) -> str:
    if _image_setup_handler is not None:
        try:
            return str(_image_setup_handler(provider) or "").strip()
        except Exception as exc:
            notifications.log_error("GUI image setup handler failed: %s", exc, exc_info=exc)
    print()
    TUI.box("Image Setup Required", [
        f"  {TUI.DIM}The image backend needs authorization before it can run.{TUI.RESET}",
        f"  {TUI.DIM}Provider:{TUI.RESET} {provider}",
        "",
        f"  {TUI.DIM}Enter an image API key to retry, or cancel to abort.{TUI.RESET}",
    ], TUI.YELLOW)
    if not sys.stdin.isatty():
        return ""
    try:
        return input(f"  {TUI.CYAN}{TUI.BOLD}Image API Key:{TUI.RESET} ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _generate_image_bytes(prompt: str, settings: dict, max_retries: int = 3) -> bytes:
    encoded_prompt = urllib.parse.quote(prompt)
    last_error: Exception | None = None
    for attempt in range(max_retries):
        seed = int(time.time()) + attempt * 7
        query = {
            "width": settings["width"],
            "height": settings["height"],
            "seed": seed,
            "nologo": "true",
            "model": settings["model"],
        }
        if settings.get("api_key"):
            query["key"] = settings["api_key"]
        url = f"{settings['base_url']}/{encoded_prompt}?{urllib.parse.urlencode(query)}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "ActionFlow/1.0", "Accept": "image/*"},
        )
        try:
            with urllib.request.urlopen(req, timeout=settings["timeout"]) as resp:
                data = resp.read(10 * 1024 * 1024 + 1)
                if len(data) > 10 * 1024 * 1024:
                    raise RuntimeError("image response too large")
                content_type = resp.headers.get("Content-Type", "")
                if "image" not in content_type.lower():
                    raise RuntimeError(f"unexpected content type: {content_type or 'unknown'}")
                return data
        except urllib.error.HTTPError as exc:
            last_error = exc
            TUI.warn(f"IMAGE: attempt {attempt + 1}/{max_retries} failed (HTTP {exc.code})")
            if exc.code == 401:
                raise RuntimeError("image provider requires authorization") from exc
            time.sleep(3 if exc.code == 429 else 1)
        except Exception as exc:
            last_error = exc
            time.sleep(1)
    raise RuntimeError(str(last_error or "image generation failed"))


_IMAGE_STYLES = {
    "photo": "photorealistic, high quality, 4K",
    "anime": "anime style, vibrant colors, detailed",
    "pixel": "pixel art, retro game style, 8-bit",
    "sketch": "pencil sketch, hand-drawn, artistic",
    "oil": "oil painting, classic art style, textured brushstrokes",
    "watercolor": "watercolor painting, soft colors, artistic",
    "3d": "3D rendered, cinema 4D, high quality render",
    "comic": "comic book style, bold lines, vibrant",
}

_IMAGE_STYLE_RE = _re.compile(
    r'^(' + '|'.join(_IMAGE_STYLES.keys()) + r'):\s*', _re.IGNORECASE
)


def handle_image(text: str, full_text: str, cmd_config: dict) -> None:
    """Generate an image from a text prompt using Pollinations.ai (free, no API key).

    Supports style prefixes: photo:, anime:, pixel:, sketch:, oil:, watercolor:, 3d:, comic:
    The image is copied to the clipboard and pasted into the active application.
    Also saved to /tmp/actionflow_images/ for later access.
    """
    prompt = text.strip()
    if not prompt:
        TUI.error("IMAGE: no prompt provided")
        notify("Image Error", "Please provide a description for the image")
        raise RuntimeError("IMAGE: no prompt provided")

    # Detect style prefix
    style_match = _IMAGE_STYLE_RE.match(prompt)
    if style_match:
        style_key = style_match.group(1).lower()
        style_desc = _IMAGE_STYLES.get(style_key, "")
        prompt = prompt[style_match.end():].strip()
        if style_desc:
            prompt = f"{prompt}, {style_desc}"
        TUI.status("🎨", f"Style: {style_key} | Generating: \"{prompt[:50]}\"...", TUI.CYAN)
    else:
        TUI.status("🎨", f"Generating image: \"{prompt[:50]}\"...", TUI.CYAN)
    notify(APP_NAME, "Generating image...")

    settings = _load_image_generation_settings()
    try:
        image_data = _generate_image_bytes(prompt, settings)
    except RuntimeError as exc:
        if "authorization" in str(exc).lower() and not settings.get("api_key"):
            api_key = _prompt_image_setup(settings["provider"])
            if not api_key:
                notify(
                    "Image Setup Required",
                    "Image generation needs an API key before it can continue.",
                    is_error=True,
                    critical=True,
                    dedupe_key="image_setup_required",
                )
                raise RuntimeError("Image generation cancelled during setup") from exc
            _save_image_api_key(api_key)
            settings["api_key"] = api_key
            image_data = _generate_image_bytes(prompt, settings)
        else:
            TUI.error(f"IMAGE: {exc}")
            notify("Image Error", str(exc))
            raise

    try:
        # Save to temp directory
        _IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = _re.sub(r'[^a-zA-Z0-9_-]', '_', prompt[:40])
        image_path = _IMAGE_DIR / f"{timestamp}_{safe_name}.png"
        image_path.write_bytes(image_data)

        TUI.success(f"Image saved: {image_path}")

        # Copy image to clipboard and paste
        if _clipboard_copy_image(str(image_path)):
            time.sleep(CLIPBOARD_DELAY)
            _send_paste_keys()
            _record_command_result(str(image_path), replaced=False)
            TUI.success("Image pasted into application")
            notifications.log_info("Image pasted and saved at %s", image_path)
        else:
            # Fallback: insert the file path as text
            clipboard_copy(str(image_path))
            time.sleep(CLIPBOARD_DELAY)
            _send_paste_keys()
            _record_command_result(str(image_path), replaced=False)
            TUI.warn("Could not paste image — inserted file path instead")
            notify(
                "Image Saved",
                f"Saved at: {image_path} (path inserted)",
                image_saved=True,
                dedupe_key=f"image_saved:{image_path}",
            )

        TUI.action("🎨", "IMAGE", f"\"{prompt[:50]}\" → {image_path.name}")

    except urllib.error.URLError as exc:
        TUI.error(f"IMAGE: network error — {exc}")
        notify("Image Error", f"Network error: {exc}")
        raise RuntimeError(f"IMAGE network error: {exc}") from exc
    except Exception as exc:
        TUI.error(f"IMAGE: generation failed — {exc}")
        notify("Image Error", f"Failed: {exc}")
        raise


# ============================================================
# WIKI / DEFINE — Web Lookup Commands
# ============================================================

_MAX_API_RESPONSE_BYTES = 512 * 1024  # 512 KB


def _safe_url_read(req, timeout: int = 10) -> bytes:
    """Read URL response with a size limit to prevent memory exhaustion."""
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read(_MAX_API_RESPONSE_BYTES + 1)
        if len(data) > _MAX_API_RESPONSE_BYTES:
            raise ValueError("API response too large (>512KB)")
        return data


def _fetch_json(url: str) -> dict | list:
    req = urllib.request.Request(url, headers={"User-Agent": "ActionFlow/1.0"})
    return json.loads(_safe_url_read(req).decode("utf-8"))


def _fetch_wiktionary_wikitext(language: str, word: str) -> str:
    encoded = urllib.parse.quote(word)
    payload = _fetch_json(
        f"https://{language}.wiktionary.org/w/api.php?"
        f"action=query&titles={encoded}&prop=revisions&rvprop=content&rvslots=main&formatversion=2&format=json"
    )
    if not isinstance(payload, dict):
        return ""
    query = payload.get("query", {})
    pages = query.get("pages", []) if isinstance(query, dict) else []
    if not isinstance(pages, list) or not pages:
        return ""
    page = pages[0]
    if not isinstance(page, dict) or page.get("missing"):
        return ""
    revisions = page.get("revisions", [])
    if not isinstance(revisions, list) or not revisions:
        return ""
    revision = revisions[0]
    if not isinstance(revision, dict):
        return ""
    slots = revision.get("slots", {})
    if isinstance(slots, dict):
        main = slots.get("main", {})
        if isinstance(main, dict):
            return str(main.get("content", "")).strip()
    return ""


def _lookup_label(language: str, russian: str, english: str) -> str:
    return russian if language == "ru" else english


def _format_definition_line(definition, index: int, language: str) -> str:
    parts: list[str] = []
    if definition.labels:
        parts.append(" ".join(definition.labels))
    if definition.part_of_speech:
        parts.append(definition.part_of_speech)
    prefix = f"({'; '.join(parts)}) " if parts else ""
    line = f"{index}. {prefix}{definition.definition}"
    if definition.example:
        example_label = _lookup_label(language, "Пример", "Example")
        line = f"{line}\n   {example_label}: \"{definition.example}\""
    return line


def handle_wiki(text: str, full_text: str, cmd_config: dict) -> None:
    """Wikipedia lookup with disambiguation-aware results."""
    query = text.strip()
    if not query:
        TUI.error("WIKI: no search term provided")
        notify("Wiki Error", "No search term provided", is_error=True)
        return

    try:
        query_language = detect_query_language(query)
        wiki_host = "ru.wikipedia.org" if query_language == "ru" else "en.wikipedia.org"
        encoded = urllib.parse.quote(query)
        summary_payload = _fetch_json(f"https://{wiki_host}/api/rest_v1/page/summary/{encoded}")
        search_payload = _fetch_json(
            f"https://{wiki_host}/w/api.php?"
            f"action=query&list=search&srsearch={encoded}&utf8=1&format=json&srlimit=5"
        )
        wiki_result = build_wiki_result(query, summary_payload, search_payload if isinstance(search_payload, dict) else {})

        if wiki_result.kind == "choices":
            preferred = choose_preferred_wiki_title(query, list(wiki_result.choices))
            if preferred:
                preferred_summary = _fetch_json(
                    f"https://{wiki_host}/api/rest_v1/page/summary/{urllib.parse.quote(preferred)}"
                )
                preferred_result = build_wiki_result(preferred, preferred_summary)
                if preferred_result.kind == "summary":
                    wiki_result = preferred_result

        if wiki_result.kind == "summary":
            full_result = f"{wiki_result.title}\n{'=' * len(wiki_result.title)}\n\n{wiki_result.summary}"
            _record_command_result(full_result, replaced=False)
            TUI.action("📖", "WIKI", f"{wiki_result.title}: {wiki_result.summary[:80]}...")
            _queue_result_popup(
                f"{_lookup_label(query_language, 'Википедия', 'Wikipedia')}: {wiki_result.title}",
                full_result,
                special_ui=True,
            )
            return

        if wiki_result.kind == "choices":
            choices_text = "\n".join(f"- {choice}" for choice in wiki_result.choices)
            full_result = (
                f"{query}\n{'=' * len(query)}\n\n"
                f"{_lookup_label(query_language, 'Термин неоднозначен. Попробуйте один из вариантов:', 'This term is ambiguous. Try one of these pages:')}\n"
                f"{choices_text}"
            )
            _record_command_result(full_result, replaced=False)
            TUI.action("📖", "WIKI", f"Ambiguous term: {query}")
            _queue_result_popup(
                f"{_lookup_label(query_language, 'Википедия', 'Wikipedia')}: {query}",
                full_result,
                special_ui=True,
            )
            return

        TUI.warn(f"WIKI: no article found for \"{query}\"")
        notify(
            _lookup_label(query_language, "Википедия", "Wikipedia"),
            _lookup_label(query_language, f"Статья для «{query}» не найдена", f"No article found for \"{query}\""),
        )

    except urllib.error.HTTPError as exc:
        TUI.error(f"WIKI: HTTP {exc.code}")
        notify("Wiki Error", f"HTTP error: {exc.code}", is_error=True)
    except Exception as exc:
        TUI.error(f"WIKI: {exc}")
        notify("Wiki Error", str(exc)[:100], is_error=True)


def handle_define(text: str, full_text: str, cmd_config: dict) -> None:
    """Dictionary lookup with safe definition ranking."""
    word = text.strip().split()[0] if text.strip() else ""
    if not word:
        TUI.error("DEFINE: no word provided")
        notify("Define Error", "No word provided", is_error=True)
        return

    try:
        query_language = detect_query_language(word)
        if query_language == "ru":
            wikitext = _fetch_wiktionary_wikitext("ru", word)
            selected = parse_russian_wiktionary_definitions(wikitext, limit=3)
        else:
            encoded = urllib.parse.quote(word.lower())
            url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{encoded}"
            data = _fetch_json(url)
            if not data or not isinstance(data, list):
                TUI.warn(f"DEFINE: no definition for \"{word}\"")
                notify(
                    _lookup_label(query_language, "Словарь", "Dictionary"),
                    _lookup_label(query_language, f"Определение для «{word}» не найдено", f"No definition for \"{word}\""),
                )
                return
            selected = select_safe_definitions(data, limit=3)

        if not selected:
            TUI.warn(f"DEFINE: no safe definitions for \"{word}\"")
            notify(
                _lookup_label(query_language, "Словарь", "Dictionary"),
                _lookup_label(
                    query_language,
                    f"Подходящее определение для «{word}» не найдено",
                    f"No safe definitions found for \"{word}\"",
                ),
            )
            return

        primary = selected[0]
        primary_line = f"({primary.part_of_speech}) {primary.definition}" if primary.part_of_speech else primary.definition
        detail_lines = [_format_definition_line(definition, index, query_language) for index, definition in enumerate(selected, 1)]

        full_result = f"{word}\n{'=' * len(word)}\n\n" + "\n".join(detail_lines)
        _record_command_result(full_result, replaced=False)
        TUI.action("📚", "DEFINE", f"{word}: {primary_line[:80]}")
        _queue_result_popup(f"{_lookup_label(query_language, 'Определение', 'Define')}: {word}", full_result, special_ui=True)

    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            TUI.warn(f"DEFINE: word \"{word}\" not found")
            query_language = detect_query_language(word)
            notify(
                _lookup_label(query_language, "Словарь", "Dictionary"),
                _lookup_label(query_language, f"Слово «{word}» не найдено", f"Word \"{word}\" not found"),
            )
        else:
            TUI.error(f"DEFINE: HTTP {exc.code}")
            notify("Define Error", f"HTTP error: {exc.code}", is_error=True)
    except Exception as exc:
        TUI.error(f"DEFINE: {exc}")
        notify("Define Error", str(exc)[:100], is_error=True)


# ============================================================
# Personal Commands via Examples
# ============================================================

def handle_personal_command(text: str, full_text: str, cmd_config: dict) -> None:
    """Handle user-defined personal commands using few-shot LLM prompting."""
    if not _ensure_llm_backend(cmd_config.get("description", "personal command")):
        raise RuntimeError("LLM setup was cancelled for personal command")

    examples = cmd_config.get("examples", [])
    prompt_parts = []
    if cmd_config.get("description"):
        prompt_parts.append(f"Task: {cmd_config['description']}")
    prompt_parts.append("")
    for ex in examples:
        prompt_parts.append(f"Input: {ex['input']}")
        prompt_parts.append(f"Output: {ex['output']}")
        prompt_parts.append("")
    prompt_parts.append(f"Input: {text.strip()}")
    prompt_parts.append("Output:")

    prompt = "\n".join(prompt_parts)
    model = cmd_config.get("model", "")
    result = _llm_call(prompt, model=model)

    _push_undo(full_text, result)
    _replace_selection(result)
    TUI.action("👤", "PERSONAL", f"{cmd_config.get('description', '')[:30]}: {result[:40]}")
    notify("Personal Command", f"{result[:80]}")


def _register_personal_commands() -> None:
    """Register personal commands from config into the command system."""
    CONFIG["commands"] = CommandRegistry.register_personal_commands(
        CONFIG.get("commands", {}),
        CONFIG.get("personal_commands", {}),
    )
    for cmd_key, cmd_config in CONFIG.get("commands", {}).items():
        if cmd_config.get("_personal"):
            _BUILTIN_HANDLERS[cmd_key] = handle_personal_command


# Map of built-in command names → handler functions
_BUILTIN_HANDLERS = {
    "translate": handle_translate,
    "command": handle_command,
    "test": handle_test,
    "fmt": handle_fmt,
    "count": handle_count,
    "mock": handle_mock,
    "b64": handle_b64,
    "decode": handle_decode,
    "hash": handle_hash,
    "redact": handle_redact,
    "calc": handle_calc,
    "date": handle_date,
    "escape": handle_escape,
    "sanitize": handle_sanitize,
    "password": handle_password,
    "repeat": handle_repeat,
    "clip": handle_clip,
    "stack": handle_stack,
    "pop": handle_pop,
    "tone": handle_tone,
    "trans": handle_trans,
    "wiki": handle_wiki,
    "define": handle_define,
    "image": handle_image,
}


# ============================================================
# History Log
# ============================================================

_HISTORY_PATH = Path.home() / ".actionflow_history.jsonl"

# Commands whose input/output must never be logged in plaintext
_SENSITIVE_COMMANDS = frozenset({"password", "redact", "command"})


def _log_history(command: str, input_text: str, output_text: str, duration_ms: int,
                 app_context: str = "", text_length: int = 0,
                 text_language: str = "", trigger: str = "") -> None:
    """Append a JSON line to ~/.actionflow_history.jsonl."""
    try:
        # Redact sensitive command data from logs
        if command in _SENSITIVE_COMMANDS:
            input_text = f"[{len(input_text)} chars]"
            output_text = "[REDACTED]"

        provider = _last_llm_provider_used or _llm_provider or "builtin"
        entry = json.dumps({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "command": command,
            "input": input_text[:500],
            "output": output_text[:500],
            "duration_ms": duration_ms,
            "provider": provider,
            "app_context": app_context,
            "text_length": text_length,
            "text_language": text_language,
            "trigger": trigger,
        }, ensure_ascii=False)
        with open(_HISTORY_PATH, "a", encoding="utf-8", newline="\n") as f:
            f.write(entry + "\n")
        # Restrict file permissions: owner read/write only
        os.chmod(_HISTORY_PATH, 0o600)
    except Exception as exc:
        TUI.warn(f"History log write failed: {exc}")


# ============================================================
# Router — 3-tier: Prefix → Keyword → LLM → Fallback
# ============================================================

def dispatch(cmd_name: str, payload: str, full_text: str, cmd_config: dict) -> None:
    """Dispatch to the correct handler for a matched command."""
    global _last_command, _current_notify_level, _last_command_output

    # Set per-command notification level (always | errors_only | never)
    _current_notify_level = cmd_config.get("notify", "always")
    _last_command_output = ""
    _reset_dispatch_result()

    with _usage_lock:
        _usage_counts[cmd_name] = _usage_counts.get(cmd_name, 0) + 1

    is_llm = cmd_config.get("llm_required", False) or cmd_name not in _BUILTIN_HANDLERS
    start_time = time.time()
    notifications.log_info(
        "Command execution start: name=%s trigger=%s payload_chars=%s llm=%s",
        cmd_name,
        _popup_trigger,
        len(payload),
        is_llm,
    )

    try:
        if cmd_name in _BUILTIN_HANDLERS:
            _BUILTIN_HANDLERS[cmd_name](payload, full_text, cmd_config)
        elif cmd_config.get("llm_required"):
            handle_llm_command(payload, full_text, cmd_config, cmd_key=cmd_name)
        else:
            handle_llm_command(payload, full_text, cmd_config, cmd_key=cmd_name)

        duration = time.time() - start_time
        if cmd_name != "repeat":
            _last_command = {"name": cmd_name, "config": cmd_config}
        output = _current_dispatch_output()
        TUI.activity_entry(cmd_name, payload, output, duration, is_llm=is_llm,
                           trigger=_popup_trigger)
        _log_history(cmd_name, payload, output, int(duration * 1000),
                     app_context=_current_app_context.context_type if _current_app_context else "",
                     text_length=len(payload),
                     text_language=_current_text_analysis.language if _current_text_analysis else "",
                     trigger=_popup_trigger)
        notifications.log_info(
            "Command execution complete: name=%s duration_ms=%s replaced=%s",
            cmd_name,
            int(duration * 1000),
            bool(_current_dispatch_result and _current_dispatch_result.get("replaced")),
        )

    except Exception as exc:
        duration = time.time() - start_time
        TUI.activity_entry(cmd_name, payload, str(exc), duration, is_error=True,
                           trigger=_popup_trigger)
        _log_history(cmd_name, payload, f"ERROR: {exc}", int(duration * 1000),
                     app_context=_current_app_context.context_type if _current_app_context else "",
                     text_length=len(payload),
                     text_language=_current_text_analysis.language if _current_text_analysis else "",
                     trigger=_popup_trigger)
        notifications.log_error("Command execution failed: name=%s error=%s", cmd_name, exc, exc_info=exc)
        raise


_chain_suppress_paste = False


def _resolve_prefix(text: str, commands: dict) -> tuple[str, str, dict] | None:
    """Match a prefix at the start of text. Returns (cmd_name, payload, cmd_config) or None."""
    text_upper = text.upper()
    for name, cmd in commands.items():
        for prefix in cmd.get("prefixes", []):
            if text_upper.startswith(prefix.upper()):
                return name, text[len(prefix):], cmd
    return None


def _parse_chain(text: str, commands: dict) -> list[tuple[str, dict]] | None:
    """Parse pipe-separated prefix chain like 'TR:|SUM: payload'.

    Returns list of (cmd_name, cmd_config) for each step, or None if not a chain.
    """
    # Quick check: must contain | between prefix-like tokens
    if "|" not in text:
        return None

    # Split on | but only the prefix portion (everything before the actual payload)
    # Strategy: greedily match PREFIX:| sequences from the left
    steps = []
    remaining = text
    while True:
        pipe_pos = remaining.find("|")
        if pipe_pos == -1:
            break
        candidate = remaining[:pipe_pos]
        match = _resolve_prefix(candidate, commands)
        if match:
            cmd_name, _, cmd_config = match
            steps.append((cmd_name, cmd_config))
            remaining = remaining[pipe_pos + 1:]
        else:
            break

    if len(steps) < 1:
        return None

    # The remaining text must start with a valid prefix too (the final command)
    final_match = _resolve_prefix(remaining, commands)
    if not final_match:
        return None

    steps.append((final_match[0], final_match[2]))
    # Store the actual payload (text after the last prefix)
    return steps


def _extract_chain_payload(text: str, commands: dict) -> str:
    """Extract the payload text from a chain like 'TR:|SUM: the actual text'."""
    remaining = text
    while True:
        pipe_pos = remaining.find("|")
        if pipe_pos == -1:
            break
        candidate = remaining[:pipe_pos]
        if _resolve_prefix(candidate, commands):
            remaining = remaining[pipe_pos + 1:]
        else:
            break
    # remaining is now "SUM: the actual text" — strip the last prefix
    match = _resolve_prefix(remaining, commands)
    if match:
        return match[1]  # payload after prefix
    return remaining


def _has_explicit_prefix_command(text: str, commands: dict) -> bool:
    if _parse_chain(text, commands):
        return True
    return _resolve_prefix(text, commands) is not None


def route(text: str) -> None:
    commands = CONFIG.get("commands", {})
    global _chain_suppress_paste

    # Check for pipe chain syntax first
    chain = _parse_chain(text, commands)
    if chain and len(chain) >= 2:
        payload = _extract_chain_payload(text, commands)
        step_names = " → ".join(name for name, _ in chain)
        TUI.status("⛓", f"Chain: {step_names}", TUI.CYAN)

        current_input = payload
        for i, (cmd_name, cmd_config) in enumerate(chain):
            is_last = (i == len(chain) - 1)
            step_label = f"[{i+1}/{len(chain)}] {cmd_name}"

            if not is_last:
                # Suppress clipboard paste for intermediate steps
                _chain_suppress_paste = True

            try:
                TUI.status("⛓", f"Step {step_label}...", TUI.CYAN)
                dispatch(cmd_name, current_input, text, cmd_config)

                # Get output from undo stack for next step
                if not is_last:
                    current_input = _last_command_output or current_input
            except Exception as exc:
                TUI.error(f"Chain failed at step {step_label}: {exc}")
                notify(APP_NAME, f"Chain failed at step {step_label}")
                return
            finally:
                _chain_suppress_paste = False

        return

    # Tier 1: Exact prefix match (fastest, backward-compatible)
    text_upper = text.upper()
    for name, cmd in commands.items():
        for prefix in cmd.get("prefixes", []):
            if text_upper.startswith(prefix.upper()):
                payload = text[len(prefix):]
                TUI.status("🎯", f"Prefix match: {prefix} → {name}", TUI.GREEN)
                dispatch(name, payload, text, cmd)
                return

    # Tier 2: Keyword match (check first 3 words)
    text_lower = text.strip().lower()
    words = text_lower.split()
    first_words = words[:3]
    for name, cmd in commands.items():
        for keyword in cmd.get("keywords", []):
            # Match single-word keywords against first 3 words
            # Match multi-word keywords against the full start of text
            if " " in keyword:
                if text_lower.startswith(keyword):
                    payload = text_lower[len(keyword):].strip()
                    TUI.status("🔑", f"Keyword match: \"{keyword}\" → {name}", TUI.GREEN)
                    dispatch(name, payload, text, cmd)
                    return
            elif keyword in first_words:
                # Strip the keyword from payload
                idx = text_lower.find(keyword)
                payload = text[idx + len(keyword):].strip()
                TUI.status("🔑", f"Keyword match: \"{keyword}\" → {name}", TUI.GREEN)
                dispatch(name, payload, text, cmd)
                return

    # Tier 3: LLM intent classification (live mode only)
    if LLM_STATE == LLM_STATE_READY and _llm_ready:
        TUI.status("🤖", "No prefix/keyword match — asking LLM to classify...", TUI.CYAN)
        intent = _llm_classify(text, commands)
        if intent:
            confidence = intent.get("confidence", 1.0)
            threshold = CONFIG.get("confidence_threshold", 0.7)
            if confidence < threshold:
                TUI.warn(
                    f"LLM classified as '{intent['name']}' but confidence {confidence:.2f} "
                    f"< threshold {threshold:.2f} — skipping"
                )
                notify(
                    APP_NAME,
                    f"Low confidence ({confidence:.0%}) on '{intent['name']}' — not applied. "
                    f"Use the prefix directly to force.",
                )
                return
            cmd = commands[intent["name"]]
            TUI.status("🤖", f"LLM classified: → {intent['name']} (confidence {confidence:.2f})", TUI.GREEN)
            dispatch(intent["name"], intent["payload"], text, cmd)
            return

    # Tier 4: Fallback
    truncated = text[:40] + ("..." if len(text) > 40 else "")
    TUI.warn(f"Unknown command: \"{truncated}\"")

    available = ", ".join(
        p for cmd in commands.values() for p in cmd.get("prefixes", [])
    )
    notify(APP_NAME, f"Unknown command. Prefixes: {available}")


# ============================================================
# Interceptor
# ============================================================

_RATE_LIMIT_INTERVAL = 1.0  # Minimum seconds between dispatches
_last_dispatch_time = 0.0
_rate_limit_lock = threading.Lock()


def _rate_limit_check() -> bool:
    """Return True if enough time has passed since last dispatch."""
    global _last_dispatch_time
    with _rate_limit_lock:
        now = time.time()
        if now - _last_dispatch_time < _RATE_LIMIT_INTERVAL:
            return False
        _last_dispatch_time = now
        return True


def _do_intercept() -> None:
    global _last_hotkey_callback_at, _hotkey_callback_count, _last_hotkey_error, _last_hotkey_error_at
    try:
        _hotkey_callback_count += 1
        _last_hotkey_callback_at = time.time()
        _last_hotkey_error = None
        _last_hotkey_error_at = None
        notifications.log_info("Hotkey callback fired: count=%s", _hotkey_callback_count)
        if not _rate_limit_check():
            TUI.warn("Rate limited — please wait before triggering again")
            return
        # Brief pause to let the user release hotkey keys
        time.sleep(0.15)
        # Release all modifier keys from the hotkey to prevent interference
        _hotkey_manager.release_modifiers()

        TUI.separator()
        TUI.status("⌨", "Hotkey triggered — reading selection...", TUI.CYAN)
        TUI.micro_log(f"Hotkey triggered — reading selection...")
        notifications.log_info("Selection capture started")

        global _last_focus_target
        _last_focus_target = _window_manager.capture_focus_target()

        if IS_LINUX and _IS_WAYLAND:
            text = _get_primary_selection()
            if not text.strip():
                text, _ = _capture_selection_via_copy()
        else:
            text, _ = _capture_selection_via_copy()
            if not text:
                TUI.warn("Selection capture timed out")
                notifications.log_warning("Selection capture failed: timeout")
                notify(APP_NAME, "No text captured from the current selection.")
                return

        if not text or not text.strip():
            TUI.warn("No text captured from selection")
            notifications.log_warning("Selection capture failed: empty selection")
            notify(APP_NAME, "No text selected.")
            return

        truncated = text[:60] + ("..." if len(text) > 60 else "")
        TUI.action("📋", "CAPTURED", f"\"{truncated}\"")
        notifications.log_info("Selection capture success: chars=%s", len(text))

        # Phase 8: detect app context and analyze text
        global _current_app_context, _current_text_analysis
        _current_app_context = detect_active_window()
        _current_text_analysis = analyze_text(text)
        TUI.micro_log(
            f"Context: {_current_app_context.context_type}"
            f" · {_current_text_analysis.looks_like}"
            f" · {_current_text_analysis.language}"
        )

        commands = CONFIG.get("commands", {})

        if _has_explicit_prefix_command(text, commands):
            TUI.micro_log("Explicit command prefix detected — executing directly")
            notifications.log_info("Command route selected: explicit prefix")
            global _popup_trigger
            _popup_trigger = "prefix"
            route(text)
            return

        # Open command picker only when no explicit command prefix is present
        if _TKINTER_AVAILABLE:
            if _dispatch_busy:
                notify(APP_NAME, "⏳ Command still processing — please wait")
                TUI.warn("Hotkey ignored — command still processing")
                notifications.log_warning("Hotkey ignored because dispatch is already busy")
                return
            _popup_queue.put(text)
            TUI.micro_log(f"Opening command picker...")
            notifications.log_info("Command picker requested from hotkey")
            return

        # Fallback if tkinter unavailable: route via prefix/keyword/LLM
        TUI.warn("tkinter unavailable — falling back to prefix routing")
        notifications.log_warning("Command picker unavailable; routing via fallback")
        _popup_trigger = "prefix"
        route(text)

    except Exception as exc:
        _last_hotkey_error = str(exc)
        _last_hotkey_error_at = time.time()
        TUI.error(f"Interceptor error: {exc}")
        TUI.micro_log(f"{TUI.RED}Error: {exc}{TUI.RESET}")
        notifications.log_error("Hotkey pipeline failed: %s", exc, exc_info=exc)
        notify(APP_NAME, f"Error: {exc}")


def on_hotkey_triggered() -> None:
    threading.Thread(target=_do_intercept, daemon=True).start()


# ============================================================
# Command Search
# ============================================================

def _read_console_char_nonblocking(timeout: float = 0.1) -> str | None:
    if IS_WINDOWS:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if msvcrt.kbhit():
                ch = msvcrt.getwch()
                if ch in ("\x00", "\xe0"):
                    msvcrt.getwch()
                    return None
                return ch
            time.sleep(0.01)
        return None

    if select.select([sys.stdin], [], [], timeout)[0]:
        return sys.stdin.read(1)
    return None

def _command_search() -> None:
    """Interactive fuzzy search over command names and keywords."""
    commands = CONFIG.get("commands", {})
    query = ""

    def _find_matches(q: str) -> list[tuple[str, dict]]:
        q_lower = q.lower()
        matches = []
        for name, cmd in commands.items():
            keywords = cmd.get("keywords", [])
            prefixes = cmd.get("prefixes", [])
            desc = cmd.get("description", "")
            searchable = f"{name} {' '.join(keywords)} {' '.join(prefixes)} {desc}".lower()
            if q_lower in searchable:
                matches.append((name, cmd))
        return matches

    while True:
        matches = _find_matches(query) if query else list(commands.items())
        sys.stdout.write(f"\r\033[K  {TUI.CYAN}{TUI.BOLD}/{TUI.RESET} {query}{TUI.DIM}  ({len(matches)} matches · esc to cancel){TUI.RESET}")
        sys.stdout.flush()

        ch = _read_console_char_nonblocking(0.1)
        if ch is None:
            continue
        else:
            if ch == '\x1b':  # Escape
                sys.stdout.write(f"\r\033[K")
                sys.stdout.flush()
                return
            elif ch == '\x03':  # Ctrl+C
                sys.stdout.write(f"\r\033[K")
                sys.stdout.flush()
                return
            elif ch in ('\r', '\n'):  # Enter — show results
                sys.stdout.write(f"\r\033[K\n")
                sys.stdout.flush()
                if matches:
                    lines = []
                    for name, cmd in matches:
                        pfx = ", ".join(cmd.get("prefixes", []))
                        desc = cmd.get("description", "")
                        is_llm = cmd.get("llm_required", False)
                        badge = f"{TUI.MAGENTA}[LLM]{TUI.RESET}" if is_llm else f"{TUI.CYAN}[FAST]{TUI.RESET}"
                        lines.append(
                            f"  {TUI.CYAN}{TUI.BOLD}{name:<12}{TUI.RESET} "
                            f"{TUI.DIM}{pfx:<18}{TUI.RESET} "
                            f"{TUI.DIM}{desc}{TUI.RESET} {badge}"
                        )
                    TUI.box(f"Search: {query}", lines, TUI.CYAN)
                else:
                    TUI.warn(f"No commands matching \"{query}\"")
                return
            elif ch in ('\x7f', '\b'):  # Backspace
                query = query[:-1]
            elif ch.isprintable():
                query += ch


# ============================================================
# Session Export
# ============================================================

def _session_export() -> None:
    """Dump the full activity log for the current session to a markdown file."""
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    export_path = Path.home() / f"actionflow_session_{ts}.md"

    lines = [
        f"# ActionFlow Session Export",
        f"",
        f"- **Date**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **Mode**: {LLM_STATE}",
    ]
    if LLM_STATE == LLM_STATE_READY:
        lines.append(f"- **Provider**: {_llm_provider}/{_llm_model}")
    lines.append("")
    lines.append("## Activity Log")
    lines.append("")

    # Read history from the JSONL file for this session
    session_start = datetime.fromtimestamp(_start_time).isoformat(timespec="seconds")
    try:
        entries = load_history_entries_safe(_HISTORY_PATH, limit=None)
        count = 0
        for entry in entries:
            if entry.get("ts", "") >= session_start:
                count += 1
                lines.append(
                    f"| {entry.get('ts', '?')} | `{entry.get('command', '?')}` | "
                    f"{str(entry.get('input', ''))[:60]} | {str(entry.get('output', ''))[:60]} | "
                    f"{entry.get('duration_ms', 0)}ms |"
                )
        if count == 0:
            lines.append("_No activity recorded this session._")
        else:
            # Insert table header before entries
            header_idx = lines.index("## Activity Log") + 2
            lines.insert(header_idx, "| Time | Command | Input | Output | Duration |")
            lines.insert(header_idx + 1, "|------|---------|-------|--------|----------|")
    except Exception as exc:
        lines.append(f"_Error reading history: {exc}_")

    lines.append("")

    try:
        export_path.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        TUI.micro_log(f"{TUI.GREEN}✓{TUI.RESET} Session exported → {TUI.CYAN}{export_path}{TUI.RESET}")
    except Exception as exc:
        TUI.error(f"Session export failed: {exc}")


# ============================================================
# System Tray (pystray)
# ============================================================

def _create_tray_icon_image(color: str = "green"):
    """Generate a 64x64 tray icon with the given status color."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    colors = {
        "green":  (0, 212, 170, 255),
        "yellow": (212, 170, 0, 255),
        "red":    (212, 0, 0, 255),
        "grey":   (128, 128, 128, 255),
    }
    c = colors.get(color, colors["green"])
    draw.rounded_rectangle([4, 4, 60, 60], radius=12, fill=c)
    try:
        fnt = ImageFont.truetype("DejaVuSansMono-Bold.ttf", 28)
    except Exception:
        fnt = ImageFont.load_default()
    draw.text((16, 14), "A", fill=(255, 255, 255, 255), font=fnt)
    return img


def _show_history_dialog() -> None:
    """Show a small tkinter window with recent history entries."""
    if not _TKINTER_AVAILABLE:
        return
    _get_tk_root()
    root = tk.Toplevel()
    root.title("ActionFlow History")
    root.geometry("650x400")
    root.configure(bg="#1a0a2e")

    text_widget = tk.Text(root, bg="#1a0a2e", fg="#e0e0e0",
                          font=("DejaVu Sans Mono", 9), wrap="word",
                          relief="flat", bd=0)
    text_widget.pack(fill="both", expand=True, padx=8, pady=8)

    try:
        entries = load_history_entries_safe(_HISTORY_PATH, limit=20)
        for e in entries[-20:]:
            ts = e.get("ts", "?")[:19]
            cmd = e.get("command", "?")
            inp = e.get("input", "")[:40].replace("\n", " ")
            out = e.get("output", "")[:40].replace("\n", " ")
            text_widget.insert("end", f"{ts}  {cmd:<12}  {inp}  →  {out}\n")
    except Exception as exc:
        text_widget.insert("end", f"Error: {exc}")

    text_widget.config(state="disabled")
    root.wait_window(root)


def _start_tray() -> None:
    """Start system tray icon in a background thread."""
    global _tray_icon
    try:
        import pystray
        from pystray import MenuItem
    except ImportError:
        TUI.warn("pystray not installed — tray icon disabled (pip install pystray Pillow)")
        return

    icon_img = _create_tray_icon_image(
        "grey" if _silent_mode else ("green" if LLM_STATE == LLM_STATE_READY else "yellow")
    )
    if icon_img is None:
        TUI.warn("Pillow not installed — tray icon disabled (pip install Pillow)")
        return

    def on_history(icon, item):
        threading.Thread(target=_show_history_dialog, daemon=True).start()

    def on_settings(icon, item):
        try:
            _system_integration.open_path(str(_CONFIG_PATH))
        except Exception:
            pass

    def on_reload(icon, item):
        _reload_config()

    def on_silent(icon, item):
        _toggle_silent_mode()

    def on_exit(icon, item):
        icon.stop()
        _exit_event.set()

    def silent_label(item):
        return f"Silent Mode {'[ON]' if _silent_mode else '[OFF]'}"

    icon = pystray.Icon(
        "actionflow",
        icon_img,
        "ActionFlow",
        menu=pystray.Menu(
            MenuItem("History (last 20)", on_history),
            MenuItem("Settings", on_settings),
            pystray.Menu.SEPARATOR,
            MenuItem("Reload Config", on_reload),
            MenuItem(silent_label, on_silent),
            pystray.Menu.SEPARATOR,
            MenuItem("Exit", on_exit),
        )
    )
    _tray_icon = icon
    try:
        icon.run()
    except Exception as exc:
        TUI.warn(f"Tray icon failed: {exc}")


# ============================================================
# Main Entry Point
# ============================================================

def _show_result_popup(title: str, text: str) -> None:
    if _TKINTER_AVAILABLE:
        result_popup = ResultPopup(title, text)
        result_popup.run()


def _drain_popup_queue_posix(fd: int, old_settings) -> None:
    try:
        popup_text = _popup_queue.get_nowait()
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        try:
            _handle_popup(popup_text)
        except Exception as exc:
            TUI.error(f"Popup error: {exc}")
        tty.setcbreak(fd)
    except queue.Empty:
        pass


def _drain_result_queue_posix(fd: int, old_settings) -> None:
    try:
        result_title, result_text, critical, special_ui = _result_queue.get_nowait()
        if notifications.should_show_result_popup(critical=critical, special_ui=special_ui):
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            try:
                _show_result_popup(result_title, result_text)
            except Exception as exc:
                notifications.log_error("Result popup error: %s", exc, exc_info=exc)
                TUI.error(f"Result popup error: {exc}")
            tty.setcbreak(fd)
            return
        notifications.log_info("Suppressed result popup: %s", result_title)
        tty.setcbreak(fd)
    except (queue.Empty, ValueError):
        pass


def _drain_popup_queue_windows() -> None:
    try:
        popup_text = _popup_queue.get_nowait()
        _handle_popup(popup_text)
    except queue.Empty:
        pass
    except Exception as exc:
        TUI.error(f"Popup error: {exc}")


def _drain_result_queue_windows() -> None:
    try:
        result_title, result_text, critical, special_ui = _result_queue.get_nowait()
        if notifications.should_show_result_popup(critical=critical, special_ui=special_ui):
            _show_result_popup(result_title, result_text)
        else:
            notifications.log_info("Suppressed result popup: %s", result_title)
    except (queue.Empty, ValueError):
        pass
    except Exception as exc:
        TUI.error(f"Result popup error: {exc}")


def _run_main_event_loop() -> None:
    if IS_WINDOWS:
        try:
            while not _exit_event.is_set():
                ch = _read_console_char_nonblocking(0.1)
                if ch == '\x03':
                    break
                elif ch == '/':
                    _command_search()
                elif ch in ('S', 's'):
                    _session_export()

                _drain_popup_queue_windows()
                _drain_result_queue_windows()
        except KeyboardInterrupt:
            pass
        return

    try:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        try:
            while not _exit_event.is_set():
                ch = _read_console_char_nonblocking(0.1)
                if ch == '\x03':  # Ctrl+C
                    break
                elif ch == '/':
                    _command_search()
                elif ch in ('S', 's'):
                    _session_export()

                _drain_popup_queue_posix(fd, old_settings)
                _drain_result_queue_posix(fd, old_settings)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except KeyboardInterrupt:
        pass


def set_setup_handlers(*, llm_handler=None, image_handler=None) -> None:
    global _llm_setup_handler, _image_setup_handler
    _llm_setup_handler = llm_handler
    _image_setup_handler = image_handler


def restart_hotkeys() -> None:
    global _hotkeys_registered, _hotkey_registration_error
    backend_name = type(_hotkey_manager).__name__
    notifications.log_info("Hotkey manager created: backend=%s", backend_name)
    _startup_logger.info("Hotkey manager created: backend=%s", backend_name)
    notifications.log_info("Registering hotkeys")
    _startup_logger.info("Registering hotkeys")
    try:
        _hotkey_manager.clear_hotkeys()
    except Exception:
        pass
    try:
        register_hotkeys(
            _hotkey_manager,
            CONFIG,
            on_hotkey_triggered,
            on_undo_triggered,
            on_silent_triggered,
        )
    except Exception as exc:
        _hotkeys_registered = False
        _hotkey_registration_error = str(exc)
        notifications.log_error("Hotkey registration failed: %s", exc, exc_info=exc)
        _startup_logger.exception("Hotkey registration failed: %s", exc)
        return
    _hotkeys_registered = True
    _hotkey_registration_error = None
    notifications.log_info("Hotkeys registered successfully: intercept=%s undo=%s", HOTKEY, UNDO_HOTKEY)
    _startup_logger.info("Hotkeys registered successfully: intercept=%s undo=%s", HOTKEY, UNDO_HOTKEY)


def initialize_background_runtime(*, force_mock_llm: bool = False, start_watchers: bool = True) -> dict:
    global _start_time, _pattern_learner, _force_mock_llm, _runtime_initialized
    if _runtime_initialized:
        return get_runtime_snapshot()

    _start_time = time.time()
    _force_mock_llm = force_mock_llm

    for cmd_name in CONFIG.get("commands", {}):
        _usage_counts.setdefault(cmd_name, 0)

    _init_llm()

    if start_watchers:
        _start_config_watcher()
        threading.Thread(target=_check_for_updates, daemon=True).start()

    _pattern_learner = PatternLearner(_HISTORY_PATH)
    _pattern_learner.load()

    notifications.configure_from_config(CONFIG)
    notifications.set_sender(_system_integration.notify)
    _sync_ui_mode_state()
    _register_personal_commands()
    restart_hotkeys()

    _runtime_initialized = True
    notifications.log_info("Background runtime initialized")
    _startup_logger.info("Background runtime initialized")
    return get_runtime_snapshot()


def get_runtime_snapshot() -> dict[str, object]:
    runtime_health, runtime_reason = _runtime_health_snapshot()
    return {
        "llm_state": LLM_STATE,
        "llm_provider": _llm_provider,
        "llm_model": _llm_model,
        "hotkeys": dict(CONFIG.get("hotkeys", {})),
        "hotkeys_registered": _hotkeys_registered,
        "hotkeys_error": _hotkey_registration_error,
        "hotkey_backend": type(_hotkey_manager).__name__,
        "hotkey_callback_count": _hotkey_callback_count,
        "last_hotkey_callback_at": _format_runtime_ts(_last_hotkey_callback_at),
        "last_hotkey_error": _last_hotkey_error,
        "last_hotkey_error_at": _format_runtime_ts(_last_hotkey_error_at),
        "command_count": len(CONFIG.get("commands", {})),
        "history_path": str(_HISTORY_PATH),
        "log_path": str(notifications.settings.log_path),
        "config_path": str(_CONFIG_PATH),
        "ui_mode": notifications.mode,
        "runtime_initialized": _runtime_initialized,
        "runtime_polling_active": _runtime_polling_active,
        "runtime_health": runtime_health,
        "runtime_health_reason": runtime_reason,
    }


def get_available_commands() -> dict[str, dict]:
    return dict(CONFIG.get("commands", {}))


def get_history_path() -> Path:
    return _HISTORY_PATH


def get_log_path() -> Path:
    return notifications.settings.log_path


def reload_runtime_config() -> dict[str, object]:
    _reload_config()
    return get_runtime_snapshot()


def save_secret_values(*, llm_api_key: str | None = None, image_api_key: str | None = None) -> None:
    if llm_api_key:
        save_llm_secrets(llm_api_key)
    if image_api_key:
        _save_image_api_key(image_api_key)


def poll_picker_request() -> str | None:
    try:
        return _popup_queue.get_nowait()
    except queue.Empty:
        return None


def poll_result_request() -> tuple[str, str, bool, bool] | None:
    try:
        return _result_queue.get_nowait()
    except queue.Empty:
        return None


def dispatch_picker_selection(command_name: str, payload: str, full_text: str | None = None) -> None:
    commands = CONFIG.get("commands", {})
    cmd_config = commands.get(command_name)
    if not isinstance(cmd_config, dict):
        raise KeyError(f"Unknown command: {command_name}")
    source_text = full_text if full_text is not None else payload
    dispatch(command_name, payload, source_text, cmd_config)


def main(keep_banner: bool = False, no_tray: bool = False, force_mock_llm: bool = False) -> None:
    global _start_time, _pattern_learner, _silent_mode, _force_mock_llm
    _start_time = time.time()
    _force_mock_llm = force_mock_llm

    print("\033[2J\033[3J\033[H", end="", flush=True)

    TUI.banner()

    # Initialize usage counters
    for cmd_name in CONFIG.get("commands", {}):
        _usage_counts[cmd_name] = 0

    # Init LLM state from config/env/secrets.
    _init_llm()

    # Environment box (rendered after LLM init so Mode is known)
    config_val = f"{TUI.DIM}{_CONFIG_PATH if _CONFIG_PATH.exists() else 'defaults (no config.yaml)'}{TUI.RESET}"
    if LLM_STATE == LLM_STATE_READY:
        mode_val = f"{TUI.GREEN}LLM ready ({_llm_provider}){TUI.RESET}"
    elif LLM_STATE == LLM_STATE_MOCK:
        mode_val = f"{TUI.YELLOW}LLM mock mode (explicit){TUI.RESET}"
    else:
        mode_val = f"{TUI.YELLOW}LLM setup required{TUI.RESET}"
    learning_val = f"{TUI.CYAN}0 samples{TUI.RESET}"
    if _pattern_learner and _pattern_learner.sample_count > 0:
        learning_val = f"{TUI.CYAN}{_pattern_learner.sample_count} samples{TUI.RESET}"
    display_val = _DISPLAY or ("native" if IS_WINDOWS else "-")
    user_val = _SUDO_USER or os.environ.get("USER") or os.environ.get("USERNAME", "?")
    TUI.box("Environment", [
        f"  {TUI.DIM}Session{TUI.RESET}    {TUI.CYAN}{_SESSION_TYPE}{TUI.RESET}",
        f"  {TUI.DIM}Display{TUI.RESET}    {TUI.CYAN}{display_val}{TUI.RESET}",
        f"  {TUI.DIM}Wayland{TUI.RESET}    {TUI.CYAN}{'Yes' if _IS_WAYLAND else 'No'}{TUI.RESET}",
        f"  {TUI.DIM}User{TUI.RESET}       {TUI.CYAN}{user_val}{TUI.RESET}",
        f"  {TUI.DIM}Mode{TUI.RESET}       {mode_val}",
        f"  {TUI.DIM}Learning{TUI.RESET}   {learning_val}",
        f"  {TUI.DIM}Config{TUI.RESET}     {config_val}",
    ], TUI.CYAN)

    print()
    TUI.llm_status_box()

    print()
    TUI.activity_placeholder()
    print()
    TUI.commands_table()
    print()
    TUI.keybind_table()
    print()

    # Collapse banner after init unless --banner flag is set
    if not keep_banner:
        time.sleep(2)
        print("\033[2J\033[3J\033[H", end="", flush=True)
        TUI.header_line()
        print()
        TUI.llm_status_box()
        print()
        TUI.activity_placeholder()
        print()
        TUI.commands_table()
        print()
        TUI.keybind_table()
        print()

    # Start config hot-reload watcher
    _start_config_watcher()

    # Background auto-update check
    threading.Thread(target=_check_for_updates, daemon=True).start()

    # Start system tray icon
    start_tray_thread(not no_tray, _start_tray)

    # Initialize PatternLearner
    _pattern_learner = PatternLearner(_HISTORY_PATH)
    _pattern_learner.load()
    if _pattern_learner.sample_count > 0:
        TUI.micro_log(f"PatternLearner: {_pattern_learner.sample_count} samples loaded")

    # Initialize UI mode from config
    notifications.configure_from_config(CONFIG)
    notifications.set_sender(_system_integration.notify)
    _sync_ui_mode_state()

    # Register personal commands from config
    _register_personal_commands()

    # Register hotkeys
    register_hotkeys(
        _hotkey_manager,
        CONFIG,
        on_hotkey_triggered,
        on_undo_triggered,
        on_silent_triggered,
    )

    notify(
        "ActionFlow Active",
        f"{HOTKEY.upper()} to intercept | {UNDO_HOTKEY.upper()} to undo | Ctrl+C to exit",
    )

    TUI.separator()
    cmd_count = len(CONFIG.get("commands", {}))
    if LLM_STATE == LLM_STATE_READY:
        llm_label = f"LLM ready: {_llm_provider}"
    elif LLM_STATE == LLM_STATE_MOCK:
        llm_label = "LLM mock mode (explicit)"
    else:
        llm_label = "LLM setup required"
    TUI.micro_log(f"{TUI.GREEN}✓{TUI.RESET} Listening for hotkeys...")
    TUI.micro_log(f"{cmd_count} commands loaded | {llm_label} | {HOTKEY.upper()} to intercept")
    TUI.micro_log(f"{TUI.DIM}/ = search  S = export session  Ctrl+C = exit{TUI.RESET}")
    print()

    set_runtime_polling_active(True)
    try:
        _run_main_event_loop()
    finally:
        set_runtime_polling_active(False)
    print()
    TUI.separator()
    TUI.status("рџ‘‹", "Shutting down. Goodbye!", TUI.MAGENTA)
    notify(APP_NAME, "Shutting down. Goodbye!")
    return

    try:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        tty.setcbreak(fd)
        try:
            while not _exit_event.is_set():
                if select.select([sys.stdin], [], [], 0.1)[0]:
                    ch = sys.stdin.read(1)
                    if ch == '\x03':  # Ctrl+C
                        break
                    elif ch == '/':
                        _command_search()
                    elif ch in ('S', 's'):
                        _session_export()

                # Check popup queue — command picker triggered by hotkey
                try:
                    popup_text = _popup_queue.get_nowait()
                    # Restore terminal for tkinter popup
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    try:
                        _handle_popup(popup_text)
                    except Exception as exc:
                        TUI.error(f"Popup error: {exc}")
                    # Restore cbreak for TUI
                    tty.setcbreak(fd)
                except queue.Empty:
                    pass

                # Check result queue — display-only popups (wiki, define, count)
                try:
                    result_title, result_text = _result_queue.get_nowait()
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    try:
                        if _TKINTER_AVAILABLE:
                            result_popup = ResultPopup(result_title, result_text)
                            result_popup.run()
                    except Exception as exc:
                        TUI.error(f"Result popup error: {exc}")
                    tty.setcbreak(fd)
                except (queue.Empty, ValueError):
                    pass
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except KeyboardInterrupt:
        pass

    print()
    TUI.separator()
    TUI.status("👋", "Shutting down. Goodbye!", TUI.MAGENTA)
    notify(APP_NAME, "Shutting down. Goodbye!")


# ============================================================
# Auto-Update Checker
# ============================================================

def _check_for_updates() -> None:
    """Silently check GitHub releases for a newer version tag. Runs in background thread."""
    try:
        url = "https://api.github.com/repos/azimxxd/actionflow/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "ActionFlow"})
        data = json.loads(_safe_url_read(req, timeout=5).decode())
        latest_tag = data.get("tag_name", "").lstrip("v")
        current = __version__.lstrip("v")
        if latest_tag and latest_tag != current:
            # Simple version comparison
            try:
                latest_parts = [int(x) for x in latest_tag.split(".")]
                current_parts = [int(x) for x in current.split(".")]
                if latest_parts > current_parts:
                    TUI.micro_log(
                        f"{TUI.YELLOW}Update available: v{current} → v{latest_tag} "
                        f"(run git pull){TUI.RESET}"
                    )
            except (ValueError, TypeError):
                pass
    except Exception:
        pass  # Silent on any failure





# ============================================================
# History CLI Browser
# ============================================================

def show_history(grep_filter: str | None = None) -> None:
    """Print last 50 history entries as a formatted table. Optionally filter by command."""
    history_path = Path.home() / ".actionflow_history.jsonl"
    if not history_path.exists():
        print(f"{TUI.YELLOW}No history file found at {history_path}{TUI.RESET}")
        return

    try:
        entries = load_history_entries_safe(history_path, limit=None)
        if grep_filter:
            entries = [
                entry for entry in entries
                if grep_filter.lower() in str(entry.get("command", "")).lower()
            ]
    except Exception as exc:
        print(f"{TUI.RED}Error reading history: {exc}{TUI.RESET}")
        return

    # Take last 50
    entries = entries[-50:]

    if not entries:
        label = f" matching \"{grep_filter}\"" if grep_filter else ""
        print(f"{TUI.YELLOW}No history entries found{label}.{TUI.RESET}")
        return

    # Print header
    print()
    print(f"  {TUI.BOLD}{TUI.CYAN}{'Timestamp':<22} {'Command':<12} {'Input':<30} {'Output':<30} {'ms':>6} {'Provider':<15}{TUI.RESET}")
    print(f"  {TUI.DIM}{'─' * 115}{TUI.RESET}")

    for e in entries:
        ts = e.get("ts", "?")[:19]
        cmd = e.get("command", "?")[:10]
        inp = e.get("input", "")[:28]
        out = e.get("output", "")[:28]
        dur = e.get("duration_ms", 0)
        prov = e.get("provider", "?")[:13]

        # Color code by command type
        is_err = out.startswith("ERROR:")
        if is_err:
            color = TUI.RED
        else:
            color = TUI.CYAN

        print(
            f"  {TUI.DIM}{ts:<22}{TUI.RESET} "
            f"{color}{TUI.BOLD}{cmd:<12}{TUI.RESET} "
            f"{TUI.DIM}{inp:<30} {out:<30}{TUI.RESET} "
            f"{TUI.DIM}{dur:>6}{TUI.RESET} "
            f"{TUI.DIM}{prov:<15}{TUI.RESET}"
        )

    print()
    label = f" (filtered: {grep_filter})" if grep_filter else ""
    print(f"  {TUI.DIM}{len(entries)} entries{label}{TUI.RESET}")
    print()


def install_systemd_service() -> None:
    """Generate and install a systemd unit file for ActionFlow."""
    if not IS_LINUX:
        print(f"{TUI.YELLOW}Systemd install is only available on Linux.{TUI.RESET}")
        sys.exit(1)

    if os.geteuid() != 0:
        print(f"{TUI.RED}Error: --install must be run as root (sudo).{TUI.RESET}")
        sys.exit(1)

    script_path = Path(__file__).resolve()
    working_dir = script_path.parent
    python_bin = sys.executable
    sudo_user = os.environ.get("SUDO_USER", "")

    if not sudo_user:
        print(f"{TUI.RED}Error: Could not determine SUDO_USER. Run with: sudo -E python main.py --install{TUI.RESET}")
        sys.exit(1)

    unit_content = f"""\
[Unit]
Description=ActionFlow by WatashiGPT
After=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/sudo -E {python_bin} {script_path}
WorkingDirectory={working_dir}
User={sudo_user}
Restart=on-failure
RestartSec=5
Environment=DISPLAY=:0
PassEnvironment=WAYLAND_DISPLAY XDG_SESSION_TYPE XDG_RUNTIME_DIR DBUS_SESSION_BUS_ADDRESS

[Install]
WantedBy=graphical-session.target
"""

    unit_path = Path("/etc/systemd/system/actionflow.service")
    unit_path.write_text(unit_content)
    print(f"{TUI.GREEN}✓{TUI.RESET} Wrote {unit_path}")

    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "actionflow"], check=True)
    print(f"{TUI.GREEN}✓{TUI.RESET} Service enabled")
    print()
    print(f"  Start now with:  {TUI.CYAN}systemctl start actionflow{TUI.RESET}")
    print(f"  Check status:    {TUI.CYAN}systemctl status actionflow{TUI.RESET}")
    print(f"  View logs:       {TUI.CYAN}journalctl -u actionflow -f{TUI.RESET}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ActionFlow by WatashiGPT")
    parser.add_argument("--install", action="store_true", help="Install as a systemd service")
    parser.add_argument("--banner", action="store_true", help="Keep the full ASCII banner permanently")
    parser.add_argument("--history", action="store_true", help="Browse last 50 history entries")
    parser.add_argument("--grep", type=str, default=None, help="Filter history by command name (use with --history)")
    parser.add_argument("--no-tray", action="store_true", help="Disable system tray icon")
    args = parser.parse_args()

    if args.install:
        install_systemd_service()
    elif args.history:
        show_history(grep_filter=args.grep)
    else:
        main(keep_banner=args.banner, no_tray=args.no_tray)

