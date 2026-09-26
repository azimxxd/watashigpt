"""Linux backend — X11 (xclip, xdotool) and Wayland (wl-clipboard, the portal
paste helper, kdotool / swaymsg).

ActionFlow runs as root on Linux (the `keyboard` library reads /dev/input);
everything that touches the user's session goes through run_as_user().
"""

from __future__ import annotations

import base64
import json
import os
import select
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import keyboard

from actionflow.tui import TUI

SESSION_TYPE: str = os.environ.get("XDG_SESSION_TYPE", "x11")
IS_WAYLAND: bool = SESSION_TYPE == "wayland"
SUDO_USER: str = os.environ.get("SUDO_USER", "")
DISPLAY: str = os.environ.get("DISPLAY", ":0")
WAYLAND_DISPLAY: str = os.environ.get("WAYLAND_DISPLAY", "")
DBUS_SESSION: str = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")
HAS_WTYPE: bool = IS_WAYLAND and shutil.which("wtype") is not None
HAS_YDOTOOL: bool = IS_WAYLAND and shutil.which("ydotool") is not None

_wtype_disabled = False
_ydotool_disabled = False


def run_as_user(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command as the real (non-root) user with their session env."""
    env = {**os.environ, "DISPLAY": DISPLAY}
    if DBUS_SESSION:
        env["DBUS_SESSION_BUS_ADDRESS"] = DBUS_SESSION
    if WAYLAND_DISPLAY:
        env["WAYLAND_DISPLAY"] = WAYLAND_DISPLAY
        env["XDG_SESSION_TYPE"] = "wayland"
    if SUDO_USER and os.geteuid() == 0:
        preserve = "DISPLAY,DBUS_SESSION_BUS_ADDRESS"
        if IS_WAYLAND:
            preserve += ",WAYLAND_DISPLAY,XDG_RUNTIME_DIR,XDG_SESSION_TYPE"
        cmd = ["sudo", "-u", SUDO_USER, f"--preserve-env={preserve}"] + cmd
    return subprocess.run(cmd, env=env, **kwargs)


def effective_home() -> Path:
    """The real user's home, also when running under sudo."""
    if SUDO_USER:
        home = os.path.expanduser(f"~{SUDO_USER}")
        if home and not home.startswith("~"):
            return Path(home)
    return Path.home()


# ============================================================
# Portal paste helper (GNOME Wayland)
# ============================================================

_helper_proc: subprocess.Popen | None = None
_helper_lock = threading.Lock()


def start_paste_helper() -> bool:
    """Start paste_helper.py as the real user (xdg-desktop-portal RemoteDesktop)."""
    global _helper_proc
    helper_path = Path(__file__).parent / "paste_helper.py"
    env = {**os.environ}
    if WAYLAND_DISPLAY:
        env["WAYLAND_DISPLAY"] = WAYLAND_DISPLAY
    if DBUS_SESSION:
        env["DBUS_SESSION_BUS_ADDRESS"] = DBUS_SESSION
    if SUDO_USER and "XDG_RUNTIME_DIR" not in env:
        try:
            uid = int(subprocess.check_output(["id", "-u", SUDO_USER]).strip())
            env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
        except Exception:
            pass

    # The helper needs the distro Python (python3-dbus / python3-gi live there)
    cmd = ["/usr/bin/python3", "-u", str(helper_path)]
    if SUDO_USER and os.geteuid() == 0:
        preserve = ("DISPLAY,DBUS_SESSION_BUS_ADDRESS,WAYLAND_DISPLAY,XDG_RUNTIME_DIR,"
                    "XDG_SESSION_TYPE,XDG_CURRENT_DESKTOP")
        cmd = ["sudo", "-u", SUDO_USER, f"--preserve-env={preserve}"] + cmd
    try:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env, text=True)
        ready, _, _ = select.select([proc.stdout], [], [], 10)
        line = proc.stdout.readline().strip() if ready else ""
        if line == "READY":
            _helper_proc = proc
            return True
        proc.kill()
        TUI.warn(f"Paste helper failed: {line or 'timed out waiting for READY'}")
    except Exception as exc:
        TUI.warn(f"Paste helper start error: {exc}")
    return False


