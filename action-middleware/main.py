# ActionFlow — OS-level background assistant
#
# Run with:
#   Linux:  sudo -E python main.py
#           (-E preserves DISPLAY, WAYLAND_DISPLAY, DBUS_SESSION_BUS_ADDRESS)
#   macOS:  python main.py
#           (grant Accessibility + Input Monitoring to your terminal app)
#
# Config: edit config.yaml to add commands, set hotkeys, configure LLM

from __future__ import annotations

from actionflow import __version__

import argparse
import json
import os
import platform
import queue
import re as _re
import select
import shlex
import subprocess
import sys
import termios
import threading
import time
import tty
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

_IS_MAC: bool = sys.platform == "darwin"
_IS_LINUX: bool = sys.platform.startswith("linux")
_NATIVE_UI: bool = False  # macOS AppKit palette (mac_ui.py) instead of Tk

if not (_IS_MAC or _IS_LINUX):
    sys.exit("ActionFlow supports macOS and Linux.")

linux = mac = None
if _IS_LINUX:
    from actionflow import platform_linux as linux
elif _IS_MAC:
    from actionflow import platform_mac as mac
    try:
        from actionflow import mac_ui
        _NATIVE_UI = True
    except ImportError:  # PyObjC missing → Tk popup (if usable)
        mac_ui = None

if _IS_MAC:
    tk_ui = None  # macOS uses the AppKit palette
    _TKINTER_AVAILABLE = False
else:
    from actionflow import tk_ui
    _TKINTER_AVAILABLE = tk_ui.TK_AVAILABLE

_POPUP_AVAILABLE: bool = _NATIVE_UI or _TKINTER_AVAILABLE

from actionflow.tui import TUI  # noqa: E402
from actionflow import history, llm, palette, prompts, service, setup_wizard, textops  # noqa: E402
from actionflow.analysis import (  # noqa: E402
    AppContext, analyze_text, get_smart_suggestions, PatternLearner,
)
from actionflow.config import (  # noqa: E402
    CONFIG, load_config, ensure_user_config, APP_DIR as _SCRIPT_DIR, CONFIG_PATH as _CONFIG_PATH,
)

# ============================================================
# Constants (from config)
# ============================================================

HOTKEY: str = CONFIG["hotkeys"]["intercept"]
UNDO_HOTKEY: str = CONFIG["hotkeys"]["undo"]
APP_NAME: str = "ActionFlow"

_IS_WAYLAND: bool = bool(linux and linux.IS_WAYLAND)
_SESSION_TYPE: str = "macos" if _IS_MAC else linux.SESSION_TYPE
_SUDO_USER: str = linux.SUDO_USER if linux else ""
# Run a command as the real user (Linux runs as root via sudo)
_run_as_user = linux.run_as_user if linux else subprocess.run

_undo_stack: list[dict] = []
_undo_lock = threading.Lock()
_exit_event = threading.Event()

_current_notify_level: str = "always"  # Set per-dispatch from cmd config

_usage_counts: dict[str, int] = {}
_usage_lock = threading.Lock()


_start_time: float = time.time()

_last_command: dict | None = None  # For REPEAT: stores {"name": ..., "config": ...}
_clipboard_stack: list[str] = []   # For STACK/POP
_CLIPS_PATH = Path.home() / ".actionflow_clips.json"

_popup_queue: queue.Queue = queue.Queue()  # Hotkey thread → main thread for popup
_popup_trigger: str = "prefix"  # Set per-dispatch: "prefix" or "popup"
_current_source_window: str | None = None  # Window ID currently targeted for paste/replacement

_current_app_context = None   # AppContext instance, set per-intercept
_current_text_analysis = None  # TextAnalysis instance, set per-intercept
_pattern_learner = None       # PatternLearner instance, initialized in main()

_silent_mode: bool = False
_silent_mode_lock = threading.Lock()


_tray_icon = None  # pystray icon, set in _start_tray()
_mac_menubar = None  # platform_mac.StatusBar, set in _start_mac_menubar()
_mac_menubar_silent_item = None


_main_thread_calls: queue.Queue = queue.Queue()  # drained by the main loop


def _run_on_main(fn) -> None:
    """Run fn on the main thread (AppKit/Tk objects must not be touched elsewhere)."""
    if threading.current_thread() is threading.main_thread():
        fn()
    else:
        _main_thread_calls.put(fn)


def _update_tray_color(color: str) -> None:
    """Update tray icon color (Linux) / menu bar icon (macOS). No-op if absent."""
    if _mac_menubar is not None:
        def _update_menubar() -> None:
            _mac_menubar.set_dimmed(color == "grey")
            _mac_menubar.set_checked(_mac_menubar_silent_item, color == "grey")
        _run_on_main(_update_menubar)
    if _tray_icon is None:
        return
    try:
        _tray_icon.icon = _create_tray_icon_image(color)
    except Exception:
        pass


# ============================================================
# Active window — detect / remember / refocus
# ============================================================

def detect_active_window() -> AppContext:
    """Classify the focused app (terminal/browser/IDE/chat/docs)."""
    title = display_name = ""
    try:
        if _IS_MAC:
            front = mac.frontmost_app()
            if front:
                name, _pid, bundle_id = front
                display_name, title = name, f"{name} {bundle_id}".lower()
        else:
            title = linux.active_window_title()
    except Exception:
        pass
    for ctx_type, patterns in AppContext.APP_PATTERNS.items():
        for pattern in patterns:
            if pattern in title:
                return AppContext(ctx_type, title, pattern, display_name)
    return AppContext(AppContext.UNKNOWN, title, "", display_name)


def _get_active_window_id() -> str | None:
    """Opaque id of the focused window, for _focus_window() later."""
    try:
        if _IS_MAC:
            front = mac.frontmost_app()
            return f"mac:{front[1]}" if front else None
        return linux.active_window_id()
    except Exception:
        return None


def _focus_window(window_id: str | None) -> bool:
    if not window_id:
        return False
    try:
        if window_id.startswith("mac:"):
            return mac.activate_app(int(window_id[4:]))
        return bool(linux) and linux.focus_window(window_id)
    except Exception as exc:
        TUI.warn(f"Focus restore failed: {exc}")
        return False


# ============================================================
# Config Hot-Reload
# ============================================================

def _reload_config() -> None:
    """Reload config.yaml and update CONFIG in place (LLM settings are kept —
    they are only applied at startup)."""
    try:
        new_cfg = load_config()
        for key, value in new_cfg.items():
            if key not in ("llm", "image_api", "hotkeys"):
                CONFIG[key] = value
        _register_personal_commands()
        _refresh_command_security()
        # Initialize usage counters for any new commands
        for cmd_name in CONFIG["commands"]:
            if cmd_name not in _usage_counts:
                _usage_counts[cmd_name] = 0
        cmd_count = len(CONFIG["commands"])
        TUI.micro_log(f"{TUI.GREEN}✓{TUI.RESET} Config reloaded — {cmd_count} commands loaded")
    except Exception as exc:
        TUI.warn(f"Config reload failed: {exc}")


def _start_config_watcher() -> None:
    """Watch config.yaml for changes using watchdog."""
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
        observer.schedule(ConfigHandler(), str(_SCRIPT_DIR), recursive=False)
        observer.daemon = True
        observer.start()
    except ImportError:
        TUI.warn("watchdog not installed — config hot-reload disabled")
    except Exception as exc:
        TUI.warn(f"Config watcher failed to start: {exc}")


# ============================================================
# Image API settings
# ============================================================

_image_api_provider = ""
_image_api_key = ""
_image_api_model = ""


def _init_image_api() -> None:
    """Image provider/key from config + env var + keychain."""
    global _image_api_provider, _image_api_key, _image_api_model
    image_cfg = CONFIG.get("image_api", {})
    _image_api_provider = (image_cfg.get("provider") or "").strip().lower()
    _image_api_key = llm.resolve_api_key("image", _image_api_provider, image_cfg.get("api_key", ""))
    model = (image_cfg.get("model") or "").strip()
    _image_api_model = "" if model == "seedream" else model  # old default, no longer served


# ============================================================
# TUI panels (app state → terminal)
# ============================================================

def _tui_header_line() -> None:
    """Compact single-line header shown after banner collapses."""
    elapsed = int(time.time() - _start_time)
    h, rem = divmod(elapsed, 3600)
    m, s = divmod(rem, 60)
    uptime = f"{h}:{m:02d}:{s:02d}"

    if llm.MODE == "live":
        mode_str = f"{TUI.GREEN}live{TUI.RESET} {TUI.DIM}· {llm.provider}/{llm.model}{TUI.RESET}"
    else:
        mode_str = f"{TUI.YELLOW}mock{TUI.RESET}"

    cmd_count = len(CONFIG.get("commands", {}))
    line = (
        f"  {TUI.MAGENTA}{TUI.BOLD}▶ ACTIONFLOW{TUI.RESET}  "
        f"{TUI.DIM}|{TUI.RESET}  {mode_str}  "
        f"{TUI.DIM}|{TUI.RESET}  {TUI.DIM}{cmd_count} commands{TUI.RESET}  "
        f"{TUI.DIM}|{TUI.RESET}  {TUI.DIM}uptime: {uptime}{TUI.RESET}"
    )
    TUI._print(line)


def _hotkey_label(spec: str) -> str:
    """"ctrl+alt+x" → "⌃⌥X  (ctrl+alt+x)" on macOS, "CTRL+ALT+X" elsewhere."""
    return f"{mac.format_hotkey(spec)}  ({spec})" if _IS_MAC else spec.upper()


