#!/usr/bin/env python3
"""Portal paste helper — runs as the real user, receives commands via stdin.

Started by main.py as: sudo -u $USER python3 -u paste_helper.py
Main app sends commands via stdin:
  PASTE\n              → send Ctrl+V via portal
  COPY\n               → send Ctrl+C via portal
  CLIPBOARD:<base64>\n → set clipboard via wl-copy (runs as user, no sudo issues)
  CLIPBOARD_GET\n      → get clipboard via wl-paste
  GETFOCUSED\n         → get focused window via AT-SPI (app:pid:title)
  ACTIVATE:<pid>\n     → activate/focus window by PID via D-Bus
  QUIT\n               → exit
Responds with OK\n, OK:<data>\n, or ERR:<message>\n
"""
import base64
import subprocess
import sys
import time

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

# AT-SPI for window detection on GNOME Wayland (Shell.Eval disabled since GNOME 45+)
try:
    import gi
    gi.require_version('Atspi', '2.0')
    from gi.repository import Atspi
    Atspi.init()
    _ATSPI_AVAILABLE = True
except Exception:
    _ATSPI_AVAILABLE = False

KEY_LEFTCTRL = 29
KEY_V = 47
KEY_C = 46

DBusGMainLoop(set_as_default=True)
bus = dbus.SessionBus()
portal = bus.get_object(
    "org.freedesktop.portal.Desktop",
    "/org/freedesktop/portal/desktop",
)
rd = dbus.Interface(portal, "org.freedesktop.portal.RemoteDesktop")

session_path = None
phase = 0
loop = GLib.MainLoop()


def on_response(response, results, path=None):
    global session_path, phase
    if phase == 0:
        if response != 0:
            phase = -1
            loop.quit()
            return
        session_path = str(results.get("session_handle", ""))
        phase = 1
        rd.SelectDevices(session_path, {"types": dbus.UInt32(1)})
    elif phase == 1:
        phase = 2
        rd.Start(session_path, "", {})
    elif phase == 2:
        phase = 3 if response == 0 else -1
        loop.quit()


bus.add_signal_receiver(
    on_response,
    signal_name="Response",
    dbus_interface="org.freedesktop.portal.Request",
    path_keyword="path",
)

rd.CreateSession({
    "session_handle_token": dbus.String("actionflow_paste"),
    "handle_token": dbus.String("actionflow_req"),
})
GLib.timeout_add_seconds(10, lambda: loop.quit())
loop.run()

if phase != 3 or not session_path:
    print("ERR:portal_session_failed", flush=True)
    sys.exit(1)

print("READY", flush=True)


def send_keycombo(key_code: int) -> None:
    """Press Ctrl+<key>, release."""
    rd.NotifyKeyboardKeycode(session_path, {}, dbus.Int32(KEY_LEFTCTRL), dbus.UInt32(1))
    time.sleep(0.02)
    rd.NotifyKeyboardKeycode(session_path, {}, dbus.Int32(key_code), dbus.UInt32(1))
    time.sleep(0.04)
    rd.NotifyKeyboardKeycode(session_path, {}, dbus.Int32(key_code), dbus.UInt32(0))
    time.sleep(0.02)
    rd.NotifyKeyboardKeycode(session_path, {}, dbus.Int32(KEY_LEFTCTRL), dbus.UInt32(0))


def _atspi_get_focused() -> tuple[str, int, str] | None:
    """Return (app_name, pid, window_title) of the focused window via AT-SPI."""
    if not _ATSPI_AVAILABLE:
        return None
    try:
        desktop = Atspi.get_desktop(0)
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if not app:
                continue
            for j in range(app.get_child_count()):
                win = app.get_child_at_index(j)
                if not win:
                    continue
                states = win.get_state_set()
                if states and states.contains(Atspi.StateType.ACTIVE):
                    return (app.get_name() or "", win.get_process_id(), win.get_name() or "")
    except Exception:
        pass
    return None


def _find_desktop_file_for_pid(pid: int) -> str | None:
    """Find .desktop file for a process by reading /proc/PID/cmdline."""
    import glob
    import os
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmdline = f.read().decode("utf-8", errors="replace").split("\0")
        exe = os.path.basename(cmdline[0]) if cmdline else ""
        if not exe:
            return None
        # Search common desktop file locations
        dirs = [
            "/usr/share/applications",
            "/var/lib/snapd/desktop/applications",
            os.path.expanduser("~/.local/share/applications"),
            "/usr/local/share/applications",
        ]
        for d in dirs:
            for df in glob.glob(os.path.join(d, "*.desktop")):
                try:
                    with open(df) as fh:
                        content = fh.read()
                    for line in content.split("\n"):
                        if line.startswith("Exec="):
                            exec_cmd = os.path.basename(line.split("=", 1)[1].split()[0])
                            if exec_cmd == exe:
                                return os.path.basename(df)
                            break
                except Exception:
                    continue
    except Exception:
        pass
    return None