def stop_paste_helper() -> None:
    global _helper_proc
    if _helper_proc is None:
        return
    try:
        _portal_send("QUIT")
        _helper_proc.wait(timeout=2)
    except Exception:
        try:
            _helper_proc.kill()
        except Exception:
            pass
    _helper_proc = None


def _portal_send_raw(command: str) -> str | None:
    """Send a command to the helper. Returns response data on OK, None on error."""
    global _helper_proc
    with _helper_lock:
        if _helper_proc is None or _helper_proc.poll() is not None:
            _helper_proc = None
            return None
        try:
            _helper_proc.stdin.write(command + "\n")
            _helper_proc.stdin.flush()
            ready, _, _ = select.select([_helper_proc.stdout], [], [], 3)
            if not ready:
                TUI.warn("Portal helper response timeout")
                return None
            response = _helper_proc.stdout.readline().strip()
            if response == "OK":
                return ""
            if response.startswith("OK:"):
                return response[3:]
            return None
        except Exception as exc:
            TUI.warn(f"Portal send error: {exc}")
            _helper_proc = None
            return None


def _portal_send(command: str) -> bool:
    return _portal_send_raw(command) is not None


# ============================================================
# Clipboard / selection
# ============================================================

def clipboard_set(text: str) -> bool:
    if IS_WAYLAND and _helper_proc is not None:
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        if _portal_send(f"CLIPBOARD:{b64}"):
            return True
        TUI.warn("Helper clipboard failed — falling back to wl-copy")
    # Text goes via stdin: argv is visible in `ps` and has a length limit
    cmd = ["wl-copy"] if IS_WAYLAND else ["xclip", "-selection", "clipboard"]
    try:
        proc = run_as_user(cmd, input=text.encode("utf-8"), capture_output=True, timeout=3)
        if proc.returncode != 0:
            TUI.error(f"Clipboard copy failed: {proc.stderr.decode(errors='ignore').strip()}")
        return proc.returncode == 0
    except subprocess.TimeoutExpired:
        TUI.warn(f"{cmd[0]} timed out")
    except FileNotFoundError:
        TUI.error(f"{cmd[0]} not found — install {'wl-clipboard' if IS_WAYLAND else 'xclip'}")
    return False


def clipboard_get(timeout: float = 1.0) -> str:
    cmd = ["wl-paste", "--no-newline"] if IS_WAYLAND else ["xclip", "-selection", "clipboard", "-o"]
    try:
        proc = run_as_user(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.stdout if proc.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def clipboard_set_image(image_path: str) -> bool:
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
            ".svg": "image/svg+xml"}.get(Path(image_path).suffix.lower(), "image/png")
    try:
        if IS_WAYLAND:
            with open(image_path, "rb") as f:
                proc = run_as_user(["wl-copy", "--type", mime], input=f.read(),
                                   capture_output=True, timeout=5)
        else:
            proc = run_as_user(["xclip", "-selection", "clipboard", "-t", mime, "-i", image_path],
                               capture_output=True, timeout=5)
        return proc.returncode == 0
    except Exception as exc:
        TUI.error(f"Image clipboard copy failed: {exc}")
        return False


def capture_selection() -> str:
    """Selected text: PRIMARY selection on Wayland, Ctrl+C round-trip on X11."""
    if IS_WAYLAND:
        try:
            proc = run_as_user(["wl-paste", "--primary", "--no-newline"],
                               capture_output=True, text=True, timeout=1.5)
            return proc.stdout if proc.returncode == 0 else ""
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            TUI.error(f"Primary selection read failed: {exc}")
            return ""
    old = clipboard_get(timeout=0.2)
    # A marker tells a real copy apart from "selection equals old clipboard"
    marker = f"__ACTIONFLOW_MARKER_{time.time_ns()}__"
    clipboard_set(marker)
    time.sleep(0.05)
    keyboard.send("ctrl+c")
    time.sleep(0.15)
    text = clipboard_get(timeout=0.25)
    clipboard_set(old)
    return "" if text == marker else text


# ============================================================
# Keyboard
# ============================================================

def reset_keyboard() -> None:
    """Release modifiers left over from the hotkey combo."""
    for key in ("ctrl", "alt", "shift"):
        try:
            keyboard.release(key)
        except Exception:
            pass
    try:
        keyboard._pressed_events.clear()
    except Exception:
        pass