def _tui_keybind_table() -> None:
    with _undo_lock:
        undo_count = len(_undo_stack)

    if undo_count > 0:
        undo_suffix = f"  {TUI.GREEN}(×{undo_count} available){TUI.RESET}"
        undo_color = TUI.YELLOW
    else:
        undo_suffix = f"  {TUI.DIM}· empty{TUI.RESET}"
        undo_color = TUI.DIM

    silent = CONFIG.get("hotkeys", {}).get("silent_toggle", "ctrl+alt+s")
    rows = [
        (_hotkey_label(HOTKEY), "Process selected text", TUI.CYAN, ""),
        (_hotkey_label(UNDO_HOTKEY), "Undo last replacement", undo_color, undo_suffix),
        (_hotkey_label(silent), "Toggle notifications", TUI.DIM, ""),
        ("CTRL+C", "Exit (in this terminal)", TUI.RED, ""),
    ]
    lines = []
    for key, desc, color, suffix in rows:
        lines.append(f"  {color}{TUI.BOLD}{key:<22}{TUI.RESET} {TUI.DIM}{desc}{TUI.RESET}{suffix}")
    TUI.box("Keybindings", lines, TUI.CYAN)


def _tui_commands_table() -> None:
    commands = CONFIG.get("commands", {})
    lines = []
    for name, cmd in commands.items():
        prefixes = ", ".join(cmd.get("prefixes", []))
        keywords = ", ".join(cmd.get("keywords", [])[:3])
        is_llm_cmd = cmd.get("llm_required", False)
        dimmed = is_llm_cmd and llm.MODE == "mock"

        if is_llm_cmd:
            badge = f" {TUI.MAGENTA}{TUI.BOLD}[LLM]{TUI.RESET}"
            if dimmed:
                badge += f" {TUI.YELLOW}[MOCK]{TUI.RESET}"
        else:
            badge = f" {TUI.CYAN}{TUI.BOLD}[FAST]{TUI.RESET}"

        count = _usage_counts.get(name, 0)
        counter = f" {TUI.DIM}×{count}{TUI.RESET}"

        if dimmed:
            lines.append(
                f"  {TUI.DIM}{name:<12} "
                f"{prefixes:<20} "
                f"{keywords}{TUI.RESET}"
                f"{badge}{counter}"
            )
        else:
            lines.append(
                f"  {TUI.CYAN}{TUI.BOLD}{name:<12}{TUI.RESET} "
                f"{TUI.DIM}{prefixes:<20}{TUI.RESET} "
                f"{TUI.DIM}{keywords}{TUI.RESET}"
                f"{badge}{counter}"
            )
    TUI.box("Commands", lines, TUI.CYAN)


def _tui_llm_status_box() -> None:
    if llm.MODE == "live":
        lines = [
            f"  {TUI.GREEN}{TUI.BOLD}LIVE{TUI.RESET}    {TUI.DIM}Provider: {llm.provider}{TUI.RESET}",
            f"          {TUI.DIM}Model: {llm.model}{TUI.RESET}",
        ]
        if llm.fallback_ready:
            lines.append(
                f"          {TUI.DIM}Fallback: {llm.fallback_provider} / {llm.fallback_model}{TUI.RESET}"
            )
        TUI.box("LLM", lines, TUI.GREEN)
    else:
        lines = [
            f"  {TUI.YELLOW}{TUI.BOLD}NO LLM{TUI.RESET}   {TUI.DIM}AI commands are off; built-in ones work{TUI.RESET}",
            f"  {TUI.DIM}Set one up: restart and pick a provider, or  main.py --set-key groq{TUI.RESET}",
        ]
        TUI.box("LLM", lines, TUI.YELLOW)


def _tui_activity_placeholder() -> None:
    """Show empty activity feed at startup."""
    TUI.box("Activity", [
        f"  {TUI.DIM}No activity yet. Select text and press {_hotkey_label(HOTKEY)}{TUI.RESET}",
    ], TUI.CYAN)


def _palette_context_line(text: str) -> str:
    preview = " ".join(text.split())
    preview = preview[:70] + ("…" if len(preview) > 70 else "")
    parts = [f"“{preview}”", f"{len(text)} chars"]
    ta, ctx = _current_text_analysis, _current_app_context
    if ta and ta.language:
        parts.append(ta.language.upper())
    if ta and ta.is_code:
        parts.append(f"code · {ta.code_language}" if ta.code_language else "code")
    if ctx and (ctx.display_name or ctx.context_type != "unknown"):
        parts.append(ctx.display_name or ctx.context_type)
    return "  ·  ".join(parts)


def _palette_status_line() -> str:
    if llm.MODE == "live":
        return f"{llm.provider} · {llm.model}"
    return "Mock mode — no LLM configured"


def _commit_generated(cmd_name: str, cmd_config: dict, text: str, result: str,
                      seconds: float) -> None:
    """Paste an accepted palette result and record it like dispatch() does."""
    global _last_command
    with _usage_lock:
        _usage_counts[cmd_name] = _usage_counts.get(cmd_name, 0) + 1
    _last_command = {"name": cmd_name, "config": cmd_config}
    _push_undo(text, result)
    _replace_selection(result, announce=False)
    TUI.activity_entry(cmd_name, text, result, seconds, is_llm=True, trigger="popup")
    _log_history(cmd_name, text, result, int(seconds * 1000),
                 app_context=_current_app_context.context_type if _current_app_context else "",
                 text_length=len(text),
                 text_language=_current_text_analysis.language if _current_text_analysis else "",
                 trigger="popup", is_llm=True)


def _handle_native_palette(text: str, source_window: str | None) -> None:
    global _popup_trigger, _current_source_window
    commands = CONFIG.get("commands", {})
    suggestions = None
    if _current_app_context and _current_text_analysis:
        pattern_scores = (_pattern_learner.get_scores(_current_app_context.context_type)
                          if _pattern_learner else {})
        suggestions = get_smart_suggestions(_current_app_context, _current_text_analysis,
                                            commands, pattern_scores=pattern_scores)

    controller = palette.PaletteController(text, commands, suggestions, prompt_for=_llm_prompt_for)
    palette_ui = mac_ui.CommandPalette(controller, context=_palette_context_line(text),
                                       status=_palette_status_line())
    outcome = palette_ui.run()
    if not outcome:
        TUI.micro_log("Command palette closed")
        return
    if outcome["kind"] == "copied":
        TUI.micro_log("Result copied to clipboard")
        return

    _popup_trigger = "popup"
    _current_source_window = source_window
    try:
        if outcome["kind"] == "replace":
            spec = palette_ui._stream_spec or {}
            cmd_name = spec.get("cmd_name", outcome["item"]["id"])
            TUI.status("🎯", f"Palette → {cmd_name}", TUI.GREEN)
            _commit_generated(cmd_name, spec.get("cmd_config", {}), text,
                              outcome["text"], outcome["seconds"])
        else:  # "run": instant built-in command
            name = outcome["item"]["id"]
            TUI.status("🎯", f"Palette → {name}", TUI.GREEN)
            dispatch(name, text, text, commands.get(name, {}))
    except Exception as exc:
        TUI.error(f"Palette action failed: {exc}")
        notify(APP_NAME, f"Failed: {exc}", is_error=True)
    finally:
        _current_source_window = None


def _handle_popup(text: str, source_window: str | None = None) -> None:
    """Show the command picker popup and dispatch the chosen command."""
    global _popup_trigger, _current_source_window

    if _NATIVE_UI:
        _handle_native_palette(text, source_window)
        return

    if not _TKINTER_AVAILABLE:
        TUI.warn("tkinter not available — cannot show popup")
        _popup_trigger = "prefix"
        _current_source_window = source_window
        try:
            route(text)
        finally:
            _current_source_window = None
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

    picker = tk_ui.CommandPicker(text, commands, suggestions=suggestions,
                           text_analysis=_current_text_analysis,
                           app_context=_current_app_context)
    result = picker.run()

    if result is None:
        TUI.micro_log(f"Command picker cancelled")
        # Refocus original app even on cancel
        if source_window:
            _focus_window(source_window)
        return

    cmd_name, cmd_config, payload = result
    is_llm = cmd_config.get("llm_required", False)

    # Mock mode notification for LLM commands
    if is_llm and llm.MODE == "mock":
        notify(APP_NAME, f"'{cmd_name}' needs an LLM — set llm.provider in config.yaml and restart")
        TUI.warn("LLM not configured — command not applied")
        if source_window:
            _focus_window(source_window)
        return

    _popup_trigger = "popup"
    TUI.status("\U0001f3af", f"Popup \u2192 {cmd_name}", TUI.GREEN)

    # Refocus the original app before processing
    if source_window:
        if _focus_window(source_window):
            TUI.micro_log("Refocused source window")
        else:
            TUI.warn("Direct source-window focus failed — will retry before paste")
    time.sleep(0.3)

    _current_source_window = source_window
    TUI.micro_log(f"Processing {cmd_name}...")
    try:
        dispatch(cmd_name, payload, text, cmd_config)
    except Exception as exc:
        TUI.error(f"Popup dispatch error: {exc}")
    finally:
        _current_source_window = None
        # Reset keyboard state after dispatch to ensure hotkeys keep working.
        time.sleep(0.05)
        _reset_keyboard_state()


# ============================================================
# Clipboard & keys (platform facades)
# ============================================================

def clipboard_copy(text: str) -> None:
    ok = mac.clipboard_set(text) if _IS_MAC else linux.clipboard_set(text)
    if not ok:
        TUI.error("Clipboard copy failed")


def clipboard_paste(timeout: float = 1.0) -> str:
    return mac.clipboard_get() if _IS_MAC else linux.clipboard_get(timeout)


def _reset_keyboard_state() -> None:
    """Don't inject keys while hotkey modifiers are still held / stuck."""
    if _IS_MAC:
        mac.wait_for_modifiers_released(timeout=0.5)
    else:
        linux.reset_keyboard()


def _send_paste_keys() -> None:
    """Paste the clipboard into the focused window (⌘V / Ctrl+V)."""
    if _IS_MAC:
        mac.wait_for_modifiers_released()
        if not mac.send_paste():
            TUI.warn("Cmd+V failed — grant Accessibility to your terminal app")
        time.sleep(0.08)
        return
    is_terminal = (_current_app_context is not None
                   and _current_app_context.context_type == AppContext.TERMINAL)
    linux.send_paste(is_terminal=is_terminal)


