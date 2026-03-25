#!/usr/bin/env python3
"""Portal paste helper — runs as the real user, receives commands via stdin.

Started by main.py as: sudo -u $USER python3 -u paste_helper.py
Main app sends commands via stdin:
  PASTE\n              → send Ctrl+V via portal
  COPY\n               → send Ctrl+C via portal
  CLIPBOARD:<base64>\n → set clipboard via wl-copy (runs as user, no sudo issues)
  CLIPBOARD_GET\n      → get clipboard via wl-paste
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
    else:
        print(f"ERR:unknown_command:{cmd}", flush=True)