def _activate_by_pid(pid: int) -> bool:
    """Try to activate/focus a window by its PID."""
    # 1. Try D-Bus org.freedesktop.Application.Activate (works for GTK/native apps)
    try:
        proc = subprocess.run(
            ["busctl", "--user", "list", "--no-pager", "--no-legend"],
            capture_output=True, text=True, timeout=2,
        )
        if proc.returncode == 0:
            candidates = []
            for line in proc.stdout.strip().split("\n"):
                parts = line.split()
                if len(parts) >= 3:
                    name, svc_pid = parts[0], parts[2]
                    if svc_pid.isdigit() and int(svc_pid) == pid and not name.startswith(":"):
                        candidates.append(name)
            for name in candidates:
                obj_path = "/" + name.replace(".", "/")
                try:
                    result = subprocess.run(
                        ["gdbus", "call", "--session",
                         "--dest", name,
                         "--object-path", obj_path,
                         "--method", "org.freedesktop.Application.Activate", "[]"],
                        capture_output=True, text=True, timeout=2,
                    )
                    if result.returncode == 0:
                        return True
                except Exception:
                    continue
    except Exception:
        pass

    # 2. Try gtk-launch with .desktop file (works for most GUI apps)
    desktop_file = _find_desktop_file_for_pid(pid)
    if desktop_file:
        try:
            result = subprocess.run(
                ["gtk-launch", desktop_file],
                capture_output=True, text=True, timeout=3,
            )
            if result.returncode == 0:
                return True
        except Exception:
            pass

    return False


for line in sys.stdin:
    cmd = line.strip()
    if not cmd:
        continue
    if cmd == "QUIT":
        print("OK", flush=True)
        break
    elif cmd == "PASTE":
        try:
            send_keycombo(KEY_V)
            print("OK", flush=True)
        except Exception as exc:
            print(f"ERR:{exc}", flush=True)
    elif cmd == "COPY":
        try:
            send_keycombo(KEY_C)
            print("OK", flush=True)
        except Exception as exc:
            print(f"ERR:{exc}", flush=True)
    elif cmd.startswith("CLIPBOARD:"):
        try:
            b64_data = cmd[len("CLIPBOARD:"):]
            text = base64.b64decode(b64_data).decode("utf-8")
            # wl-copy forks a daemon to serve the clipboard — use Popen
            # and don't wait for it (the daemon stays alive until next copy).
            # Pass text via stdin to avoid command-line length limits.
            proc = subprocess.Popen(
                ["wl-copy"],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            proc.stdin.write(text.encode("utf-8"))
            proc.stdin.close()
            # Wait briefly for the parent to fork and exit
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass  # daemon child still running — that's OK
            print("OK", flush=True)
        except Exception as exc:
            print(f"ERR:{exc}", flush=True)
    elif cmd == "CLIPBOARD_GET":
        try:
            proc = subprocess.run(
                ["wl-paste", "--no-newline"],
                capture_output=True, timeout=2,
            )
            if proc.returncode == 0:
                b64 = base64.b64encode(proc.stdout).decode("ascii")
                print(f"OK:{b64}", flush=True)
            else:
                print("ERR:wl-paste failed", flush=True)
        except subprocess.TimeoutExpired:
            print("ERR:wl-paste timeout", flush=True)
        except Exception as exc:
            print(f"ERR:{exc}", flush=True)
    elif cmd == "GETFOCUSED":
        try:
            result = _atspi_get_focused()
            if result:
                app_name, pid, title = result
                # Encode title in base64 to avoid delimiter issues
                b64_title = base64.b64encode(title.encode("utf-8")).decode("ascii")
                print(f"OK:{app_name}:{pid}:{b64_title}", flush=True)
            else:
                print("ERR:no_focused_window", flush=True)
        except Exception as exc:
            print(f"ERR:{exc}", flush=True)
    elif cmd.startswith("ACTIVATE:"):
        try:
            pid = int(cmd[len("ACTIVATE:"):])
            if _activate_by_pid(pid):
                print("OK", flush=True)
            else:
                print("ERR:activation_failed", flush=True)
        except Exception as exc:
            print(f"ERR:{exc}", flush=True)
    else:
        print(f"ERR:unknown_command:{cmd}", flush=True)