def _focus_by_alt_tab() -> bool:
    """Blind Alt+Tab — only where windows can't be activated directly (Linux)."""
    return False if _IS_MAC else linux.focus_by_alt_tab()


# ============================================================
# Result Popup — Display-only command output
# ============================================================

_result_queue: queue.Queue = queue.Queue()  # Worker thread → main thread for result popup

# Commands that show output in a popup instead of replacing text
_DISPLAY_ONLY_COMMANDS = frozenset([
    "count", "define", "wiki",
])


# ============================================================
# Auto-Replace
# ============================================================

_CLIPBOARD_SYNC_TIMEOUT = 0.25
_CLIPBOARD_SYNC_POLL = 0.01
_CLIPBOARD_RESTORE_DELAY = 2.0
_FOCUS_RETRY_COUNT = 3
_FOCUS_RETRY_DELAY = 0.06
_clipboard_restore_token = 0
_clipboard_restore_lock = threading.Lock()


def _wait_for_clipboard_sync(expected_text: str,
                             timeout: float = _CLIPBOARD_SYNC_TIMEOUT) -> bool:
    """Wait briefly until clipboard content matches expected text."""
    expected = expected_text.rstrip("\n")
    deadline = time.time() + timeout

    while time.time() < deadline:
        current = clipboard_paste(timeout=0.08).rstrip("\n")
        if current == expected:
            return True
        time.sleep(_CLIPBOARD_SYNC_POLL)
    return False


def _cancel_pending_clipboard_restore() -> None:
    """Invalidate any pending async clipboard restore task."""
    global _clipboard_restore_token
    with _clipboard_restore_lock:
        _clipboard_restore_token += 1


def _schedule_clipboard_restore(previous_clipboard: str) -> None:
    """Restore clipboard asynchronously so dispatch can finish immediately."""
    global _clipboard_restore_token
    with _clipboard_restore_lock:
        _clipboard_restore_token += 1
        token = _clipboard_restore_token

    def _worker() -> None:
        try:
            time.sleep(_CLIPBOARD_RESTORE_DELAY)
            with _clipboard_restore_lock:
                if token != _clipboard_restore_token:
                    return
            clipboard_copy(previous_clipboard)
            TUI.micro_log("Clipboard restored")
        except Exception as exc:
            TUI.warn(f"Clipboard restore failed: {exc}")

    threading.Thread(target=_worker, daemon=True).start()


def _refocus_source_window_for_paste() -> None:
    """Best-effort refocus of the original app before sending Ctrl+V."""
    if not _current_source_window:
        if _popup_trigger == "popup":
            _focus_by_alt_tab()
        return

    for attempt in range(_FOCUS_RETRY_COUNT):
        if _focus_window(_current_source_window):
            if attempt > 0:
                TUI.micro_log("Refocused source window for paste")
            time.sleep(0.06)
            return
        time.sleep(_FOCUS_RETRY_DELAY)

    TUI.warn("Could not refocus source window before paste")
    if _popup_trigger == "popup":
        _focus_by_alt_tab()


def _replace_selection(new_text: str, announce: bool = True) -> None:
    if _chain_suppress_paste:
        # Intermediate chain step — store result but don't paste
        TUI.success("Chain step complete (output passed to next step)")
        return

    previous_clipboard = clipboard_paste(timeout=0.2)

    # 1. Copy result to clipboard
    TUI.micro_log("Copying result to clipboard...")
    clipboard_copy(new_text)
    # Give clipboard time to register
    time.sleep(0.15)
    TUI.micro_log("Clipboard set")

    # 2. Refocus the source window before pasting
    TUI.micro_log(f"Source window: {_current_source_window or 'none (staying in current)'}")
    _refocus_source_window_for_paste()
    time.sleep(0.08)

    # 3. Paste into focused window
    TUI.micro_log("Sending paste keys...")
    _send_paste_keys()
    # Delay clipboard restore to ensure paste completes first
    _schedule_clipboard_restore(previous_clipboard)

    TUI.success("Text replaced in-place")
    TUI.micro_log("Paste sequence complete")
    if announce:
        truncated = new_text[:60] + ("..." if len(new_text) > 60 else "")
        notify(APP_NAME, f"Done: \"{truncated}\"")


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


def notify(title: str, message: str, is_error: bool = False) -> None:
    with _silent_mode_lock:
        if _silent_mode:
            return
    if not _should_notify(is_error=is_error):
        return
    try:
        if _IS_MAC:
            mac.notify(title, message)
        else:
            linux.notify(title, message)
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
        count = len(_undo_stack)
    TUI.micro_log(f"Undo stack: {TUI.YELLOW}×{count}{TUI.RESET} available")


def _do_undo() -> None:
    try:
        time.sleep(0.2)
        # Release modifier keys from the undo hotkey combo
        _reset_keyboard_state()

        with _undo_lock:
            if not _undo_stack:
                TUI.warn("Nothing to undo")
                notify(APP_NAME, "Nothing to undo.")
                return
            entry = _undo_stack.pop()

        TUI.separator()
        TUI.action("↩", "UNDO", "Restoring previous text")
        previous_clipboard = clipboard_paste(timeout=0.2)
        clipboard_copy(entry["original"])
        time.sleep(0.4)
        _refocus_source_window_for_paste()
        _send_paste_keys()
        _schedule_clipboard_restore(previous_clipboard)
        time.sleep(0.15)

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
    """Toggle silent mode on/off."""
    global _silent_mode
    with _silent_mode_lock:
        _silent_mode = not _silent_mode
        state = _silent_mode
    if state:
        TUI.micro_log(f"{TUI.DIM}Silent mode ON — notifications suppressed{TUI.RESET}")
    else:
        TUI.micro_log(f"{TUI.GREEN}Silent mode OFF — notifications enabled{TUI.RESET}")
    _update_tray_color("grey" if state else ("green" if llm.MODE == "live" else "yellow"))


def on_silent_triggered() -> None:
    threading.Thread(target=_toggle_silent_mode, daemon=True).start()


# ============================================================
# Built-in Handlers
# ============================================================

def handle_polite(text: str, full_text: str, cmd_config: dict) -> None:
    """Rewrite rude/blunt text politely — phrase lookup first, LLM fallback."""
    phrases = cmd_config.get("phrases", {})
    normalised = text.strip().lower()

    # Try exact phrase match from config first
    result = phrases.get(normalised)

    # No phrase match — use LLM if available
    if result is None:
        if llm.ready:
            TUI.status("\U0001f916", "Rewriting politely with LLM...", TUI.CYAN)
            prompt, model = _llm_prompt_for("polite", cmd_config, text)
            result = llm.call(prompt, model_override=model)
        else:
            notify(APP_NAME, "LLM not configured — cannot rewrite. Use POL: with a provider.")
            TUI.warn("No phrase match and LLM unavailable — text unchanged")
            return

    _push_undo(full_text, result)
    _replace_selection(result)
    TUI.action("📝", "POLITE", f"\"{normalised}\" → \"{result[:60]}\"")


# Read-only tools only. Interpreters (python, node), env/printenv (leak API
# keys into notifications), curl and find (-exec/-delete) are deliberately
# absent; add them in config.yaml → command_security.allowed_commands at your
# own risk.
_CMD_DEFAULT_ALLOWED = [
    "ls", "cat", "grep", "git", "echo", "date", "wc", "head", "tail", "sort",
    "uniq", "diff", "file", "stat", "whoami", "hostname", "uname", "which",
    "pwd", "df", "du", "uptime", "cal",
]

# Arguments that turn an otherwise harmless binary into arbitrary execution
_CMD_BLOCKED_ARGS: dict[str, tuple[str, ...]] = {
    "git": ("-c", "--config-env", "--exec-path", "-C", "--upload-pack",
            "--receive-pack", "--ext-cmd"),
    "find": ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fls"),
}

_CMD_ALLOWED_COMMANDS: list[str] = list(_CMD_DEFAULT_ALLOWED)


def _refresh_command_security() -> None:
    global _CMD_ALLOWED_COMMANDS
    allowed = (CONFIG.get("command_security") or {}).get("allowed_commands")
    _CMD_ALLOWED_COMMANDS = list(allowed) if isinstance(allowed, list) else list(_CMD_DEFAULT_ALLOWED)


_refresh_command_security()


def handle_command(text: str, full_text: str, cmd_config: dict) -> None:
    """Terminal Magic — execute a shell command silently.

    Security: uses shlex.split (no shell=True), allowlist for binary names,
    and runs as $SUDO_USER (not root) via _run_as_user().
    """
    command = text.strip()

    # Parse into argument list — no shell interpretation
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        TUI.error(f"CMD: invalid command syntax: {exc}")
        notify("Security Block", "Invalid command syntax")
        return

    if not parts:
        TUI.error("CMD: empty command")
        return

    # Allowlist check: only the binary name (basename), not full paths
    binary = os.path.basename(parts[0])
    if "/" in parts[0] or binary not in _CMD_ALLOWED_COMMANDS:
        TUI.error(f"BLOCKED — '{parts[0]}' not in allowed commands list")
        notify("Security Block",
               f"'{parts[0]}' is not allowed. Allowed: {', '.join(_CMD_ALLOWED_COMMANDS[:10])}...")
        return
    blocked = _CMD_BLOCKED_ARGS.get(binary, ())
    bad = next((a for a in parts[1:] if a in blocked or
                any(a.startswith(b + "=") for b in blocked if b.startswith("--"))), None)
    if bad:
        TUI.error(f"BLOCKED — '{binary} {bad}' can execute arbitrary code")
        notify("Security Block", f"'{bad}' is not allowed with {binary}")
        return

    try:
        # Execute as the real user, NOT root — shell=False by default
        result = _run_as_user(parts, capture_output=True, text=True, timeout=30,
                              cwd=str(Path.home()), stdin=subprocess.DEVNULL)

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