def add_hotkey(spec: str, callback) -> None:
    keyboard.add_hotkey(spec, callback)


def send_paste(is_terminal: bool = False) -> None:
    """Paste into the focused window. Terminals use Ctrl+Shift+V.

    Order: portal helper (GNOME Wayland) → ydotool → wtype → uinput.
    """
    global _wtype_disabled, _ydotool_disabled
    reset_keyboard()
    time.sleep(0.12)

    if _helper_proc is not None:
        if _portal_send("PASTE_TERMINAL" if is_terminal else "PASTE"):
            time.sleep(0.08)
            return
        TUI.warn("Portal paste failed — trying next method")

    if HAS_YDOTOOL and not _ydotool_disabled:
        # evdev codes: 29 LEFTCTRL, 42 LEFTSHIFT, 47 V
        keys = (["29:1", "42:1", "47:1", "47:0", "42:0", "29:0"] if is_terminal
                else ["29:1", "47:1", "47:0", "29:0"])
        try:
            result = subprocess.run(["ydotool", "key", *keys], capture_output=True, timeout=1.0)
            if result.returncode == 0:
                time.sleep(0.05)
                reset_keyboard()
                return
            TUI.warn(f"ydotool failed: {result.stderr.decode(errors='ignore').strip()}")
        except FileNotFoundError:
            _ydotool_disabled = True
        except Exception as exc:
            TUI.warn(f"ydotool error: {exc}")

    if HAS_WTYPE and not _wtype_disabled:
        mods = ["-M", "ctrl", "-M", "shift"] if is_terminal else ["-M", "ctrl"]
        try:
            result = run_as_user(["wtype", "-d", "50", *mods, "-k", "v", "-m", "ctrl"]
                                 + (["-m", "shift"] if is_terminal else []),
                                 capture_output=True, timeout=0.6)
            if result.returncode == 0:
                return
            err = result.stderr.decode(errors="ignore").strip()
            if "virtual keyboard protocol" in err.lower():
                _wtype_disabled = True
            else:
                TUI.warn(f"wtype failed: {err}")
        except Exception as exc:
            TUI.warn(f"wtype error: {exc}")

    keyboard.send("ctrl+shift+v" if is_terminal else "ctrl+v")
    time.sleep(0.1)
    reset_keyboard()


def focus_by_alt_tab() -> bool:
    """Fallback when direct window activation is unavailable."""
    try:
        reset_keyboard()
        time.sleep(0.04)
        keyboard.press("alt")
        keyboard.press_and_release("tab")
        time.sleep(0.03)
        keyboard.release("alt")
        time.sleep(0.14)
        return True
    except Exception as exc:
        TUI.warn(f"Alt+Tab fallback failed: {exc}")
        return False


# ============================================================
# Windows
# ============================================================

def _find_focused_sway(node: dict) -> dict | None:
    if node.get("focused"):
        return node
    for child in node.get("nodes", []) + node.get("floating_nodes", []):
        found = _find_focused_sway(child)
        if found:
            return found
    return None


def active_window_title() -> str:
    """Lower-cased title of the focused window ('' if unknown)."""
    try:
        if not IS_WAYLAND:
            proc = run_as_user(["xdotool", "getactivewindow", "getwindowname"],
                               capture_output=True, text=True, timeout=2)
            return proc.stdout.strip().lower() if proc.returncode == 0 else ""
        resp = _portal_send_raw("GETFOCUSED") if _helper_proc is not None else None
        if resp and resp.count(":") >= 2:
            _app, _pid, b64_title = resp.split(":", 2)
            return base64.b64decode(b64_title).decode("utf-8").lower()
        if shutil.which("kdotool"):
            proc = run_as_user(["kdotool", "getactivewindow", "getwindowname"],
                               capture_output=True, text=True, timeout=2)
            if proc.returncode == 0:
                return proc.stdout.strip().lower()
        if shutil.which("swaymsg"):
            proc = run_as_user(["swaymsg", "-t", "get_tree"], capture_output=True, text=True, timeout=2)
            if proc.returncode == 0:
                focused = _find_focused_sway(json.loads(proc.stdout))
                if focused:
                    return (focused.get("name") or focused.get("app_id") or "").lower()
    except Exception:
        pass
    return ""


