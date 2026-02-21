# Action Middleware — OS-level background assistant MVP
#
# Run with: sudo -E python main.py
#   -E preserves DISPLAY, WAYLAND_DISPLAY, DBUS_SESSION_BUS_ADDRESS
#
# Dependencies: keyboard, wl-clipboard (Wayland), xclip (X11), libnotify-bin

import os
import sys
import time
import threading
import subprocess
import platform
import keyboard
import shutil
from datetime import datetime

if platform.system() != "Linux":
    import pyperclip
    from plyer import notification

# ============================================================
# Constants
# ============================================================

HOTKEY: str = "ctrl+alt+x"
UNDO_HOTKEY: str = "ctrl+alt+z"
CLIPBOARD_DELAY: float = 0.3
APP_NAME: str = "Action Middleware"

_SESSION_TYPE: str = os.environ.get("XDG_SESSION_TYPE", "x11")
_IS_WAYLAND: bool = _SESSION_TYPE == "wayland"
_SUDO_USER: str = os.environ.get("SUDO_USER", "")
_DISPLAY: str = os.environ.get("DISPLAY", ":0")
_WAYLAND_DISPLAY: str = os.environ.get("WAYLAND_DISPLAY", "")
_DBUS_SESSION: str = os.environ.get("DBUS_SESSION_BUS_ADDRESS", "")