def handle_test(text: str, full_text: str, cmd_config: dict) -> None:
    """Pipeline test — verifies capture → process → replace."""
    content = text.strip()
    result = f"[TEST OK] \"{content}\" | session={_SESSION_TYPE} | wayland={_IS_WAYLAND} | llm={llm.MODE}"

    _push_undo(full_text, result)
    _replace_selection(result)

    TUI.action("🧪", "TEST", f"Input: \"{content}\"")
    TUI.success(f"Output: \"{result}\"")
    notify("Test", f"Pipeline OK: \"{content[:60]}\"")


def _llm_unavailable(label: str) -> bool:
    """True (after telling the user) when no LLM is configured."""
    if llm.MODE == "live":
        return False
    TUI.warn(f"{label}: no LLM configured — text left unchanged")
    notify(APP_NAME, f"{label} needs an LLM — set llm.provider in config.yaml "
                     "(run: python main.py --check)")
    return True


def _llm_prompt_for(cmd_name: str, cmd_config: dict, text: str) -> tuple[str, str]:
    """prompts.prompt_for() with the current selection's analysis/app context."""
    return prompts.prompt_for(cmd_name, cmd_config, text,
                              prompts.context_vars(text, _current_text_analysis, _current_app_context))


def handle_llm_command(text: str, full_text: str, cmd_config: dict,
                       cmd_key: str = "") -> None:
    """Generic handler for LLM-backed commands defined in config.yaml.

    Injects context variables: {context}, {code_language}, {app_context}
    """
    cmd_name = cmd_config.get("description", "LLM")
    is_display_only = cmd_config.get("display_only", False) or cmd_key in _DISPLAY_ONLY_COMMANDS

    if _llm_unavailable(cmd_name):
        return

    prompt, cmd_model = _llm_prompt_for(cmd_key, cmd_config, text)

    TUI.status("\U0001f916", f"Processing with LLM...", TUI.CYAN)

    result = llm.call(prompt, model_override=cmd_model)

    if is_display_only:
        # Display-only: show in a popup, don't replace text
        _result_queue.put((cmd_name, result))
    else:
        _push_undo(full_text, result)
        _replace_selection(result)

    truncated = result[:80] + ("..." if len(result) > 80 else "")
    provider_tag = f" [{llm.last_provider_used}]" if llm.last_provider_used else ""
    TUI.action("\U0001f916", cmd_name.upper(), f"\"{truncated}\"{provider_tag}")


def _apply(full_text: str, result: str) -> None:
    """Record an undo point and paste `result` over the selection."""
    _push_undo(full_text, result)
    _replace_selection(result)


def handle_fmt(text: str, full_text: str, cmd_config: dict) -> None:
    """Pretty-print / minify ("min:") / sort ("sort:") JSON, YAML or XML."""
    try:
        label, result = textops.format_structured(text)
    except ValueError as exc:
        TUI.error(f"FMT: {exc}")
        notify("Format Error", str(exc)[:200], is_error=True)
        return
    _apply(full_text, result)
    TUI.action("🔧", "FMT", f"{label} ({len(text.strip())} → {len(result)} chars)")


def handle_count(text: str, full_text: str, cmd_config: dict) -> None:
    """Word/char/line stats — shown in a popup, text unchanged."""
    stats = textops.text_stats(text)
    TUI.action("📊", "COUNT", stats.replace("\n", " | "))
    _result_queue.put(("Text Stats", stats))


def handle_mock(text: str, full_text: str, cmd_config: dict) -> None:
    result = textops.mocking_case(text)
    _apply(full_text, result)
    TUI.action("🧽", "MOCK", f"\"{text.strip()[:40]}\" → \"{result[:40]}\"")


def handle_b64(text: str, full_text: str, cmd_config: dict) -> None:
    result = textops.b64_encode(text)
    _apply(full_text, result)
    TUI.action("🔐", "B64", f"Encoded {len(text.strip())} chars → {len(result)} chars")


def handle_decode(text: str, full_text: str, cmd_config: dict) -> None:
    try:
        result = textops.b64_decode(text)
    except ValueError as exc:
        TUI.error(f"DECODE: {exc}")
        notify("Decode Error", f"Invalid input: {exc}", is_error=True)
        return
    _apply(full_text, result)
    TUI.action("🔓", "DECODE", f"Decoded {len(text.strip())} chars → {len(result)} chars")


def handle_hash(text: str, full_text: str, cmd_config: dict) -> None:
    digest = textops.sha256(text)
    _apply(full_text, digest)
    TUI.action("🔑", "HASH", f"SHA256: {digest[:32]}...")


def handle_redact(text: str, full_text: str, cmd_config: dict) -> None:
    """Mask emails, phones, cards, IPs, tokens…"""
    result, count = textops.redact(text)
    _apply(full_text, result)
    TUI.action("🔒", "REDACT", f"Masked {count} item(s)")
    notify("Redact", f"Masked {count} item(s)")


def handle_calc(text: str, full_text: str, cmd_config: dict) -> None:
    content = text.strip()
    result = textops.calc(content)
    if result is None:
        TUI.error(f"CALC: could not evaluate \"{content[:60]}\"")
        notify("Calc Error", f"Could not evaluate: {content[:60]}", is_error=True)
        return
    display = f"{content} = {result}"
    _apply(full_text, display)
    TUI.action("🧮", "CALC", display[:80])


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
    """Escape for HTML / SQL / regex (auto-detected or "html:" / "sql:" / "regex:")."""
    mode, result = textops.escape(text)
    _apply(full_text, result)
    TUI.action("🛡", "ESCAPE", f"[{mode}] {len(text.strip())} chars escaped")


def handle_sanitize(text: str, full_text: str, cmd_config: dict) -> None:
    """Strip HTML / Markdown / ANSI formatting."""
    result = textops.sanitize(text)
    _apply(full_text, result)
    TUI.action("🧹", "SANITIZE", f"Stripped formatting ({len(text.strip())} → {len(result)} chars)")


def handle_password(text: str, full_text: str, cmd_config: dict) -> None:
    length = (cmd_config.get("password_config") or {}).get("length", 20)
    password = textops.generate_password(length)
    _push_undo(full_text, password)
    _replace_selection(password, announce=False)  # never show it in Notification Center
    TUI.action("🔑", "PASSWORD", f"Generated {len(password)}-char password ({password[:4]}...)")


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


_CLIP_NAME_RE = _re.compile(r'^[a-zA-Z0-9_-]{1,64}$')
_MAX_CLIP_SLOTS = 100


def handle_clip(text: str, full_text: str, cmd_config: dict) -> None:
    """Named clipboard slots: save/load/list."""
    content = text.strip()

    # Parse sub-command
    parts = content.split(None, 1)
    sub = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""

    # Validate slot name
    if sub in ("save", "load") and arg and not _CLIP_NAME_RE.match(arg):
        TUI.error("CLIP: name must be 1-64 alphanumeric/dash/underscore characters")
        notify("Clip Error", "Invalid slot name")
        return

    # Load existing clips
    clips = {}
    if _CLIPS_PATH.exists():
        try:
            clips = json.loads(_CLIPS_PATH.read_text())
        except Exception:
            pass

    if sub == "save" and arg:
        if len(clips) >= _MAX_CLIP_SLOTS and arg not in clips:
            TUI.error(f"CLIP: maximum {_MAX_CLIP_SLOTS} slots reached")
            notify("Clip Error", f"Maximum {_MAX_CLIP_SLOTS} slots reached")
            return
        current = clipboard_paste()
        clips[arg] = current
        _CLIPS_PATH.write_text(json.dumps(clips, ensure_ascii=False, indent=2))
        os.chmod(_CLIPS_PATH, 0o600)
        TUI.action("📌", "CLIP:SAVE", f"Saved slot \"{arg}\" ({len(current)} chars)")
        notify("Clip Save", f"Saved to slot \"{arg}\"")

    elif sub == "load" and arg:
        if arg not in clips:
            TUI.error(f"CLIP: slot \"{arg}\" not found")
            notify("Clip Error", f"Slot \"{arg}\" not found")
            return
        clipboard_copy(clips[arg])
        TUI.action("📋", "CLIP:LOAD", f"Loaded slot \"{arg}\" ({len(clips[arg])} chars)")
        notify("Clip Load", f"Loaded \"{arg}\": {clips[arg][:60]}")

    elif sub == "list":
        if not clips:
            TUI.warn("CLIP: no saved slots")
            notify("Clip List", "No saved slots")
        else:
            slot_list = ", ".join(f"{k} ({len(v)} chars)" for k, v in clips.items())
            TUI.action("📋", "CLIP:LIST", slot_list)
            notify("Clip Slots", slot_list[:200])

    else:
        TUI.error("CLIP: use save <name>, load <name>, or list")
        notify("Clip Error", "Usage: CLIP:save <name> | CLIP:load <name> | CLIP:list")


_MAX_CLIPBOARD_STACK = 50


def handle_stack(text: str, full_text: str, cmd_config: dict) -> None:
    """Push current clipboard onto the stack."""
    if len(_clipboard_stack) >= _MAX_CLIPBOARD_STACK:
        TUI.warn(f"STACK: maximum depth ({_MAX_CLIPBOARD_STACK}) reached")
        notify("Stack Full", f"Maximum {_MAX_CLIPBOARD_STACK} items")
        return
    current = clipboard_paste()
    _clipboard_stack.append(current)
    depth = len(_clipboard_stack)

    TUI.action("📥", "STACK", f"Pushed · stack depth: {depth}")
    notify("Stack", f"Pushed · stack depth: {depth}")


def handle_pop(text: str, full_text: str, cmd_config: dict) -> None:
    """Pop top item from clipboard stack and restore to clipboard."""
    if not _clipboard_stack:
        TUI.error("POP: clipboard stack is empty")
        notify("Pop Error", "Clipboard stack is empty")
        return

    item = _clipboard_stack.pop()
    clipboard_copy(item)
    depth = len(_clipboard_stack)

    preview = item[:60] + ("..." if len(item) > 60 else "")
    TUI.action("📤", "POP", f"Restored \"{preview}\" · stack depth: {depth}")
    notify("Pop", f"Restored · stack depth: {depth}")


