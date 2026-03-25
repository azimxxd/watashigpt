from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import keyboard

try:
    import pyperclip
except ImportError:
    pyperclip = None

from actionflow.core.models import AppContext
from actionflow.platform.base import ClipboardManager, HotkeyManager, PlatformServices, SystemIntegration, WindowManager


def _sanitize_notify(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\x00", "")[:300]


def _find_focused_sway(node: dict) -> dict | None:
    if node.get("focused"):
        return node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        result = _find_focused_sway(child)
        if result:
            return result
    return None


class LinuxSystemIntegration(SystemIntegration):
    def __init__(self, display: str, wayland_display: str, dbus_session: str, sudo_user: str, is_wayland: bool):
        self._display = display
        self._wayland_display = wayland_display
        self._dbus_session = dbus_session
        self._sudo_user = sudo_user
        self._is_wayland = is_wayland

    def run_process(self, cmd: list[str], **kwargs):
        env = {**os.environ, "DISPLAY": self._display}
        if self._dbus_session:
            env["DBUS_SESSION_BUS_ADDRESS"] = self._dbus_session
        if self._wayland_display:
            env["WAYLAND_DISPLAY"] = self._wayland_display
            env["XDG_SESSION_TYPE"] = "wayland"
        if self._sudo_user and os.geteuid() == 0:
            preserve = "DISPLAY,DBUS_SESSION_BUS_ADDRESS"
            if self._is_wayland:
                preserve += ",WAYLAND_DISPLAY,XDG_RUNTIME_DIR,XDG_SESSION_TYPE"
            cmd = ["sudo", "-u", self._sudo_user, f"--preserve-env={preserve}"] + cmd
        return subprocess.run(cmd, env=env, **kwargs)

    def notify(self, title: str, message: str) -> None:
        self.run_process(
            ["notify-send", "-t", "5000", _sanitize_notify(title), _sanitize_notify(message)],
            capture_output=True,
            timeout=3,
        )

    def open_path(self, path: str) -> None:
        self.run_process(["xdg-open", path], capture_output=True, timeout=5)


class LinuxClipboardManager(ClipboardManager):
    def __init__(self, system: LinuxSystemIntegration, is_wayland: bool, backend: str):
        self._system = system
        self._is_wayland = is_wayland
        self._backend = backend

    def copy(self, text: str) -> None:
        if self._backend == "pyperclip" and pyperclip is not None:
            pyperclip.copy(text)
            return
        if self._is_wayland:
            proc = self._system.run_process(["wl-copy", "--", text], capture_output=True)
        else:
            proc = self._system.run_process(["xclip", "-selection", "clipboard"], input=text.encode(), capture_output=True)
        if proc.returncode != 0:
            raise RuntimeError("Clipboard copy failed on Linux")

    def paste(self) -> str:
        if self._backend == "pyperclip" and pyperclip is not None:
            return pyperclip.paste()
        cmd = ["wl-paste", "--no-newline"] if self._is_wayland else ["xclip", "-selection", "clipboard", "-o"]
        proc = self._system.run_process(cmd, capture_output=True, text=True)
        return proc.stdout if proc.returncode == 0 else ""

    def get_primary_selection(self) -> str:
        proc = self._system.run_process(["wl-paste", "--primary", "--no-newline"], capture_output=True, text=True)
        return proc.stdout if proc.returncode == 0 else ""

    def copy_image(self, image_path: str) -> bool:
        try:
            if self._is_wayland:
                with open(image_path, "rb") as handle:
                    proc = self._system.run_process(["wl-copy", "--type", "image/png"], input=handle.read(), capture_output=True)
            else:
                proc = self._system.run_process(
                    ["xclip", "-selection", "clipboard", "-t", "image/png", "-i", image_path],
                    capture_output=True,
                )
            return proc.returncode == 0
        except Exception:
            return False


class LinuxHotkeyManager(HotkeyManager):
    def __init__(self, system: LinuxSystemIntegration, has_wtype: bool):
        self._system = system
        self._has_wtype = has_wtype

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

    def send_copy(self) -> None:
        self.release_modifiers()
        keyboard.send("ctrl+c")

    def send_paste(self) -> None:
        self.release_modifiers()
        time.sleep(0.05)
        if self._has_wtype:
            try:
                result = self._system.run_process(
                    ["wtype", "-d", "50", "-M", "ctrl", "-k", "v", "-m", "ctrl"],
                    capture_output=True,
                    timeout=3,
                )
                if result.returncode == 0:
                    return
            except Exception:
                pass
        keyboard.send("ctrl+v")


class LinuxWindowManager(WindowManager):
    def __init__(self, system: LinuxSystemIntegration, is_wayland: bool, backend: str):
        self._system = system
        self._is_wayland = is_wayland
        self._backend = backend

    def detect_active_window(self) -> AppContext:
        title = ""
        app_name = ""
        try:
            if self._is_wayland:
                if self._backend in ("auto", "kdotool"):
                    try:
                        proc = self._system.run_process(["kdotool", "getactivewindow", "getwindowname"], capture_output=True, text=True, timeout=2)
                        if proc.returncode == 0:
                            title = proc.stdout.strip().lower()
                    except Exception:
                        pass
                if not title and self._backend in ("auto", "swaymsg"):
                    proc = self._system.run_process(["swaymsg", "-t", "get_tree"], capture_output=True, text=True, timeout=2)
                    if proc.returncode == 0:
                        tree = json.loads(proc.stdout)
                        focused = _find_focused_sway(tree)
                        if focused:
                            title = (focused.get("name", "") or "").lower()
                            app_name = (focused.get("app_id", "") or "").lower()
            elif self._backend in ("auto", "xdotool"):
                proc = self._system.run_process(["xdotool", "getactivewindow", "getwindowname"], capture_output=True, text=True, timeout=2)
                if proc.returncode == 0:
                    title = proc.stdout.strip().lower()
        except Exception:
            pass

        searchable = " ".join(part for part in [title, app_name] if part)
        for ctx_type, patterns in AppContext.APP_PATTERNS.items():
            for pattern in patterns:
                if pattern in searchable:
                    return AppContext(ctx_type, title, app_name or pattern)
        return AppContext(AppContext.UNKNOWN, title, app_name)


def build_linux_services(clipboard_backend: str = "auto", window_backend: str = "auto") -> PlatformServices:
    session_type = os.environ.get("XDG_SESSION_TYPE", "x11")
    is_wayland = session_type == "wayland"
    system = LinuxSystemIntegration(
        display=os.environ.get("DISPLAY", ":0"),
        wayland_display=os.environ.get("WAYLAND_DISPLAY", ""),
        dbus_session=os.environ.get("DBUS_SESSION_BUS_ADDRESS", ""),
        sudo_user=os.environ.get("SUDO_USER", ""),
        is_wayland=is_wayland,
    )
    return PlatformServices(
        clipboard=LinuxClipboardManager(system, is_wayland=is_wayland, backend=clipboard_backend),
        hotkeys=LinuxHotkeyManager(system, has_wtype=is_wayland and shutil.which("wtype") is not None),
        windows=LinuxWindowManager(system, is_wayland=is_wayland, backend=window_backend),
        system=system,
    )
