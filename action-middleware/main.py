# Action Middleware — OS-level background assistant
#
# Run with: sudo -E python main.py
#   -E preserves DISPLAY, WAYLAND_DISPLAY, DBUS_SESSION_BUS_ADDRESS
#
# Config: edit config.yaml to add commands, set hotkeys, configure LLM

import os
import sys
import time
import threading
import subprocess
import platform
import keyboard
import shutil
import yaml
from pathlib import Path
from datetime import datetime

if platform.system() != "Linux":
    import pyperclip
    from plyer import notification

# ============================================================
# Config Loading
# ============================================================

_SCRIPT_DIR = Path(__file__).parent
_CONFIG_PATH = _SCRIPT_DIR / "config.yaml"

_DEFAULT_CONFIG = {
    "hotkeys": {"intercept": "ctrl+alt+x", "undo": "ctrl+alt+z"},
    "commands": {
        "translate": {
            "prefixes": ["TR:"],
            "keywords": ["translate", "polite", "rephrase"],
            "description": "Convert rude text to professional",
            "phrases": {
                "fix this garbage": "Please review the code for potential improvements.",
                "this is broken": "I've identified an issue that needs attention.",
            },
        },
        "command": {
            "prefixes": ["CMD:"],
            "keywords": ["run", "execute", "shell"],
            "description": "Execute a shell command",
        },
        "test": {
            "prefixes": ["TEST:"],
            "keywords": ["test", "ping", "check"],
            "description": "Test the pipeline",
        },
    },
    "llm": {"provider": "", "api_key": "", "model": ""},
}


def load_config() -> dict:
    """Load config.yaml, falling back to defaults if missing or invalid."""
    if _CONFIG_PATH.exists():
        try:
            with open(_CONFIG_PATH, "r") as f:
                user_cfg = yaml.safe_load(f) or {}
            # Merge: user config overrides defaults
            cfg = {**_DEFAULT_CONFIG}
            if "hotkeys" in user_cfg:
                cfg["hotkeys"] = {**cfg["hotkeys"], **user_cfg["hotkeys"]}
            if "commands" in user_cfg:
                cfg["commands"] = user_cfg["commands"]
            if "llm" in user_cfg:
                cfg["llm"] = {**cfg["llm"], **user_cfg["llm"]}
            return cfg
        except Exception as exc:
            print(f"  Warning: Failed to load config.yaml: {exc}")
            print(f"  Falling back to defaults.")
            return _DEFAULT_CONFIG
    return _DEFAULT_CONFIG


CONFIG = load_config()

# ============================================================
# Constants (from config)
# ============================================================

HOTKEY: str = CONFIG["hotkeys"]["intercept"]
UNDO_HOTKEY: str = CONFIG["hotkeys"]["undo"]
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
# LLM Integration
# ============================================================

_llm_client = None
_llm_ready = False
_llm_provider = ""
_llm_model = ""


def _init_llm() -> None:
    """Initialize LLM client from config. Sets _llm_ready=True on success."""
    global _llm_client, _llm_ready, _llm_provider, _llm_model

    llm_cfg = CONFIG.get("llm", {})
    provider = llm_cfg.get("provider", "").strip().lower()
    api_key = llm_cfg.get("api_key", "").strip()
    model = llm_cfg.get("model", "").strip()

    # Allow env var override for API key
    if not api_key:
        api_key = os.environ.get("ACTION_MW_API_KEY", "").strip()

    if not provider or not api_key:
        _llm_ready = False
        return

    try:
        from openai import OpenAI

        if provider == "groq":
            _llm_client = OpenAI(
                api_key=api_key,
                base_url="https://api.groq.com/openai/v1",
            )
            _llm_model = model or "llama-3.3-70b-versatile"
        elif provider == "openai":
            _llm_client = OpenAI(api_key=api_key)
            _llm_model = model or "gpt-4o-mini"
        else:
            TUI.warn(f"Unknown LLM provider: '{provider}'. Mock mode active.")
            return

        _llm_provider = provider
        _llm_ready = True

    except ImportError:
        TUI.warn("openai package not installed. Run: pip install openai")
        TUI.warn("Mock mode active.")
    except Exception as exc:
        TUI.error(f"LLM init failed: {exc}")