def handle_tone(text: str, full_text: str, cmd_config: dict) -> None:
    """Dynamic tone rewriting. Expects payload like 'casual: some text here'."""
    m = prompts.TONE_STYLE_RE.match(text)
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

    if _llm_unavailable("Tone"):
        return

    prompt, cmd_model = _llm_prompt_for("tone", cmd_config, text)

    TUI.status("🎨", f"Rewriting in {style} tone...", TUI.CYAN)
    notify(APP_NAME, f"Rewriting in {style} tone...")

    result = llm.call(prompt, model_override=cmd_model)

    _push_undo(full_text, result)
    _replace_selection(result)

    truncated = result[:80] + ("..." if len(result) > 80 else "")
    provider_tag = f" [{llm.last_provider_used}]" if llm.last_provider_used else ""
    TUI.action("🎨", f"TONE→{style}", f"\"{truncated}\"{provider_tag}")
    notify(f"Tone ({style})", truncated)


def handle_trans(text: str, full_text: str, cmd_config: dict) -> None:
    """Translate text to a target language via LLM. Expects payload like 'JP: hello world'."""
    m = prompts.TRANS_LANG_RE.match(text)
    if not m:
        TUI.error("TRANS requires a language code, e.g. TRANS:JP: hello world")
        notify("Translation Error", "Missing language code — use TRANS:<LANG>: text")
        return

    lang_code = m.group(1).strip()
    body = text[m.end():].strip()
    if not body:
        TUI.error("TRANS: no text to translate")
        notify("Translation Error", "No text provided after language code")
        return

    if _llm_unavailable("Translate"):
        return

    prompt, cmd_model = _llm_prompt_for("trans", cmd_config, text)

    TUI.status("🌐", f"Translating to {lang_code} with LLM...", TUI.CYAN)
    notify(APP_NAME, f"Translating to {lang_code}...")

    result = llm.call(prompt, model_override=cmd_model)

    _push_undo(full_text, result)
    _replace_selection(result)

    truncated = result[:80] + ("..." if len(result) > 80 else "")
    provider_tag = f" [{llm.last_provider_used}]" if llm.last_provider_used else ""
    TUI.action("🌐", f"TRANS→{lang_code}", f"\"{truncated}\"{provider_tag}")
    notify(f"Translated ({lang_code})", truncated)


# ============================================================
# IMAGE — AI Image Generation
# ============================================================

_IMAGE_DIR = (linux.effective_home() if linux else Path.home()) / "Pictures" / "ActionFlow_Generated"


def _open_image_folder(path: Path) -> bool:
    return mac.open_path(str(path)) if _IS_MAC else linux.open_path(str(path))


def _clipboard_copy_image(image_path: str) -> bool:
    """Put a PNG on the clipboard so ⌘V / Ctrl+V pastes the image."""
    return mac.clipboard_set_image(image_path) if _IS_MAC else linux.clipboard_set_image(image_path)


_POLLINATIONS_LEGACY = "https://image.pollinations.ai/prompt/"   # anonymous, free
_POLLINATIONS_API = "https://gen.pollinations.ai/image/"           # needs a key
_IMAGE_EXTENSIONS = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp",
                     "image/svg+xml": ".svg"}


