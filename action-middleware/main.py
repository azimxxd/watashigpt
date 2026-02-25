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
import json
import base64
import hashlib
import yaml
import tty
import termios
import select
import argparse
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

_usage_counts: dict[str, int] = {}
_usage_lock = threading.Lock()

_activity_log: list[dict] = []
_activity_lock = threading.Lock()
_ACTIVITY_MAX = 5

_micro_log: list[str] = []
_micro_log_lock = threading.Lock()
_MICRO_LOG_MAX = 3


# ============================================================
# Config Hot-Reload
# ============================================================

def _reload_config() -> None:
    """Reload config.yaml and update CONFIG commands in place."""
    try:
        new_cfg = load_config()
        CONFIG["commands"] = new_cfg.get("commands", CONFIG["commands"])
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


def _llm_setup_prompt() -> None:
    """Interactive terminal prompt for LLM configuration with arrow-key selection."""
    llm_cfg = CONFIG.get("llm", {})
    has_config = bool(llm_cfg.get("provider", "").strip())
    if has_config:
        return

    c = TUI.CYAN
    r = TUI.RESET
    b = TUI.BOLD
    d = TUI.DIM
    g = TUI.GREEN

    print()
    TUI.box("LLM Setup", [
        f"  {d}LLM enables smart commands: summarize, rewrite, explain{r}",
        f"  {d}Without LLM, commands run in mock mode (instant, offline){r}",
        f"",
        f"  {b}Select a provider:{r}",
    ], TUI.CYAN)

    options = ["groq", "openai", "skip"]

    if sys.stdin.isatty():
        choice = TUI.selector(options)
    else:
        print(f"  {d}[1] groq  [2] openai  [3] skip{r}")
        try:
            raw = input(f"  {c}Choice (1-3):{r} ").strip()
        except (EOFError, KeyboardInterrupt):
            raw = "3"
        choice = {"1": 0, "2": 1, "3": 2}.get(raw)

    if choice is None or choice == 2:
        print(f"  {d}Launching in mock mode.{r}\n")
        return

    provider = options[choice]

    print(f"  {d}Enter your {provider} API key:{r}")
    try:
        api_key = input(f"  {c}{b}API Key:{r} ").strip()
    except (EOFError, KeyboardInterrupt):
        print(f"\n  {d}Cancelled. Launching in mock mode.{r}\n")
        return

    if not api_key:
        print(f"  {TUI.RED}No API key provided. Launching in mock mode.{r}\n")
        return

    default_model = "llama-3.3-70b-versatile" if provider == "groq" else "gpt-4o-mini"
    try:
        model = input(f"  {c}{b}Model{r} {d}[{default_model}]{r}{c}{b}:{r} ").strip()
    except (EOFError, KeyboardInterrupt):
        model = ""
    if not model:
        model = default_model

    CONFIG["llm"]["provider"] = provider
    CONFIG["llm"]["api_key"] = api_key
    CONFIG["llm"]["model"] = model

    print(f"\n  {g}✓ LLM configured: {provider}/{model} (session only){r}\n")


def _init_llm_client(provider: str, api_key: str, model: str):
    """Create an OpenAI client for the given provider. Returns (client, model) or (None, "")."""
    try:
        from openai import OpenAI

        if provider == "groq":
            client = OpenAI(api_key=api_key, base_url="https://api.groq.com/openai/v1")
            return client, model or "llama-3.3-70b-versatile"
        elif provider == "openai":
            client = OpenAI(api_key=api_key)
            return client, model or "gpt-4o-mini"
        else:
            TUI.warn(f"Unknown LLM provider: '{provider}'.")
            return None, ""
    except ImportError:
        TUI.warn("openai package not installed. Run: pip install openai")
        return None, ""
    except Exception as exc:
        TUI.error(f"LLM client init failed for {provider}: {exc}")
        return None, ""