_undo_stack: list[dict] = []
_undo_lock = threading.Lock()
_exit_event = threading.Event()


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
    def _print(cls, *args, **kwargs) -> None:
        with cls._print_lock:
            print(*args, **kwargs)
            sys.stdout.flush()

    @classmethod
    def box(cls, title: str, lines: list[str], color: str = "") -> None:
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
        w = cls._width() - 2
        inner = w - 2
        c = cls.MAGENTA

        logo = [
            "  ▄▀█ █▀▀ ▀█▀ █ █▀█ █▄░█",
            "  █▀█ █▄▄ ░█░ █ █▄█ █░▀█",
            "",
            "  █▀▄▀█ █ █▀▄ █▀▄ █░░ █▀▀ █░█░█ ▄▀█ █▀█ █▀▀",
            "  █░▀░█ █ █▄▀ █▄▀ █▄▄ ██▄ ▀▄▀▄▀ █▀█ █▀▄ ██▄",
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
    def status(cls, label: str, value: str, color: str = "") -> None:
        c = color or cls.WHITE
        cls._print(f"  {cls._timestamp()}  {c}{cls.BOLD}{label}{cls.RESET} {cls.DIM}{value}{cls.RESET}")

    @classmethod
    def success(cls, message: str) -> None:
        cls._print(f"  {cls._timestamp()}  {cls.GREEN}✓{cls.RESET} {message}")

    @classmethod
    def warn(cls, message: str) -> None:
        cls._print(f"  {cls._timestamp()}  {cls.YELLOW}⚠{cls.RESET} {message}")

    @classmethod
    def error(cls, message: str) -> None:
        cls._print(f"  {cls._timestamp()}  {cls.RED}✗{cls.RESET} {message}")

    @classmethod
    def action(cls, icon: str, label: str, detail: str) -> None:
        cls._print(f"  {cls._timestamp()}  {cls.CYAN}{icon}{cls.RESET} {cls.BOLD}{label}{cls.RESET} {cls.DIM}→{cls.RESET} {detail}")

    @classmethod
    def separator(cls) -> None:
        w = cls._width() - 4
        cls._print(f"  {cls.DIM}{cls.HORIZ * w}{cls.RESET}")

    @classmethod
    def keybind_table(cls) -> None:
        rows = [
            (HOTKEY.upper(), "Intercept selected text", cls.CYAN),
            (UNDO_HOTKEY.upper(), "Undo last replacement", cls.YELLOW),
            ("CTRL+C", "Exit application", cls.RED),
        ]
        lines = []
        for key, desc, color in rows:
            lines.append(f"  {color}{cls.BOLD}{key:<16}{cls.RESET} {cls.DIM}{desc}{cls.RESET}")
        cls.box("Keybindings", lines, cls.BLUE)


# ============================================================
# Subprocess — Run as Original User
# ============================================================

def _run_as_user(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
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


# ============================================================
# Clipboard Helpers
# ============================================================

def clipboard_copy(text: str) -> None:
    if platform.system() == "Linux":
        if _IS_WAYLAND:
            cmd = ["wl-copy", "--", text]
        else:
            cmd = ["xclip", "-selection", "clipboard"]
        if _IS_WAYLAND:
            proc = _run_as_user(cmd, capture_output=True)
        else:
            proc = _run_as_user(cmd, input=text.encode(), capture_output=True)
        if proc.returncode != 0:
            TUI.error(f"Clipboard copy failed: {proc.stderr.decode().strip()}")
    else:
        pyperclip.copy(text)


def clipboard_paste() -> str:
    if platform.system() == "Linux":
        try:
            if _IS_WAYLAND:
                cmd = ["wl-paste", "--no-newline"]
            else:
                cmd = ["xclip", "-selection", "clipboard", "-o"]
            proc = _run_as_user(cmd, capture_output=True, text=True)
            return proc.stdout if proc.returncode == 0 else ""
        except Exception as exc:
            TUI.error(f"Clipboard paste failed: {exc}")
            return ""
    else:
        return pyperclip.paste()


# ============================================================
# Primary Selection (Wayland)
# ============================================================

def _get_primary_selection() -> str:
    try:
        proc = _run_as_user(
            ["wl-paste", "--primary", "--no-newline"],
            capture_output=True,
            text=True,
        )
        return proc.stdout if proc.returncode == 0 else ""
    except Exception as exc:
        TUI.error(f"Primary selection read failed: {exc}")
        return ""


# ============================================================
# Auto-Replace — clipboard + uinput Ctrl+V
# ============================================================

def _replace_selection(new_text: str) -> None:
    """Replace currently selected text.

    Uses wl-copy/xclip to set clipboard, then keyboard lib's /dev/uinput
    to simulate Ctrl+V. uinput is kernel-level and works on Wayland.
    """
    clipboard_copy(new_text)
    time.sleep(CLIPBOARD_DELAY)
    keyboard.send("ctrl+v")
    TUI.success("Text replaced in-place")


# ============================================================
# Notification Helper
# ============================================================

def notify(title: str, message: str) -> None:
    try:
        if platform.system() == "Linux":
            _run_as_user(
                ["notify-send", "-t", "5000", title, message],
                capture_output=True,
                timeout=3,
            )
        else:
            notification.notify(
                title=title, message=message,
                timeout=5, app_name=APP_NAME,
            )
    except subprocess.TimeoutExpired:
        TUI.warn("notify-send timed out")
    except FileNotFoundError:
        TUI.warn("notify-send not found — install libnotify-bin")
    except Exception as exc:
        TUI.error(f"Notification failed: {exc}")


# ============================================================
# Undo System
# ============================================================

def _push_undo(original: str, replacement: str) -> None:
    with _undo_lock:
        _undo_stack.append({"original": original, "replacement": replacement})
        if len(_undo_stack) > 20:
            _undo_stack.pop(0)


def _do_undo() -> None:
    """Actual undo work — runs in its own thread.

    Uses native Ctrl+Z — since the replacement was done via Ctrl+V paste,
    the app's native undo reverses it perfectly. No need to re-select text.
    """
    try:
        # Wait for user to release Ctrl+Alt+Z physically
        time.sleep(0.5)

        with _undo_lock:
            if not _undo_stack:
                TUI.warn("Nothing to undo")
                notify(APP_NAME, "Nothing to undo.")
                return
            entry = _undo_stack.pop()

        TUI.separator()
        TUI.action("↩", "UNDO", "Sending native Ctrl+Z")

        # Native undo — reverses the Ctrl+V paste that did the replacement
        keyboard.send("ctrl+z")

        truncated = entry["original"][:50] + ("..." if len(entry["original"]) > 50 else "")
        TUI.success(f"Undone — original was: \"{truncated}\"")
        notify("Undo", f"Reverted last replacement")

    except Exception as exc:
        TUI.error(f"Undo error: {exc}")


def on_undo_triggered() -> None:
    """Callback — MUST return immediately. Spawns thread for actual work."""
    threading.Thread(target=_do_undo, daemon=True).start()


# ============================================================
# Handlers
# ============================================================

def handle_translate(text: str, full_text: str) -> None:
    translations: dict[str, str] = {
        # Frustration
        "fix this garbage": "Please review the code for potential improvements.",
        "this is broken": "I've identified an issue that needs attention.",
        "this sucks": "There may be room for improvement here.",
        "what a mess": "This could benefit from some restructuring.",
        "this is trash": "I think we should consider a different approach.",
        "terrible code": "The implementation could use some refinement.",
        # Blame / confrontation
        "who wrote this": "Could we discuss the approach taken here?",
        "that's wrong": "I have a different perspective on this.",
        "are you serious": "Could you help me understand the reasoning behind this?",
        "are you stupid": "I think there might be a misunderstanding here.",
        "you don't know what you're doing": "Perhaps we could pair on this together?",
        "this is your fault": "Let's focus on finding a solution together.",
        # Refusal / dismissal
        "do it yourself": "I'd appreciate your help with this task.",
        "not my problem": "I understand — let me find the right person to help.",
        "i don't care": "I'll defer to the team's judgment on this.",
        "whatever": "I'm open to any approach the team prefers.",
        "figure it out": "Happy to brainstorm solutions together.",
        # Deadline / pressure
        "this is taking forever": "Could we discuss the timeline and priorities?",
        "hurry up": "I'd like to understand the timeline expectations.",
        "why isn't this done yet": "Could we review the current blockers together?",
        "just ship it": "Let's discuss what an acceptable MVP looks like.",
        # Meetings / process
        "this meeting is pointless": "Could we clarify the meeting objectives?",
        "stop wasting my time": "I want to make sure we're using everyone's time effectively.",
        "nobody asked for this": "Could we revisit the requirements and priorities?",
    }

    normalised: str = text.strip().lower()
    result: str = translations.get(normalised, f"Politely: {text.strip()}")

    _push_undo(full_text, result)
    _replace_selection(result)

    TUI.action("📝", "TRANSLATE", f"\"{normalised}\" → \"{result}\"")
    notify("Corporate Translator", f"Replaced: \"{result[:80]}\"")


def handle_command(text: str, full_text: str) -> None:
    command: str = text.strip()

    dangerous_patterns: list[str] = [
        "rm -rf /", "format", "mkfs", ":(){", "dd if=",
    ]
    for pattern in dangerous_patterns:
        if pattern in command.lower():
            TUI.error(f"BLOCKED — dangerous pattern: '{pattern}'")
            notify("Security Block", f"Command blocked — contains '{pattern}'")
            return

    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["cmd", "/c", command],
                capture_output=True, text=True, timeout=30,
            )
        else:
            result = subprocess.run(
                command, shell=True,
                capture_output=True, text=True, timeout=30,
            )

        TUI.action("⚡", "COMMAND", f"`{command}`")

        if result.returncode == 0:
            TUI.success("Exit code 0")
            if result.stdout.strip():
                for line in result.stdout.strip().split("\n")[:5]:
                    TUI._print(f"    {TUI.DIM}{line}{TUI.RESET}")
        else:
            TUI.warn(f"Exit code {result.returncode}")
            if result.stderr.strip():
                for line in result.stderr.strip().split("\n")[:5]:
                    TUI._print(f"    {TUI.RED}{line}{TUI.RESET}")

        output = result.stdout.strip() or result.stderr.strip() or "(no output)"
        notify("Terminal Magic", f"Done (exit {result.returncode}): {output[:100]}")

    except subprocess.TimeoutExpired:
        TUI.error(f"Command timed out: '{command}'")
        notify("Terminal Magic", "Command timed out after 30 seconds.")
    except Exception as exc:
        TUI.error(f"Command failed: {exc}")
        notify("Terminal Magic", f"Command failed: {exc}")


def handle_test(text: str, full_text: str) -> None:
    """Test prefix — verifies the full pipeline: capture → process → replace.

    Wraps the input in markers so you can visually confirm the replacement
    worked. Also reports timing and environment info.
    """
    content = text.strip()
    result = f"[TEST OK] \"{content}\" | session={_SESSION_TYPE} | wayland={_IS_WAYLAND}"

    _push_undo(full_text, result)
    _replace_selection(result)

    TUI.action("🧪", "TEST", f"Input: \"{content}\"")
    TUI.success(f"Output: \"{result}\"")
    notify("Test", f"Pipeline OK: \"{content[:60]}\"")


# ============================================================
# Router
# ============================================================

def route(text: str) -> None:
    if text.startswith("TR:"):
        handle_translate(text[3:], text)
    elif text.startswith("CMD:"):
        handle_command(text[4:], text)
    elif text.startswith("TEST:"):
        handle_test(text[5:], text)
    else:
        truncated = text[:40] + ("..." if len(text) > 40 else "")
        TUI.warn(f"Unknown prefix: \"{truncated}\"")
        notify(APP_NAME, "Unknown prefix. Use TR:, CMD:, or TEST:")


# ============================================================
# Interceptor
# ============================================================

def _do_intercept() -> None:
    """Actual intercept work — runs in its own thread."""
    try:
        # Wait for user to physically release Ctrl+Alt+X
        time.sleep(0.5)

        TUI.separator()
        TUI.status("⌨", "Hotkey triggered — reading selection...", TUI.CYAN)

        # Get selected text
        if _IS_WAYLAND:
            text: str = _get_primary_selection()
        else:
            old_clipboard: str = clipboard_paste()
            keyboard.send("ctrl+c")
            time.sleep(CLIPBOARD_DELAY)
            text: str = clipboard_paste()
            if text == old_clipboard:
                TUI.warn("Clipboard unchanged — no text copied")
                notify(APP_NAME, "No text selected (clipboard unchanged).")
                return

        if not text or not text.strip():
            TUI.warn("No text captured from selection")
            notify(APP_NAME, "No text selected.")
            return

        truncated = text[:60] + ("..." if len(text) > 60 else "")
        TUI.action("📋", "CAPTURED", f"\"{truncated}\"")
        route(text)

    except Exception as exc:
        TUI.error(f"Interceptor error: {exc}")
        notify(APP_NAME, f"Error: {exc}")


def on_hotkey_triggered() -> None:
    """Callback — MUST return immediately. Spawns thread for actual work."""
    threading.Thread(target=_do_intercept, daemon=True).start()


# ============================================================
# Main Entry Point
# ============================================================

def main() -> None:
    # Clear screen + scrollback, move cursor to top-left
    print("\033[2J\033[3J\033[H", end="", flush=True)

    TUI.banner()

    TUI.box("Environment", [
        f"  {TUI.BOLD}Session{TUI.RESET}    {_SESSION_TYPE}",
        f"  {TUI.BOLD}Display{TUI.RESET}    {_DISPLAY}",
        f"  {TUI.BOLD}Wayland{TUI.RESET}    {'Yes' if _IS_WAYLAND else 'No'}",
        f"  {TUI.BOLD}User{TUI.RESET}       {_SUDO_USER or os.environ.get('USER', '?')}",
        f"  {TUI.BOLD}UID{TUI.RESET}        {os.geteuid()}",
    ], TUI.GREEN)

    print()
    TUI.keybind_table()
    print()

    keyboard.add_hotkey(HOTKEY, on_hotkey_triggered)
    keyboard.add_hotkey(UNDO_HOTKEY, on_undo_triggered)

    notify(
        "Action Middleware Active",
        f"{HOTKEY.upper()} to intercept | {UNDO_HOTKEY.upper()} to undo | Ctrl+C to exit",
    )

    TUI.separator()
    TUI.success("Listening for hotkeys...")
    TUI.status("", f"{TUI.DIM}Select text and press {HOTKEY.upper()} to intercept{TUI.RESET}")
    print()

    try:
        while not _exit_event.is_set():
            _exit_event.wait(timeout=1.0)
    except KeyboardInterrupt:
        pass

    print()
    TUI.separator()
    TUI.status("👋", "Shutting down. Goodbye!", TUI.MAGENTA)
    notify(APP_NAME, "Shutting down. Goodbye!")


if __name__ == "__main__":
    main()