def _pollinations_generate(prompt: str, max_retries: int = 3) -> tuple[bytes, str] | None:
    """Generate an image via Pollinations. Returns (bytes, content_type) or None.

    With a key: gen.pollinations.ai, key in the Authorization header (never
    in the URL, where it would end up in logs). Without one — or when the key's
    balance runs out — the free anonymous endpoint.
    """
    encoded_prompt = urllib.parse.quote(prompt)
    use_key = _image_api_key
    for attempt in range(max_retries):
        params: dict[str, str | int] = {"width": 1024, "height": 1024,
                                        "seed": int(time.time()) + attempt * 7}
        headers = {"User-Agent": f"ActionFlow/{__version__}", "Accept": "image/*"}
        model = (_image_api_model or "").strip()
        if use_key:
            url = _POLLINATIONS_API
            headers["Authorization"] = f"Bearer {use_key}"
            if "/" in model:  # new-style ids, e.g. tongyi-mai/z-image-turbo
                params["model"] = model
        else:
            url = _POLLINATIONS_LEGACY
            params["nologo"] = "true"
            if model and "/" not in model:  # legacy names: flux, turbo
                params["model"] = model
        req = urllib.request.Request(f"{url}{encoded_prompt}?{urllib.parse.urlencode(params)}",
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = resp.read(15 * 1024 * 1024 + 1)
                content_type = resp.headers.get_content_type()
                if len(data) <= 15 * 1024 * 1024 and content_type.startswith("image/"):
                    return data, content_type
                TUI.warn(f"IMAGE: attempt {attempt + 1}: unexpected response ({content_type})")
        except urllib.error.HTTPError as exc:
            TUI.warn(f"IMAGE: attempt {attempt + 1}/{max_retries} failed (HTTP {exc.code})")
            if use_key and exc.code in (401, 402, 403):
                TUI.warn("IMAGE: key rejected or out of balance — using the free endpoint")
                use_key = ""
                continue
            time.sleep(3 if exc.code == 429 else 1)
        except Exception as exc:
            TUI.warn(f"IMAGE: attempt {attempt + 1}/{max_retries} failed ({exc})")
            time.sleep(1)
    return None


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
    Also saved to ~/Pictures/ActionFlow_Generated/ for later access.
    """
    prompt = text.strip()
    if not prompt:
        TUI.error("IMAGE: no prompt provided")
        notify("Image Error", "Please provide a description for the image")
        return

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

    generated = _pollinations_generate(prompt)

    if generated is None:
        TUI.error("IMAGE: all generation attempts failed")
        if not _image_api_key:
            notify(
                "Image Error",
                "Could not generate image. Set ACTIONFLOW_IMAGE_API_KEY and run with sudo -E.",
            )
        else:
            notify("Image Error", "Could not generate image — check image API key/provider config")
        return

    try:
        # Save to persistent images directory
        _IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_name = _re.sub(r'[^a-zA-Z0-9_-]', '_', prompt[:40])
        image_data, content_type = generated
        ext = _IMAGE_EXTENSIONS.get(content_type, ".png")
        image_path = _IMAGE_DIR / f"{timestamp}_{safe_name}{ext}"
        image_path.write_bytes(image_data)

        TUI.success(f"Image saved: {image_path}")

        # Copy image to clipboard and paste
        if _clipboard_copy_image(str(image_path)):
            time.sleep(0.15)
            _send_paste_keys()
            TUI.success("Image pasted into application")
            paste_msg = "Pasted into app"
        else:
            # Fallback: insert the file path as text
            clipboard_copy(str(image_path))
            time.sleep(0.4)
            _send_paste_keys()
            TUI.warn("Could not paste image — inserted file path instead")
            paste_msg = "Path inserted (image paste unavailable)"

        folder_opened = _open_image_folder(_IMAGE_DIR)
        open_msg = "Folder opened" if folder_opened else "Folder not opened"
        notify("Image Generated",
               f"{paste_msg}. Saved at: {image_path} · {open_msg}")

        TUI.action("🎨", "IMAGE", f"\"{prompt[:50]}\" → {image_path.name}")

    except urllib.error.URLError as exc:
        TUI.error(f"IMAGE: network error — {exc}")
        notify("Image Error", f"Network error: {exc}")
    except Exception as exc:
        TUI.error(f"IMAGE: generation failed — {exc}")
        notify("Image Error", f"Failed: {exc}")


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


def handle_wiki(text: str, full_text: str, cmd_config: dict) -> None:
    """Wikipedia lookup — fetches first paragraph, shows as notification only."""
    query = text.strip()
    if not query:
        TUI.error("WIKI: no search term provided")
        notify("Wiki Error", "No search term provided", is_error=True)
        return

    try:
        encoded = urllib.parse.quote(query)
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{encoded}"
        req = urllib.request.Request(url, headers={"User-Agent": "ActionFlow/1.0"})
        data = json.loads(_safe_url_read(req).decode())

        extract = data.get("extract", "")
        title = data.get("title", query)

        if not extract:
            TUI.warn(f"WIKI: no results for \"{query}\"")
            notify("Wikipedia", f"No results for \"{query}\"")
            return

        # Show in result popup instead of just notification
        full_result = f"{title}\n{'=' * len(title)}\n\n{extract}"
        TUI.action("📖", "WIKI", f"{title}: {extract[:80]}...")
        _result_queue.put((f"Wikipedia: {title}", full_result))

    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            TUI.warn(f"WIKI: no article found for \"{query}\"")
            notify("Wikipedia", f"No article found for \"{query}\"")
        else:
            TUI.error(f"WIKI: HTTP {exc.code}")
            notify("Wiki Error", f"HTTP error: {exc.code}", is_error=True)
    except Exception as exc:
        TUI.error(f"WIKI: {exc}")
        notify("Wiki Error", str(exc)[:100], is_error=True)


def handle_define(text: str, full_text: str, cmd_config: dict) -> None:
    """Dictionary lookup — fetches definition, shows as notification only."""
    word = text.strip().split()[0] if text.strip() else ""
    if not word:
        TUI.error("DEFINE: no word provided")
        notify("Define Error", "No word provided", is_error=True)
        return

    try:
        encoded = urllib.parse.quote(word.lower())
        url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{encoded}"
        req = urllib.request.Request(url, headers={"User-Agent": "ActionFlow/1.0"})
        data = json.loads(_safe_url_read(req).decode())

        if not data or not isinstance(data, list):
            TUI.warn(f"DEFINE: no definition for \"{word}\"")
            notify("Dictionary", f"No definition for \"{word}\"")
            return

        entry = data[0]
        meanings = entry.get("meanings", [])
        if not meanings:
            TUI.warn(f"DEFINE: no meanings for \"{word}\"")
            notify("Dictionary", f"No meanings found for \"{word}\"")
            return

        # Build definition text from first meaning
        first = meanings[0]
        part_of_speech = first.get("partOfSpeech", "")
        definitions = first.get("definitions", [])
        defn = definitions[0].get("definition", "") if definitions else "(no definition)"

        result = f"({part_of_speech}) {defn}" if part_of_speech else defn

        # Build full definition text with all meanings
        all_parts = []
        for meaning in meanings:
            pos = meaning.get("partOfSpeech", "")
            defs = meaning.get("definitions", [])
            if pos:
                all_parts.append(f"{pos}:")
            for j, d in enumerate(defs[:3], 1):
                defn_text = d.get("definition", "")
                example = d.get("example", "")
                all_parts.append(f"  {j}. {defn_text}")
                if example:
                    all_parts.append(f"     Example: \"{example}\"")

        full_result = f"{word}\n{'=' * len(word)}\n\n" + "\n".join(all_parts)
        TUI.action("📚", "DEFINE", f"{word}: {result[:80]}")
        _result_queue.put((f"Define: {word}", full_result))

    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            TUI.warn(f"DEFINE: word \"{word}\" not found")
            notify("Dictionary", f"Word \"{word}\" not found")
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
    if _llm_unavailable(cmd_config.get("description", "Personal command")):
        return

    prompt, model = _llm_prompt_for("personal", cmd_config, text)
    result = llm.call(prompt, model_override=model)

    _push_undo(full_text, result)
    _replace_selection(result)
    TUI.action("👤", "PERSONAL", f"{cmd_config.get('description', '')[:30]}: {result[:40]}")
    notify("Personal Command", f"{result[:80]}")


def _register_personal_commands() -> None:
    """Register personal commands from config into the command system."""
    for pc_name, pc_config in CONFIG.get("personal_commands", {}).items():
        if not isinstance(pc_config, dict):
            continue
        trigger = pc_config.get("trigger", f"{pc_name.upper()}:")
        cmd_key = f"personal_{pc_name}"
        CONFIG.setdefault("commands", {})[cmd_key] = {
            "prefixes": [trigger],
            "keywords": [pc_name],
            "description": pc_config.get("description", f"Personal: {pc_name}"),
            "llm_required": True,
            "_personal": True,
            "examples": pc_config.get("examples", []),
            "model": pc_config.get("model", ""),
        }
        _BUILTIN_HANDLERS[cmd_key] = handle_personal_command


# Map of built-in command names → handler functions
_BUILTIN_HANDLERS = {
    "polite": handle_polite,
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

def _log_history(command: str, input_text: str, output_text: str, duration_ms: int,
                 app_context: str = "", text_length: int = 0, text_language: str = "",
                 trigger: str = "", is_llm: bool = False) -> None:
    history.log(command, input_text, output_text, duration_ms,
                provider=(llm.last_provider_used or llm.provider) if is_llm else "builtin",
                app_context=app_context, text_length=text_length,
                text_language=text_language, trigger=trigger)


# ============================================================
# Router — 3-tier: Prefix → Keyword → LLM → Fallback
# ============================================================

def dispatch(cmd_name: str, payload: str, full_text: str, cmd_config: dict) -> str | None:
    """Dispatch to the correct handler for a matched command.

    Returns the replacement text the handler produced, "" when the command
    ran without replacing text (COUNT, WIKI, CMD, ...), or None on failure.
    Errors are reported to the user here; they are never re-raised.
    """
    global _last_command, _current_notify_level

    # Set per-command notification level (always | errors_only | never)
    _current_notify_level = cmd_config.get("notify", "always")

    with _usage_lock:
        _usage_counts[cmd_name] = _usage_counts.get(cmd_name, 0) + 1

    # Track for REPEAT (don't track repeat itself)
    if cmd_name != "repeat":
        _last_command = {"name": cmd_name, "config": cmd_config}

    is_llm = cmd_config.get("llm_required", False) or cmd_name not in _BUILTIN_HANDLERS
    start_time = time.time()
    try:
        return _dispatch(cmd_name, payload, full_text, cmd_config, is_llm, start_time)
    finally:
        _current_notify_level = "always"  # per-command setting must not leak


def _dispatch(cmd_name: str, payload: str, full_text: str, cmd_config: dict,
              is_llm: bool, start_time: float) -> str | None:
    with _undo_lock:
        undo_depth = len(_undo_stack)
    history_ctx = dict(
        app_context=_current_app_context.context_type if _current_app_context else "",
        text_length=len(payload),
        text_language=_current_text_analysis.language if _current_text_analysis else "",
        trigger=_popup_trigger,
        is_llm=is_llm,
    )

    try:
        handler = _BUILTIN_HANDLERS.get(cmd_name)
        if handler is not None:
            handler(payload, full_text, cmd_config)
        else:
            handle_llm_command(payload, full_text, cmd_config, cmd_key=cmd_name)
    except Exception as exc:
        duration = time.time() - start_time
        label = "LLM request failed" if isinstance(exc, llm.LLMError) else "Error"
        TUI.activity_entry(cmd_name, payload, str(exc), duration, is_error=True,
                           trigger=_popup_trigger)
        _log_history(cmd_name, payload, f"ERROR: {exc}", int(duration * 1000), **history_ctx)
        notify(APP_NAME, f"{cmd_name}: {label} — text left unchanged. {exc}"[:200],
               is_error=True)
        return None

    duration = time.time() - start_time
    with _undo_lock:
        pushed = len(_undo_stack) > undo_depth
        output = _undo_stack[-1]["replacement"] if pushed else ""
    TUI.activity_entry(cmd_name, payload, output or "(no text change)", duration,
                       is_llm=is_llm, trigger=_popup_trigger)
    _log_history(cmd_name, payload, output or "(no text change)", int(duration * 1000),
                 **history_ctx)
    return output


_chain_suppress_paste = False


def _resolve_prefix(text: str, commands: dict) -> tuple[str, str, dict] | None:
    """Match a prefix at the start of text. Returns (cmd_name, payload, cmd_config) or None.

    Handles leading whitespace and optional space after prefix colon.
    E.g. "  SUM: hello" and "SUM:hello" both match.
    """
    stripped = text.lstrip()
    text_upper = stripped.upper()
    # Sort prefixes by length descending to match longest prefix first
    # (e.g. "TONE:casual:" before "TONE:")
    candidates: list[tuple[str, str, dict]] = []
    for name, cmd in commands.items():
        for prefix in cmd.get("prefixes", []):
            prefix_upper = prefix.upper()
            if text_upper.startswith(prefix_upper):
                payload = stripped[len(prefix):]
                # Allow optional space after prefix (e.g. "SUM: text" and "SUM:text")
                if payload.startswith(" "):
                    payload = payload[1:]
                candidates.append((name, payload, cmd, len(prefix)))
    if not candidates:
        return None
    # Return the longest matching prefix
    candidates.sort(key=lambda c: c[3], reverse=True)
    return candidates[0][0], candidates[0][1], candidates[0][2]


def _parse_chain(text: str, commands: dict) -> list[tuple[str, dict]] | None:
    """Parse pipe-separated prefix chain like 'POL:|SUM: payload'.

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
    """Extract the payload text from a chain like 'POL:|SUM: the actual text'."""
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

            # Suppress clipboard paste for intermediate steps
            _chain_suppress_paste = not is_last
            try:
                TUI.status("⛓", f"Step {step_label}...", TUI.CYAN)
                output = dispatch(cmd_name, current_input, text, cmd_config)
            finally:
                _chain_suppress_paste = False

            if is_last:
                break
            if not output:
                TUI.error(f"Chain stopped at step {step_label}: no text output")
                notify(APP_NAME, f"Chain stopped at step {step_label} — text left unchanged",
                       is_error=True)
                return
            # Intermediate results are not undo points: undo must restore the
            # originally selected text, not a half-processed one.
            with _undo_lock:
                if _undo_stack and _undo_stack[-1]["replacement"] == output:
                    _undo_stack.pop()
            current_input = output

        return

    # Tier 1: Exact prefix match (fastest, backward-compatible)
    prefix_match = _resolve_prefix(text, commands)
    if prefix_match:
        name, payload, cmd = prefix_match
        TUI.status("🎯", f"Prefix match → {name}", TUI.GREEN)
        dispatch(name, payload, text, cmd)
        return

    # Tier 2: Keyword match (check first 3 words)
    stripped = text.strip()
    first_words = [w.strip(".,:;!?") for w in stripped.lower().split()[:3]]
    for name, cmd in commands.items():
        for keyword in cmd.get("keywords", []):
            kw = keyword.lower()
            if " " in kw:
                # Multi-word keywords must open the text
                if not stripped.lower().startswith(kw):
                    continue
                payload = stripped[len(kw):].strip()  # keep the original case
            elif kw in first_words:
                # Cut everything up to and including the keyword *word*
                m = _re.search(rf"\b{_re.escape(kw)}\b[.,:;!?]?", stripped, _re.IGNORECASE)
                payload = stripped[m.end():].strip() if m else stripped
            else:
                continue
            TUI.status("🔑", f"Keyword match: \"{keyword}\" → {name}", TUI.GREEN)
            dispatch(name, payload, text, cmd)
            return

    # Tier 3: LLM intent classification (live mode only)
    if llm.MODE == "live" and llm.ready:
        TUI.status("🤖", "No prefix/keyword match — asking LLM to classify...", TUI.CYAN)
        intent = llm.classify(text, commands)
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

# One selection at a time: capture → (palette) → paste. Held by the hotkey
# worker and, when the palette opens, handed to the main thread which
# releases it after the palette closes. (threading.Lock may be released by
# another thread.)
_job_lock = threading.Lock()


def _do_intercept() -> None:
    if not _job_lock.acquire(blocking=False):
        TUI.warn("Hotkey ignored — still working on the previous selection")
        notify(APP_NAME, "⏳ Still working on the previous selection")
        return
    handed_to_palette = False
    try:
        # A new cycle must not be clobbered by a pending clipboard restore
        _cancel_pending_clipboard_restore()

        if not _IS_MAC:
            time.sleep(0.15)  # let the user release the hotkey (macOS waits on modifiers instead)
        _reset_keyboard_state()

        TUI.separator()
        TUI.status("⌨", "Hotkey triggered — reading selection...", TUI.CYAN)
        TUI.micro_log(f"Hotkey triggered — reading selection...")

        if _IS_MAC:
            text: str = mac.capture_selection()
            if not text:
                if mac.is_accessibility_trusted() is False:
                    TUI.warn("Cmd+C was blocked — grant Accessibility to your terminal app")
                    notify(APP_NAME, "Grant Accessibility permission to your terminal app.")
                else:
                    TUI.warn("No text copied from selection")
                    notify(APP_NAME, "No text selected.")
                return
        else:
            text = linux.capture_selection()

        if not text or not text.strip():
            TUI.warn("No text captured from selection")
            notify(APP_NAME, "No text selected.")
            return

        truncated = text[:60] + ("..." if len(text) > 60 else "")
        TUI.action("📋", "CAPTURED", f"\"{truncated}\"")

        # Phase 8: detect app context and analyze text
        global _current_app_context, _current_text_analysis
        global _popup_trigger, _current_source_window
        _current_app_context = detect_active_window()
        _current_text_analysis = analyze_text(text)
        TUI.micro_log(
            f"Context: {_current_app_context.context_type}"
            f" · {_current_text_analysis.looks_like}"
            f" · {_current_text_analysis.language}"
        )

        commands = CONFIG.get("commands", {})
        routed_text = text.lstrip()
        has_prefix = _resolve_prefix(routed_text, commands) is not None
        has_chain = _parse_chain(routed_text, commands) is not None

        # If user explicitly typed a prefix/chain, run immediately without popup.
        if has_prefix or has_chain:
            _popup_trigger = "prefix"
            _current_source_window = _get_active_window_id()
            if _current_source_window:
                TUI.micro_log(f"Captured source window: {_current_source_window}")
            else:
                TUI.micro_log("Captured source window: unavailable (will use Alt+Tab fallback)")

            TUI.micro_log("Prefix detected — executing without popup")
            try:
                route(routed_text)
            finally:
                _current_source_window = None
                # Reset keyboard state after dispatch to ensure hotkeys keep working.
                time.sleep(0.05)
                _reset_keyboard_state()
            return

        # No prefix: open command picker popup
        if _POPUP_AVAILABLE:
            # Save which window is focused so we can refocus it after the popup
            source_window = _get_active_window_id()
            if source_window:
                TUI.micro_log(f"Captured source window: {source_window}")
            else:
                TUI.micro_log("Captured source window: unavailable (will use Alt+Tab fallback)")
            _popup_queue.put((text, source_window))
            handed_to_palette = True
            TUI.micro_log("Opening command picker...")
            return

        # No popup available. Guessing a command from keywords / LLM intent
        # would rewrite ordinary selected text, so it is opt-in.
        if not CONFIG.get("smart_routing", False):
            TUI.warn("No command prefix in selection (popup unavailable)")
            notify(APP_NAME, "No command prefix found — start the selection with e.g. SUM: or POL:")
            return
        TUI.warn("tkinter unavailable — using keyword/LLM routing (smart_routing: true)")
        _popup_trigger = "prefix"
        _current_source_window = _get_active_window_id()
        try:
            route(text)
        finally:
            _current_source_window = None

    except Exception as exc:
        TUI.error(f"Interceptor error: {exc}")
        notify(APP_NAME, f"Error: {exc}", is_error=True)
    finally:
        if not handed_to_palette:
            _job_lock.release()


def on_hotkey_triggered() -> None:
    threading.Thread(target=_do_intercept, daemon=True).start()


# ============================================================
# Command Search
# ============================================================

def _command_search() -> None:
    """Interactive fuzzy search over command names and keywords. Called from cbreak-mode main loop."""
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

        if select.select([sys.stdin], [], [], 0.1)[0]:
            ch = sys.stdin.read(1)
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
            elif ch == '\x7f':  # Backspace
                query = query[:-1]
            elif ch.isprintable():
                query += ch


# ============================================================
# Session Export
# ============================================================

def _session_export() -> None:
    """Dump this session's activity to ~/actionflow_session_<ts>.md (the S key)."""
    mode = f"live · {llm.provider}/{llm.model}" if llm.MODE == "live" else "mock"
    try:
        path = history.export_session(datetime.fromtimestamp(_start_time), mode)
        TUI.micro_log(f"{TUI.GREEN}✓{TUI.RESET} Session exported → {TUI.CYAN}{path}{TUI.RESET}")
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


def _start_mac_menubar() -> None:
    """Menu bar icon on macOS (the pystray tray is Linux-only)."""
    global _mac_menubar, _mac_menubar_silent_item
    try:
        bar = mac.StatusBar()
    except Exception as exc:
        TUI.warn(f"Menu bar icon unavailable: {exc}")
        return

    mode = f"live · {llm.provider}/{llm.model}" if llm.MODE == "live" else "mock mode (no LLM)"
    bar.add_item(f"ActionFlow — {mode}")
    bar.add_item(f"{mac.format_hotkey(HOTKEY)}  process selection   "
                 f"{mac.format_hotkey(UNDO_HOTKEY)}  undo")
    bar.add_separator()

    def toggle_silent() -> None:
        _toggle_silent_mode()

    _mac_menubar_silent_item = bar.add_item("Silent mode", toggle_silent)
    bar.set_checked(_mac_menubar_silent_item, _silent_mode)
    bar.set_dimmed(_silent_mode)
    bar.add_item("Recent history…", lambda: _result_queue.put(("History (last 20)",
                                                                history.recent_text())))
    bar.add_separator()
    bar.add_item("Open config.yaml", lambda: mac.open_path(str(_CONFIG_PATH)))
    bar.add_item("Reload config", _reload_config)
    bar.add_item("Open images folder", lambda: mac.open_path(str(_IMAGE_DIR))
                 if _IMAGE_DIR.exists() else notify(APP_NAME, "No generated images yet"))
    bar.add_separator()
    bar.add_item("Quit ActionFlow", _exit_event.set, key="q")
    _mac_menubar = bar


def _start_tray() -> None:
    """Start system tray icon in a background thread."""
    global _tray_icon
    try:
        import pystray
        from pystray import MenuItem
        # Force-check that the backend actually loads (catches missing GIR bindings)
        pystray.Icon("_test")
    except Exception as exc:
        TUI.warn(f"Tray icon unavailable: {exc}")
        TUI.warn("Fix: sudo apt install gir1.2-ayatanaappindicator3-0.1")
        return

    icon_img = _create_tray_icon_image(
        "grey" if _silent_mode else ("green" if llm.MODE == "live" else "yellow")
    )
    if icon_img is None:
        TUI.warn("Pillow not installed — tray icon disabled (pip install Pillow)")
        return

    def on_history(icon, item):
        _result_queue.put(("History (last 20)", history.recent_text()))

    def on_settings(icon, item):
        try:
            _run_as_user(["xdg-open", str(_CONFIG_PATH)], capture_output=True, timeout=5)
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
# macOS — Hotkeys & Permissions
# ============================================================

_mac_hotkeys = None  # platform_mac.HotkeyListener, set in _start_mac_hotkeys()


def _start_mac_hotkeys(bindings: list) -> bool:
    """Register global hotkeys on macOS, explaining missing permissions."""
    global _mac_hotkeys
    if os.geteuid() == 0:
        TUI.warn("Don't run ActionFlow with sudo on macOS — it isn't needed and "
                 "breaks clipboard/notification access. Run: python main.py")

    if not mac.has_pyobjc():
        TUI.error("PyObjC is missing — run: pip install -r requirements.txt")
        return False

    trusted = mac.is_accessibility_trusted()
    listening = mac.has_input_monitoring()
    if not trusted or not listening:
        missing = [name for name, ok in (("Accessibility", trusted),
                                         ("Input Monitoring", listening)) if not ok]
        TUI.box("macOS permissions needed", [
            f"  {TUI.YELLOW}Missing: {', '.join(missing)}{TUI.RESET}",
            f"  {TUI.DIM}System Settings → Privacy & Security → enable your terminal app{TUI.RESET}",
            f"  {TUI.DIM}(Terminal / iTerm2 / VS Code …) in both lists, then restart it.{TUI.RESET}",
        ], TUI.YELLOW)
        mac.request_permissions()
        if not trusted:
            mac.open_privacy_settings("Accessibility")

    listener = mac.HotkeyListener()
    try:
        for spec, callback in bindings:
            listener.add_hotkey(spec, callback)
    except ValueError as exc:
        TUI.error(f"Invalid hotkey in config.yaml: {exc}")
        return False
    if not listener.start():
        TUI.error(f"Global hotkeys unavailable: {listener.error}")
        return False
    if listener.listen_only:
        TUI.warn("Hotkeys work but are not swallowed (no Accessibility permission yet)")
    _mac_hotkeys = listener
    return True


# ============================================================
# Main Entry Point
# ============================================================

_instance_lock_file = None  # kept open for the process lifetime


def _acquire_single_instance_lock() -> bool:
    """Two instances would both grab the hotkey and paste twice."""
    global _instance_lock_file
    import fcntl
    try:
        _instance_lock_file = open(Path.home() / ".actionflow.lock", "w")
        fcntl.flock(_instance_lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _ensure_user_config() -> None:
    """First run: copy config.yaml.example → config.yaml so the setup prompt
    (which saves provider/model) doesn't create a config with no commands."""
    try:
        if ensure_user_config():
            TUI.micro_log(f"Created {_CONFIG_PATH.name} from config.yaml.example")
    except OSError as exc:
        TUI.warn(f"Could not create config.yaml: {exc}")


def main(keep_banner: bool = False, no_tray: bool = False) -> None:
    global _start_time, _pattern_learner, _silent_mode
    _start_time = time.time()
    if not _acquire_single_instance_lock():
        print("ActionFlow is already running (another terminal or the login agent).")
        return
    interactive_out = sys.stdout.isatty()
    if not interactive_out:
        TUI.disable_colors()
        keep_banner = True  # nothing to collapse in a log file
    _ensure_user_config()

    if interactive_out:
        print("\033[2J\033[3J\033[H", end="", flush=True)

    TUI.banner()

    # Initialize usage counters
    for cmd_name in CONFIG.get("commands", {}):
        _usage_counts[cmd_name] = 0

    # Interactive LLM setup (only if not already configured)
    setup_wizard.run_llm_setup()
    _init_image_api()

    # Init LLM
    llm.on_warning = TUI.warn
    llm.init()

    # Environment box (rendered after LLM init so Mode is known)
    config_val = f"{TUI.DIM}{_CONFIG_PATH if _CONFIG_PATH.exists() else 'defaults (no config.yaml)'}{TUI.RESET}"
    if llm.MODE == "live":
        mode_val = f"{TUI.GREEN}LIVE ({llm.provider}){TUI.RESET}"
    else:
        mode_val = f"{TUI.YELLOW}MOCK{TUI.RESET}"
    learning_val = f"{TUI.CYAN}0 samples{TUI.RESET}"
    if _pattern_learner and _pattern_learner.sample_count > 0:
        learning_val = f"{TUI.CYAN}{_pattern_learner.sample_count} samples{TUI.RESET}"
    session = (f"macOS {platform.mac_ver()[0]}" if _IS_MAC
               else f"{_SESSION_TYPE} ({'Wayland' if _IS_WAYLAND else linux.DISPLAY})")
    TUI.box("Environment", [
        f"  {TUI.DIM}Session{TUI.RESET}    {TUI.CYAN}{session}{TUI.RESET}",
        f"  {TUI.DIM}User{TUI.RESET}       {TUI.CYAN}{_SUDO_USER or os.environ.get('USER', '?')}{TUI.RESET}",
        f"  {TUI.DIM}Mode{TUI.RESET}       {mode_val}",
        f"  {TUI.DIM}Learning{TUI.RESET}   {learning_val}",
        f"  {TUI.DIM}Config{TUI.RESET}     {config_val}",
    ], TUI.CYAN)

    print()
    _tui_llm_status_box()

    print()
    _tui_activity_placeholder()
    print()
    _tui_commands_table()
    print()
    _tui_keybind_table()
    print()

    # Collapse banner after init unless --banner flag is set
    if not keep_banner:
        time.sleep(2)
        print("\033[2J\033[3J\033[H", end="", flush=True)
        _tui_header_line()
        print()
        _tui_llm_status_box()
        print()
        _tui_activity_placeholder()
        print()
        _tui_commands_table()
        print()
        _tui_keybind_table()
        print()

    # Start config hot-reload watcher
    _start_config_watcher()

    # Background auto-update check
    threading.Thread(target=_check_for_updates, daemon=True).start()

    # Start system tray icon. On macOS both pystray and Tk need the main
    # thread's run loop, so the tray is Linux-only for now.
    if _IS_MAC:
        if _NATIVE_UI:
            mac_ui.init_app()  # NSApplication on the main thread (accessory: no Dock icon)
    elif not no_tray:
        threading.Thread(target=_start_tray, daemon=True).start()

    # Initialize PatternLearner
    history.rotate()
    _pattern_learner = PatternLearner(history.HISTORY_PATH)
    _pattern_learner.load()
    if _pattern_learner.sample_count > 0:
        TUI.micro_log(f"PatternLearner: {_pattern_learner.sample_count} samples loaded")

    # Initialize silent mode from config
    _silent_mode = CONFIG.get("silent_mode", False)

    if _NATIVE_UI and not no_tray:
        _start_mac_menubar()

    # Register personal commands from config
    _register_personal_commands()

    # Start portal paste helper for GNOME Wayland
    if _IS_WAYLAND:
        TUI.micro_log("Starting portal paste helper...")
        if linux.start_paste_helper():
            TUI.micro_log(f"{TUI.GREEN}Portal paste helper ready{TUI.RESET}")
        else:
            TUI.warn("Portal paste helper unavailable — paste may not work on GNOME Wayland")
            TUI.warn("Install ydotool as fallback: sudo apt install ydotool")

    # Register hotkeys
    silent_hotkey = CONFIG.get("hotkeys", {}).get("silent_toggle", "ctrl+alt+s")
    hotkey_bindings = [
        (HOTKEY, on_hotkey_triggered),
        (UNDO_HOTKEY, on_undo_triggered),
        (silent_hotkey, on_silent_triggered),
    ]
    if _IS_MAC:
        if not _start_mac_hotkeys(hotkey_bindings):
            return
    else:
        for spec, callback in hotkey_bindings:
            linux.add_hotkey(spec, callback)

    notify(
        "ActionFlow Active",
        f"{HOTKEY.upper()} to intercept | {UNDO_HOTKEY.upper()} to undo | Ctrl+C to exit",
    )

    TUI.separator()
    cmd_count = len(CONFIG.get("commands", {}))
    llm_label = f"LLM: {llm.provider}" if llm.MODE == "live" else "Mock mode"
    TUI.micro_log(f"{TUI.GREEN}✓{TUI.RESET} Listening for hotkeys...")
    TUI.micro_log(f"{cmd_count} commands loaded | {llm_label} | {HOTKEY.upper()} to intercept")
    TUI.micro_log(f"{TUI.DIM}/ = search  S = export session  Ctrl+C = exit{TUI.RESET}")
    print()

    interactive = sys.stdin.isatty()
    try:
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd) if interactive else None
        if interactive:
            tty.setcbreak(fd)
        try:
            while not _exit_event.is_set():
                if not interactive:
                    time.sleep(0.1)  # no terminal (launchd/systemd) — just pump queues
                elif select.select([sys.stdin], [], [], 0.1)[0]:
                    ch = sys.stdin.read(1)
                    if ch == '\x03':  # Ctrl+C
                        break
                    elif ch == '/':
                        _command_search()
                    elif ch in ('S', 's'):
                        _session_export()

                while not _main_thread_calls.empty():
                    try:
                        _main_thread_calls.get_nowait()()
                    except Exception as exc:
                        TUI.warn(f"Main-thread task failed: {exc}")

                # Keep the Cocoa / Tk event loop alive between popups (menu bar
                # clicks; otherwise macOS marks the process as "not responding")
                if _NATIVE_UI:
                    mac_ui.pump(0)
                elif tk_ui is not None and tk_ui.root_if_created() is not None:
                    try:
                        tk_ui.root_if_created().update()
                    except Exception:
                        pass

                # Check popup queue — command picker triggered by hotkey
                try:
                    popup_text, source_window = _popup_queue.get_nowait()
                    # Restore terminal for tkinter popup
                    if interactive:
                        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    try:
                        _handle_popup(popup_text, source_window=source_window)
                    except Exception as exc:
                        TUI.error(f"Popup error: {exc}")
                    finally:
                        _job_lock.release()
                    # Restore cbreak for TUI
                    if interactive:
                        tty.setcbreak(fd)
                except queue.Empty:
                    pass

                # Check result queue — display-only popups (wiki, define, count)
                try:
                    result_title, result_text = _result_queue.get_nowait()
                    if interactive:
                        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    try:
                        if _NATIVE_UI:
                            if mac_ui.show_result(result_title, result_text, _palette_status_line()):
                                TUI.micro_log("Result copied to clipboard")
                        elif _TKINTER_AVAILABLE:
                            result_popup = tk_ui.ResultPopup(result_title, result_text, clipboard_copy)
                            result_popup.run()
                    except Exception as exc:
                        TUI.error(f"Result popup error: {exc}")
                    if interactive:
                        tty.setcbreak(fd)
                except (queue.Empty, ValueError):
                    pass
        finally:
            if interactive:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except KeyboardInterrupt:
        pass

    if _mac_hotkeys is not None:
        _mac_hotkeys.stop()
    if _mac_menubar is not None:
        _mac_menubar.remove()

    if linux:
        linux.stop_paste_helper()

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
        url = "https://api.github.com/repos/azimxxd/watashigpt/releases/latest"
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





if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ActionFlow by WatashiGPT")
    parser.add_argument("--install", action="store_true",
                        help="Start at login (macOS LaunchAgent / Linux systemd service)")
    parser.add_argument("--uninstall", action="store_true", help="Remove the macOS login agent")
    parser.add_argument("--check", action="store_true",
                        help="Test the configured LLM provider(s) and list their models")
    parser.add_argument("--set-key", metavar="PROVIDER",
                        help="Save an API key in the system keychain (e.g. groq, image:pollinations)")
    parser.add_argument("--banner", action="store_true", help="Keep the full ASCII banner permanently")
    parser.add_argument("--history", action="store_true", help="Browse last 50 history entries")
    parser.add_argument("--grep", type=str, default=None, help="Filter history by command name (use with --history)")
    parser.add_argument("--no-tray", action="store_true", help="No tray (Linux) / menu bar (macOS) icon")
    args = parser.parse_args()

    if args.set_key:
        setup_wizard.set_key_cli(args.set_key)
    elif args.check:
        setup_wizard.check_cli()
    elif args.install:
        if _IS_MAC:
            service.install_launch_agent(Path(__file__).resolve())
        else:
            linux.install_systemd_service(Path(__file__).resolve())
    elif args.uninstall:
        service.uninstall_launch_agent()
    elif args.history:
        history.show_cli(args.grep)
    else:
        main(keep_banner=args.banner, no_tray=args.no_tray)