def _init_llm() -> None:
    """Initialize LLM client from config. Sets _llm_ready=True on success."""
    global _llm_client, _llm_ready, _llm_provider, _llm_model
    global _llm_fallback_client, _llm_fallback_ready, _llm_fallback_provider, _llm_fallback_model

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

    client, resolved_model = _init_llm_client(provider, api_key, model)
    if client:
        _llm_client = client
        _llm_model = resolved_model
        _llm_provider = provider
        _llm_ready = True

    # Initialize fallback provider if configured
    fb_cfg = llm_cfg.get("fallback", {})
    fb_provider = fb_cfg.get("provider", "").strip().lower() if isinstance(fb_cfg, dict) else ""
    fb_api_key = fb_cfg.get("api_key", "").strip() if isinstance(fb_cfg, dict) else ""
    fb_model = fb_cfg.get("model", "").strip() if isinstance(fb_cfg, dict) else ""

    if not fb_api_key:
        fb_api_key = api_key  # reuse primary key if not specified

    if fb_provider and fb_provider != provider:
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

    if not _llm_ready or not _llm_client:
        _last_llm_provider_used = "mock"
        return _mock_llm_call(prompt)

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

        _last_llm_provider_used = "mock"
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
    def _read_key(cls) -> str:
        """Read a single keypress in raw mode. Returns 'left', 'right', 'enter', etc."""
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
        commands = CONFIG.get("commands", {})
        lines = []
        for name, cmd in commands.items():
            prefixes = ", ".join(cmd.get("prefixes", []))
            keywords = ", ".join(cmd.get("keywords", [])[:3])

            if cmd.get("llm_required"):
                badge = f" {cls.MAGENTA}{cls.BOLD}[LLM]{cls.RESET}"
            else:
                badge = f" {cls.CYAN}{cls.BOLD}[FAST]{cls.RESET}"

            count = _usage_counts.get(name, 0)
            counter = f" {cls.DIM}×{count}{cls.RESET}"

            lines.append(
                f"  {cls.CYAN}{cls.BOLD}{name:<12}{cls.RESET} "
                f"{cls.DIM}{prefixes:<20}{cls.RESET} "
                f"{cls.DIM}{keywords}{cls.RESET}"
                f"{badge}{counter}"
            )
        cls.box("Commands", lines, cls.CYAN)

    @classmethod
    def llm_status_box(cls) -> None:
        if _llm_ready:
            lines = [
                f"  {cls.GREEN}{cls.BOLD}LIVE{cls.RESET}    {cls.DIM}Provider: {_llm_provider}{cls.RESET}",
                f"          {cls.DIM}Model: {_llm_model}{cls.RESET}",
            ]
            if _llm_fallback_ready:
                lines.append(
                    f"          {cls.DIM}Fallback: {_llm_fallback_provider} / {_llm_fallback_model}{cls.RESET}"
                )
            cls.box("LLM", lines, cls.GREEN)
        else:
            lines = [
                f"  {cls.YELLOW}{cls.BOLD}MOCK MODE{cls.RESET}",
                f"  {cls.DIM}Set provider + api_key in config.yaml for live LLM{cls.RESET}",
            ]
            cls.box("LLM", lines, cls.YELLOW)

    @classmethod
    def activity_entry(cls, cmd_name: str, input_text: str, output_text: str,
                       duration: float, is_llm: bool = False, is_error: bool = False) -> None:
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

        line = (
            f"  {cls.DIM}{ts}{cls.RESET}  "
            f"{color}{cls.BOLD}{cmd_name.upper():<10}{cls.RESET}  "
            f"{cls.DIM}\"{inp}\" → \"{out}\"{cls.RESET}   "
            f"{check} {cls.DIM}{duration:.1f}s{cls.RESET}"
        )
        cls._print(line)

    @classmethod
    def activity_placeholder(cls) -> None:
        """Show empty activity feed at startup."""
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
        cls._print(entry)


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
    if _chain_suppress_paste:
        # Intermediate chain step — store result but don't paste
        TUI.success("Chain step complete (output passed to next step)")
        return
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
        count = len(_undo_stack)
    TUI.micro_log(f"Undo stack: {TUI.YELLOW}×{count}{TUI.RESET} available")


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
        with _undo_lock:
            remaining = len(_undo_stack)
        if remaining == 0:
            TUI.micro_log(f"Undo applied — stack {TUI.DIM}empty{TUI.RESET}")
        else:
            TUI.micro_log(f"Undo applied — stack {TUI.YELLOW}×{remaining}{TUI.RESET} remaining")
        notify("Undo", "Reverted last replacement")

    except Exception as exc:
        TUI.error(f"Undo error: {exc}")
        TUI.micro_log(f"{TUI.RED}Undo error: {exc}{TUI.RESET}")


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
    cmd_model = cmd_config.get("model", "")

    TUI.status("🤖", f"Processing with {'LLM' if _llm_ready else 'Mock'}...", TUI.CYAN)
    notify(APP_NAME, "Processing...")

    if _llm_ready:
        result = _llm_call(prompt, model=cmd_model)
    else:
        result = _mock_llm_call(prompt)

    _push_undo(full_text, result)
    _replace_selection(result)

    truncated = result[:80] + ("..." if len(result) > 80 else "")
    provider_tag = f" [{_last_llm_provider_used}]" if _last_llm_provider_used else ""
    TUI.action("🤖", cmd_name.upper(), f"\"{truncated}\"{provider_tag}")
    notify(cmd_name, f"Done: \"{truncated}\"")