def _llm_call(prompt: str) -> str:
    """Send prompt to configured LLM. Returns response text."""
    if not _llm_ready or not _llm_client:
        return _mock_llm_call(prompt)
    try:
        response = _llm_client.chat.completions.create(
            model=_llm_model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.7,
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        TUI.error(f"LLM call failed: {exc}")
        return _mock_llm_call(prompt)


def _mock_llm_call(prompt: str) -> str:
    """Mock fallback — returns placeholder text without any API call."""
    # Extract the actual user text from the prompt template
    lines = prompt.strip().split("\n")
    user_text = lines[-1] if lines else prompt
    return f"[Mock Mode] {user_text[:120]}"


def _llm_classify(text: str, commands: dict) -> dict | None:
    """Ask LLM to classify text intent. Returns {"name": ..., "payload": ...} or None."""
    if not _llm_ready:
        return None

    cmd_list = "\n".join(
        f"- {name}: {cmd.get('description', '')}"
        for name, cmd in commands.items()
    )

    prompt = (
        f"Classify the following text into one of these commands:\n{cmd_list}\n\n"
        f"Text: \"{text}\"\n\n"
        f"Reply with ONLY the command name (one word, lowercase). "
        f"If none match, reply \"unknown\"."
    )

    try:
        result = _llm_call(prompt).strip().lower()
        if result in commands and result != "unknown":
            return {"name": result, "payload": text}
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

    @classmethod
    def commands_table(cls) -> None:
        commands = CONFIG.get("commands", {})
        lines = []
        for name, cmd in commands.items():
            prefixes = ", ".join(cmd.get("prefixes", []))
            keywords = ", ".join(cmd.get("keywords", [])[:3])
            llm_tag = f" {cls.YELLOW}[LLM]{cls.RESET}" if cmd.get("llm_required") else ""
            desc = cmd.get("description", "")
            lines.append(
                f"  {cls.CYAN}{cls.BOLD}{name:<12}{cls.RESET} "
                f"{cls.DIM}{prefixes:<20}{cls.RESET} "
                f"{cls.DIM}{keywords}{cls.RESET}"
                f"{llm_tag}"
            )
        cls.box("Commands", lines, cls.CYAN)

    @classmethod
    def llm_status_box(cls) -> None:
        if _llm_ready:
            lines = [
                f"  {cls.GREEN}{cls.BOLD}LIVE{cls.RESET}    {cls.DIM}Provider: {_llm_provider}{cls.RESET}",
                f"          {cls.DIM}Model: {_llm_model}{cls.RESET}",
            ]
            cls.box("LLM", lines, cls.GREEN)
        else:
            lines = [
                f"  {cls.YELLOW}{cls.BOLD}MOCK MODE{cls.RESET}",
                f"  {cls.DIM}Set provider + api_key in config.yaml for live LLM{cls.RESET}",
            ]
            cls.box("LLM", lines, cls.YELLOW)


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
# Auto-Replace
# ============================================================

def _replace_selection(new_text: str) -> None:
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
    try:
        time.sleep(0.5)

        with _undo_lock:
            if not _undo_stack:
                TUI.warn("Nothing to undo")
                notify(APP_NAME, "Nothing to undo.")
                return
            entry = _undo_stack.pop()

        TUI.separator()
        TUI.action("↩", "UNDO", "Sending native Ctrl+Z")
        keyboard.send("ctrl+z")

        truncated = entry["original"][:50] + ("..." if len(entry["original"]) > 50 else "")
        TUI.success(f"Undone — original was: \"{truncated}\"")
        notify("Undo", "Reverted last replacement")

    except Exception as exc:
        TUI.error(f"Undo error: {exc}")


def on_undo_triggered() -> None:
    threading.Thread(target=_do_undo, daemon=True).start()


# ============================================================
# Built-in Handlers
# ============================================================

def handle_translate(text: str, full_text: str, cmd_config: dict) -> None:
    """Corporate Translator — uses phrases from config."""
    phrases = cmd_config.get("phrases", {})
    normalised = text.strip().lower()
    result = phrases.get(normalised, f"Politely: {text.strip()}")

    _push_undo(full_text, result)
    _replace_selection(result)

    TUI.action("📝", "TRANSLATE", f"\"{normalised}\" → \"{result}\"")
    notify("Corporate Translator", f"Replaced: \"{result[:80]}\"")


def handle_command(text: str, full_text: str, cmd_config: dict) -> None:
    """Terminal Magic — execute a shell command silently."""
    command = text.strip()

    dangerous_patterns = ["rm -rf /", "format", "mkfs", ":(){", "dd if="]
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


def handle_test(text: str, full_text: str, cmd_config: dict) -> None:
    """Pipeline test — verifies capture → process → replace."""
    content = text.strip()
    result = f"[TEST OK] \"{content}\" | session={_SESSION_TYPE} | wayland={_IS_WAYLAND} | llm={'live' if _llm_ready else 'mock'}"

    _push_undo(full_text, result)
    _replace_selection(result)

    TUI.action("🧪", "TEST", f"Input: \"{content}\"")
    TUI.success(f"Output: \"{result}\"")
    notify("Test", f"Pipeline OK: \"{content[:60]}\"")


def handle_llm_command(text: str, full_text: str, cmd_config: dict) -> None:
    """Generic handler for LLM-backed commands defined in config.yaml."""
    prompt_template = cmd_config.get("llm_prompt", "Process this text: {text}")
    prompt = prompt_template.format(text=text.strip())
    cmd_name = cmd_config.get("description", "LLM")

    TUI.status("🤖", f"Processing with {'LLM' if _llm_ready else 'Mock'}...", TUI.CYAN)
    notify(APP_NAME, "Processing...")

    if _llm_ready:
        result = _llm_call(prompt)
    else:
        result = _mock_llm_call(prompt)

    _push_undo(full_text, result)
    _replace_selection(result)

    truncated = result[:80] + ("..." if len(result) > 80 else "")
    TUI.action("🤖", cmd_name.upper(), f"\"{truncated}\"")
    notify(cmd_name, f"Done: \"{truncated}\"")


# Map of built-in command names → handler functions
_BUILTIN_HANDLERS = {
    "translate": handle_translate,
    "command": handle_command,
    "test": handle_test,
}


# ============================================================
# Router — 3-tier: Prefix → Keyword → LLM → Fallback
# ============================================================

def dispatch(cmd_name: str, payload: str, full_text: str, cmd_config: dict) -> None:
    """Dispatch to the correct handler for a matched command."""
    if cmd_name in _BUILTIN_HANDLERS:
        _BUILTIN_HANDLERS[cmd_name](payload, full_text, cmd_config)
    elif cmd_config.get("llm_required"):
        handle_llm_command(payload, full_text, cmd_config)
    else:
        # Unknown built-in, no LLM — treat as generic LLM command anyway
        handle_llm_command(payload, full_text, cmd_config)


def route(text: str) -> None:
    commands = CONFIG.get("commands", {})

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

    # Tier 3: LLM intent classification (if available)
    if _llm_ready:
        TUI.status("🤖", "No prefix/keyword match — asking LLM to classify...", TUI.CYAN)
        intent = _llm_classify(text, commands)
        if intent:
            cmd = commands[intent["name"]]
            TUI.status("🤖", f"LLM classified: → {intent['name']}", TUI.GREEN)
            dispatch(intent["name"], intent["payload"], text, cmd)
            return

    # Tier 4: Fallback
    truncated = text[:40] + ("..." if len(text) > 40 else "")
    TUI.warn(f"Unknown command: \"{truncated}\"")

    available = ", ".join(
        p for cmd in commands.values() for p in cmd.get("prefixes", [])
    )
    keywords = ", ".join(
        k for cmd in commands.values() for k in cmd.get("keywords", [])[:2]
    )
    notify(APP_NAME, f"Unknown command. Prefixes: {available}")


# ============================================================
# Interceptor
# ============================================================

def _do_intercept() -> None:
    try:
        time.sleep(0.5)

        TUI.separator()
        TUI.status("⌨", "Hotkey triggered — reading selection...", TUI.CYAN)

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
    threading.Thread(target=_do_intercept, daemon=True).start()


# ============================================================
# Main Entry Point
# ============================================================

def main() -> None:
    print("\033[2J\033[3J\033[H", end="", flush=True)

    TUI.banner()

    # Environment box
    TUI.box("Environment", [
        f"  {TUI.BOLD}Session{TUI.RESET}    {_SESSION_TYPE}",
        f"  {TUI.BOLD}Display{TUI.RESET}    {_DISPLAY}",
        f"  {TUI.BOLD}Wayland{TUI.RESET}    {'Yes' if _IS_WAYLAND else 'No'}",
        f"  {TUI.BOLD}User{TUI.RESET}       {_SUDO_USER or os.environ.get('USER', '?')}",
        f"  {TUI.BOLD}Config{TUI.RESET}     {_CONFIG_PATH if _CONFIG_PATH.exists() else 'defaults (no config.yaml)'}",
    ], TUI.GREEN)

    print()

    # Init LLM
    _init_llm()
    TUI.llm_status_box()

    print()
    TUI.commands_table()
    print()
    TUI.keybind_table()
    print()

    # Register hotkeys
    keyboard.add_hotkey(HOTKEY, on_hotkey_triggered)
    keyboard.add_hotkey(UNDO_HOTKEY, on_undo_triggered)

    notify(
        "Action Middleware Active",
        f"{HOTKEY.upper()} to intercept | {UNDO_HOTKEY.upper()} to undo | Ctrl+C to exit",
    )

    TUI.separator()
    TUI.success("Listening for hotkeys...")
    cmd_count = len(CONFIG.get("commands", {}))
    llm_label = f"LLM: {_llm_provider}" if _llm_ready else "Mock mode"
    TUI.status("", f"{TUI.DIM}{cmd_count} commands loaded | {llm_label} | Select text and press {HOTKEY.upper()}{TUI.RESET}")
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