def active_window_id() -> str | None:
    """Opaque id for focus_window(): "atspi:<pid>:<app>", "kde:<id>" or "x11:<id>"."""
    try:
        if not IS_WAYLAND:
            proc = run_as_user(["xdotool", "getactivewindow"], capture_output=True, text=True, timeout=2)
            return f"x11:{proc.stdout.strip()}" if proc.returncode == 0 and proc.stdout.strip() else None
        # GNOME 45+ disabled Shell.Eval — AT-SPI via the helper is the way in
        resp = _portal_send_raw("GETFOCUSED") if _helper_proc is not None else None
        if resp and resp.count(":") >= 2:
            app_name, pid_str, _ = resp.split(":", 2)
            return f"atspi:{pid_str}:{app_name}"
        if shutil.which("kdotool"):
            proc = run_as_user(["kdotool", "getactivewindow"], capture_output=True, text=True, timeout=2)
            if proc.returncode == 0 and proc.stdout.strip():
                return f"kde:{proc.stdout.strip()}"
    except Exception:
        pass
    return None


def focus_window(window_id: str) -> bool:
    kind, _, wid = window_id.partition(":")
    try:
        if kind == "atspi":
            return _helper_proc is not None and _portal_send(f"ACTIVATE:{wid.split(':', 1)[0]}")
        if kind == "kde":
            return run_as_user(["kdotool", "windowactivate", wid], capture_output=True, timeout=2).returncode == 0
        if kind == "x11":
            return run_as_user(["xdotool", "windowactivate", wid], capture_output=True, timeout=2).returncode == 0
    except Exception as exc:
        TUI.warn(f"Focus restore failed: {exc}")
    return False


# ============================================================
# Misc
# ============================================================

def _notify_markup(s: str) -> str:
    """notify-send interprets a subset of HTML."""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\x00", "")[:300]


def notify(title: str, message: str) -> None:
    try:
        run_as_user(["notify-send", "-a", "ActionFlow", "-t", "5000",
                     _notify_markup(title), _notify_markup(message)],
                    capture_output=True, timeout=3)
    except subprocess.TimeoutExpired:
        TUI.warn("notify-send timed out")
    except FileNotFoundError:
        TUI.warn("notify-send not found — install libnotify-bin")


def open_path(path: str) -> bool:
    try:
        return run_as_user(["xdg-open", path], capture_output=True, timeout=5).returncode == 0
    except FileNotFoundError:
        TUI.warn("xdg-open not found")
        return False


def install_systemd_service(script_path: Path) -> None:
    """System service running as root (needed for /dev/input) that talks to
    the invoking user's graphical session."""
    if os.geteuid() != 0 or not SUDO_USER:
        print(f"{TUI.RED}Run with: sudo -E python main.py --install{TUI.RESET}")
        sys.exit(1)
    uid = int(subprocess.check_output(["id", "-u", SUDO_USER]).strip())
    unit = f"""\
[Unit]
Description=ActionFlow by WatashiGPT
After=network-online.target

[Service]
Type=simple
User=root
Environment=SUDO_USER={SUDO_USER}
Environment=DISPLAY={DISPLAY}
Environment=XDG_SESSION_TYPE={SESSION_TYPE}
Environment=WAYLAND_DISPLAY={WAYLAND_DISPLAY or 'wayland-0'}
Environment=XDG_RUNTIME_DIR=/run/user/{uid}
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus
Environment=PYTHONUNBUFFERED=1
# API keys: ACTIONFLOW_API_KEY=... (root can't read your desktop keyring)
EnvironmentFile=-/etc/actionflow.env
ExecStart={sys.executable} {script_path}
WorkingDirectory={script_path.parent}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    unit_path = Path("/etc/systemd/system/actionflow.service")
    unit_path.write_text(unit)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "actionflow"], check=True)
    print(f"{TUI.GREEN}✓{TUI.RESET} Installed and started {unit_path}")
    print(f"  Put your key in /etc/actionflow.env:  ACTIONFLOW_API_KEY=...  (chmod 600)")
    print(f"  Logs:  {TUI.CYAN}journalctl -u actionflow -f{TUI.RESET}")