def handle_fmt(text: str, full_text: str, cmd_config: dict) -> None:
    """Auto-format JSON text with indentation."""
    content = text.strip()
    try:
        parsed = json.loads(content)
        result = json.dumps(parsed, indent=2, ensure_ascii=False)

        _push_undo(full_text, result)
        _replace_selection(result)

        TUI.action("🔧", "FMT", f"Formatted JSON ({len(content)} → {len(result)} chars)")
        notify("Format", "JSON formatted successfully")
    except json.JSONDecodeError as exc:
        TUI.error(f"FMT: invalid JSON — {exc}")
        notify("Format Error", f"FMT: invalid JSON — {exc}")


def handle_count(text: str, full_text: str, cmd_config: dict) -> None:
    """Word/char/line stats — notification only, no clipboard replacement."""
    content = text.strip()
    words = len(content.split())
    chars = len(content)
    lines = content.count('\n') + 1

    stats = f"Words: {words} | Chars: {chars} | Lines: {lines}"
    TUI.action("📊", "COUNT", stats)
    notify("Text Stats", stats)


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
        result = base64.b64decode(content).decode()

        _push_undo(full_text, result)
        _replace_selection(result)

        TUI.action("🔓", "DECODE", f"Decoded {len(content)} chars → {len(result)} chars")
        notify("Base64 Decode", f"{result[:80]}")
    except Exception as exc:
        TUI.error(f"DECODE: invalid base64 — {exc}")
        notify("Decode Error", f"Invalid base64 input: {exc}")


def handle_hash(text: str, full_text: str, cmd_config: dict) -> None:
    """SHA256 digest of selected text."""
    content = text.strip()
    digest = hashlib.sha256(content.encode()).hexdigest()

    _push_undo(full_text, digest)
    _replace_selection(digest)

    TUI.action("🔑", "HASH", f"SHA256: {digest[:32]}...")
    notify("SHA256", digest)


import re as _re

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

    prompt_template = cmd_config.get("llm_prompt", "Translate to {lang}: {text}")
    prompt = prompt_template.format(lang=lang_code, text=body)
    cmd_model = cmd_config.get("model", "")

    TUI.status("🌐", f"Translating to {lang_code} with {'LLM' if _llm_ready else 'Mock'}...", TUI.CYAN)
    notify(APP_NAME, f"Translating to {lang_code}...")

    result = _llm_call(prompt, model=cmd_model) if _llm_ready else _mock_llm_call(prompt)

    _push_undo(full_text, result)
    _replace_selection(result)

    truncated = result[:80] + ("..." if len(result) > 80 else "")
    provider_tag = f" [{_last_llm_provider_used}]" if _last_llm_provider_used else ""
    TUI.action("🌐", f"TRANS→{lang_code}", f"\"{truncated}\"{provider_tag}")
    notify(f"Translated ({lang_code})", truncated)


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
    "trans": handle_trans,
}


# ============================================================
# History Log
# ============================================================

_HISTORY_PATH = Path.home() / ".watashigpt_history.jsonl"


def _log_history(command: str, input_text: str, output_text: str, duration_ms: int) -> None:
    """Append a JSON line to ~/.watashigpt_history.jsonl."""
    try:
        entry = json.dumps({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "command": command,
            "input": input_text[:500],
            "output": output_text[:500],
            "duration_ms": duration_ms,
        }, ensure_ascii=False)
        with open(_HISTORY_PATH, "a") as f:
            f.write(entry + "\n")
    except Exception as exc:
        TUI.warn(f"History log write failed: {exc}")


# ============================================================
# Router — 3-tier: Prefix → Keyword → LLM → Fallback
# ============================================================

def dispatch(cmd_name: str, payload: str, full_text: str, cmd_config: dict) -> None:
    """Dispatch to the correct handler for a matched command."""
    with _usage_lock:
        _usage_counts[cmd_name] = _usage_counts.get(cmd_name, 0) + 1

    is_llm = cmd_config.get("llm_required", False) or cmd_name not in _BUILTIN_HANDLERS
    start_time = time.time()

    try:
        if cmd_name in _BUILTIN_HANDLERS:
            _BUILTIN_HANDLERS[cmd_name](payload, full_text, cmd_config)
        elif cmd_config.get("llm_required"):
            handle_llm_command(payload, full_text, cmd_config)
        else:
            handle_llm_command(payload, full_text, cmd_config)

        duration = time.time() - start_time
        with _undo_lock:
            output = _undo_stack[-1]["replacement"] if _undo_stack else "(done)"
        TUI.activity_entry(cmd_name, payload, output, duration, is_llm=is_llm)
        _log_history(cmd_name, payload, output, int(duration * 1000))

    except Exception as exc:
        duration = time.time() - start_time
        TUI.activity_entry(cmd_name, payload, str(exc), duration, is_error=True)
        _log_history(cmd_name, payload, f"ERROR: {exc}", int(duration * 1000))
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
                    with _undo_lock:
                        current_input = _undo_stack[-1]["replacement"] if _undo_stack else current_input
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
    notify(APP_NAME, f"Unknown command. Prefixes: {available}")


# ============================================================
# Interceptor
# ============================================================

def _do_intercept() -> None:
    try:
        time.sleep(0.5)

        TUI.separator()
        TUI.status("⌨", "Hotkey triggered — reading selection...", TUI.CYAN)
        TUI.micro_log(f"Hotkey triggered — reading selection...")

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
        TUI.micro_log(f"{TUI.RED}Error: {exc}{TUI.RESET}")
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
    ], TUI.CYAN)

    print()

    # Initialize usage counters
    for cmd_name in CONFIG.get("commands", {}):
        _usage_counts[cmd_name] = 0

    # Interactive LLM setup (only if not already configured)
    _llm_setup_prompt()

    # Init LLM
    _init_llm()
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

    # Register hotkeys
    keyboard.add_hotkey(HOTKEY, on_hotkey_triggered)
    keyboard.add_hotkey(UNDO_HOTKEY, on_undo_triggered)

    notify(
        "Action Middleware Active",
        f"{HOTKEY.upper()} to intercept | {UNDO_HOTKEY.upper()} to undo | Ctrl+C to exit",
    )

    TUI.separator()
    cmd_count = len(CONFIG.get("commands", {}))
    llm_label = f"LLM: {_llm_provider}" if _llm_ready else "Mock mode"
    TUI.micro_log(f"{TUI.GREEN}✓{TUI.RESET} Listening for hotkeys...")
    TUI.micro_log(f"{cmd_count} commands loaded | {llm_label} | {HOTKEY.upper()} to intercept")
    TUI.micro_log(f"Ready.")
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


def install_systemd_service() -> None:
    """Generate and install a systemd unit file for WatashiGPT."""
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
Description=WatashiGPT Action Middleware
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

    unit_path = Path("/etc/systemd/system/watashigpt.service")
    unit_path.write_text(unit_content)
    print(f"{TUI.GREEN}✓{TUI.RESET} Wrote {unit_path}")

    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "watashigpt"], check=True)
    print(f"{TUI.GREEN}✓{TUI.RESET} Service enabled")
    print()
    print(f"  Start now with:  {TUI.CYAN}systemctl start watashigpt{TUI.RESET}")
    print(f"  Check status:    {TUI.CYAN}systemctl status watashigpt{TUI.RESET}")
    print(f"  View logs:       {TUI.CYAN}journalctl -u watashigpt -f{TUI.RESET}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="WatashiGPT Action Middleware")
    parser.add_argument("--install", action="store_true", help="Install as a systemd service")
    args = parser.parse_args()

    if args.install:
        install_systemd_service()
    else:
        main()
