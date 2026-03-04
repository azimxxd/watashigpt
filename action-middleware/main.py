# Action Middleware — OS-level background assistant
#
# Run with: sudo -E python main.py
#   -E preserves DISPLAY, WAYLAND_DISPLAY, DBUS_SESSION_BUS_ADDRESS
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
import json
import base64
import hashlib
import yaml
import tty
import termios
import select
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

try:
    import tkinter as tk
    from tkinter import font as tkfont
    _TKINTER_AVAILABLE = True
except ImportError:
    _TKINTER_AVAILABLE = False

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
_clipboard_stack: list[str] = []   # For STACK/POP
_CLIPS_PATH = Path.home() / ".watashigpt_clips.json"

_popup_queue: queue.Queue = queue.Queue()  # Hotkey thread → main thread for popup
_popup_trigger: str = "prefix"  # Set per-dispatch: "prefix" or "popup"


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

LLM_MODE = "mock"  # "live" or "mock" — set during startup, never changes after


def _save_llm_config(provider: str, api_key: str, model: str) -> None:
    """Persist LLM provider/key/model to config.yaml."""
    try:
        if _CONFIG_PATH.exists():
            with open(_CONFIG_PATH, "r") as f:
                data = yaml.safe_load(f) or {}
        else:
            data = {}
        if "llm" not in data:
            data["llm"] = {}
        data["llm"]["provider"] = provider
        data["llm"]["api_key"] = api_key
        data["llm"]["model"] = model
        with open(_CONFIG_PATH, "w") as f:
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
    except Exception as exc:
        TUI.warn(f"Could not save LLM config: {exc}")


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

    options = ["groq", "openai", "gemini", "openrouter", "github", "skip → mock"]

    if sys.stdin.isatty():
        choice = TUI.selector(options)
    else:
        labels = "  ".join(f"[{i+1}] {o}" for i, o in enumerate(options))
        print(f"  {d}{labels}{r}")
        try:
            raw = input(f"  {c}Choice (1-{len(options)}):{r} ").strip()
        except (EOFError, KeyboardInterrupt):
            raw = str(len(options))
        choice = {str(i+1): i for i in range(len(options))}.get(raw)

    skip_index = len(options) - 1
    if choice is None or choice == skip_index:
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

    _PROVIDER_DEFAULTS = {
        "groq": "llama-3.3-70b-versatile",
        "openai": "gpt-4o-mini",
        "gemini": "gemini-2.0-flash",
        "openrouter": "meta-llama/llama-3.3-70b-instruct",
        "github": "gpt-4o-mini",
    }
    default_model = _PROVIDER_DEFAULTS.get(provider, "gpt-4o-mini")
    try:
        model = input(f"  {c}{b}Model{r} {d}[{default_model}]{r}{c}{b}:{r} ").strip()
    except (EOFError, KeyboardInterrupt):
        model = ""
    if not model:
        model = default_model

    CONFIG["llm"]["provider"] = provider
    CONFIG["llm"]["api_key"] = api_key
    CONFIG["llm"]["model"] = model

    # Persist to config.yaml so subsequent runs skip the selector
    _save_llm_config(provider, api_key, model)

    print(f"\n  {g}✓ LLM configured: {provider}/{model}{r}\n")


_PROVIDER_BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta/openai/",
    "openrouter": "https://openrouter.ai/api/v1",
    "github": "https://models.inference.ai.azure.com",
}

_PROVIDER_DEFAULT_MODELS = {
    "groq": "llama-3.3-70b-versatile",
    "openai": "gpt-4o-mini",
    "gemini": "gemini-2.0-flash",
    "openrouter": "meta-llama/llama-3.3-70b-instruct",
    "github": "gpt-4o-mini",
}


def _init_llm_client(provider: str, api_key: str, model: str):
    """Create an OpenAI client for the given provider. Returns (client, model) or (None, "")."""
    try:
        from openai import OpenAI

        default_model = _PROVIDER_DEFAULT_MODELS.get(provider, "gpt-4o-mini")
        resolved_model = model or default_model

        if provider == "openai":
            client = OpenAI(api_key=api_key)
        elif provider in _PROVIDER_BASE_URLS:
            client = OpenAI(api_key=api_key, base_url=_PROVIDER_BASE_URLS[provider])
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
    """Initialize LLM client from config. Sets _llm_ready=True and LLM_MODE='live' on success."""
    global _llm_client, _llm_ready, _llm_provider, _llm_model
    global _llm_fallback_client, _llm_fallback_ready, _llm_fallback_provider, _llm_fallback_model
    global LLM_MODE

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
        LLM_MODE = "live"

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
    """Ask LLM to classify text intent. Returns {"name": ..., "payload": ..., "confidence": float} or None."""
    if LLM_MODE == "mock" or not _llm_ready:
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
    def header_line(cls) -> None:
        """Compact single-line header shown after banner collapses."""
        elapsed = int(time.time() - _start_time)
        h, rem = divmod(elapsed, 3600)
        m, s = divmod(rem, 60)
        uptime = f"{h}:{m:02d}:{s:02d}"

        if LLM_MODE == "live":
            mode_str = f"{cls.GREEN}live{cls.RESET} {cls.DIM}· {_llm_provider}/{_llm_model}{cls.RESET}"
        else:
            mode_str = f"{cls.YELLOW}mock{cls.RESET}"

        cmd_count = len(CONFIG.get("commands", {}))
        line = (
            f"  {cls.MAGENTA}{cls.BOLD}▶ WATASHIGPT{cls.RESET}  "
            f"{cls.DIM}|{cls.RESET}  {mode_str}  "
            f"{cls.DIM}|{cls.RESET}  {cls.DIM}{cmd_count} commands{cls.RESET}  "
            f"{cls.DIM}|{cls.RESET}  {cls.DIM}uptime: {uptime}{cls.RESET}"
        )
        cls._print(line)

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
            is_llm_cmd = cmd.get("llm_required", False)
            dimmed = is_llm_cmd and LLM_MODE == "mock"

            if is_llm_cmd:
                badge = f" {cls.MAGENTA}{cls.BOLD}[LLM]{cls.RESET}"
                if dimmed:
                    badge += f" {cls.YELLOW}[MOCK]{cls.RESET}"
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
        if LLM_MODE == "live":
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
                f"  {cls.DIM}LLM commands return placeholders · no API calls{cls.RESET}",
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
# Command Picker — Tkinter Popup
# ============================================================

_TONE_STYLES = ["casual", "formal", "aggressive", "empathetic", "confident", "sarcastic", "diplomatic"]
_TRANS_LANGS = [
    ("Japanese", "JP", "\U0001f1ef\U0001f1f5"), ("Spanish", "ES", "\U0001f1ea\U0001f1f8"),
    ("French", "FR", "\U0001f1eb\U0001f1f7"), ("German", "DE", "\U0001f1e9\U0001f1ea"),
    ("Chinese", "ZH", "\U0001f1e8\U0001f1f3"), ("Arabic", "AR", "\U0001f1f8\U0001f1e6"),
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

        def __init__(self, selected_text: str, commands: dict):
            self._text = selected_text
            self._commands = commands
            self._cmd_list: list[tuple[str, dict]] = list(commands.items())
            self._filtered: list[tuple[str, dict]] = list(self._cmd_list)
            self._selected_idx = 0
            self._result: tuple | None = None  # (cmd_name, cmd_config) or None
            self._submenu: str | None = None  # "tone" or "trans" or None
            self._sub_items: list = []
            self._sub_selected = 0
            self._custom_entry = None
            self._row_widgets: list = []

            self._root = tk.Tk()
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
            # Preview
            preview = self._text[:60] + ("..." if len(self._text) > 60 else "")
            tk.Label(self._root, text=f'"{preview}"', bg=self.BG, fg=self.PREVIEW_FG,
                     font=self._font_small, anchor="w", padx=8, pady=4
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

            items = self._filtered
            for i, (name, cmd) in enumerate(items):
                row = tk.Frame(self._inner_frame, bg=self.BG_ROW, cursor="hand2")
                row.pack(fill="x", padx=2, pady=1)
                self._row_widgets.append(row)

                is_llm = cmd.get("llm_required", False)
                is_mock_llm = is_llm and LLM_MODE == "mock"

                # Number
                num_label = str(i + 1) if i < 9 else " "
                fg_main = self.FG_DIM if is_mock_llm else self.FG
                tk.Label(row, text=num_label, bg=self.BG_ROW, fg=self.FG_DIM,
                         font=self._font_small, width=2).pack(side="left", padx=(6, 2))

                # Name
                display_name = name.replace("_", " ").title()
                tk.Label(row, text=display_name, bg=self.BG_ROW, fg=fg_main,
                         font=self._font_bold, anchor="w", width=16).pack(side="left")

                # Description
                desc = cmd.get("description", "")[:30]
                tk.Label(row, text=desc, bg=self.BG_ROW, fg=self.FG_DIM,
                         font=self._font_small, anchor="w").pack(side="left", fill="x", expand=True)

                # Badge
                if is_mock_llm:
                    badge_text, badge_fg = "[MOCK]", self.BADGE_MOCK
                elif is_llm:
                    badge_text, badge_fg = "[LLM]", self.BADGE_LLM
                else:
                    badge_text, badge_fg = "[FAST]", self.BADGE_FAST
                tk.Label(row, text=badge_text, bg=self.BG_ROW, fg=badge_fg,
                         font=self._font_small).pack(side="right", padx=(4, 8))

                # Highlight
                if i == self._selected_idx:
                    self._set_row_bg(row, self.BG_SELECTED)

                # Mouse bindings
                idx = i
                row.bind("<Enter>", lambda e, r=row, j=idx: self._on_row_hover(r, j))
                row.bind("<Leave>", lambda e, r=row, j=idx: self._on_row_leave(r, j))
                row.bind("<Button-1>", lambda e, j=idx: self._on_row_click(j))
                for child in row.winfo_children():
                    child.bind("<Enter>", lambda e, r=row, j=idx: self._on_row_hover(r, j))
                    child.bind("<Leave>", lambda e, r=row, j=idx: self._on_row_leave(r, j))
                    child.bind("<Button-1>", lambda e, j=idx: self._on_row_click(j))

            self._update_scroll_height()

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
            if idx >= len(self._filtered):
                return
            name, cmd = self._filtered[idx]

            # MOCK mode: block LLM commands
            is_llm = cmd.get("llm_required", False)
            if is_llm and LLM_MODE == "mock":
                self._result = None
                self._root.destroy()
                return

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
                self._root.mainloop()
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
    picker = CommandPicker(text, commands)
    result = picker.run()

    if result is None:
        # Check if an LLM command was blocked in mock mode
        TUI.micro_log(f"Command picker cancelled")
        return

    cmd_name, cmd_config, payload = result
    is_llm = cmd_config.get("llm_required", False)

    # Mock mode notification for LLM commands
    if is_llm and LLM_MODE == "mock":
        notify(APP_NAME, "LLM not configured — enable a provider at startup")
        TUI.warn("LLM not configured — command not applied")
        return

    _popup_trigger = "popup"
    TUI.status("\U0001f3af", f"Popup \u2192 {cmd_name}", TUI.GREEN)

    # Dispatch in a worker thread so we don't block the main loop
    def _run():
        try:
            dispatch(cmd_name, payload, text, cmd_config)
        except Exception as exc:
            TUI.error(f"Popup dispatch error: {exc}")

    threading.Thread(target=_run, daemon=True).start()


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

def _should_notify(is_error: bool = False) -> bool:
    """Check if a notification should be sent based on _current_notify_level."""
    level = _current_notify_level
    if level == "never":
        return False
    if level == "errors_only" and not is_error:
        return False
    return True


def notify(title: str, message: str, is_error: bool = False) -> None:
    if not _should_notify(is_error=is_error):
        return
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
        TUI.action("↩", "UNDO", "Restoring previous clipboard")
        clipboard_copy(entry["original"])
        time.sleep(CLIPBOARD_DELAY)
        keyboard.send("ctrl+v")

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
    result = f"[TEST OK] \"{content}\" | session={_SESSION_TYPE} | wayland={_IS_WAYLAND} | llm={LLM_MODE}"

    _push_undo(full_text, result)
    _replace_selection(result)

    TUI.action("🧪", "TEST", f"Input: \"{content}\"")
    TUI.success(f"Output: \"{result}\"")
    notify("Test", f"Pipeline OK: \"{content[:60]}\"")


def handle_llm_command(text: str, full_text: str, cmd_config: dict) -> None:
    """Generic handler for LLM-backed commands defined in config.yaml."""
    cmd_name = cmd_config.get("description", "LLM")

    # Mock mode: return placeholder immediately, no API call
    if LLM_MODE == "mock":
        result = f"[MOCK] {cmd_name}: (LLM not configured)"
        _push_undo(full_text, result)
        _replace_selection(result)
        TUI.action("🤖", cmd_name.upper(), f"\"{result}\" [mock]")
        notify(cmd_name, result)
        return

    prompt_template = cmd_config.get("llm_prompt", "Process this text: {text}")
    prompt = prompt_template.format(text=text.strip())
    cmd_model = cmd_config.get("model", "")

    TUI.status("🤖", f"Processing with LLM...", TUI.CYAN)
    notify(APP_NAME, "Processing...")

    result = _llm_call(prompt, model=cmd_model)

    _push_undo(full_text, result)
    _replace_selection(result)

    truncated = result[:80] + ("..." if len(result) > 80 else "")
    provider_tag = f" [{_last_llm_provider_used}]" if _last_llm_provider_used else ""
    TUI.action("🤖", cmd_name.upper(), f"\"{truncated}\"{provider_tag}")
    notify(cmd_name, f"Done: \"{truncated}\"")


def handle_fmt(text: str, full_text: str, cmd_config: dict) -> None:
    """Auto-format JSON or XML text with indentation."""
    content = text.strip()
    # Try JSON first
    try:
        parsed = json.loads(content)
        result = json.dumps(parsed, indent=2, ensure_ascii=False)

        _push_undo(full_text, result)
        _replace_selection(result)

        TUI.action("🔧", "FMT", f"Formatted JSON ({len(content)} → {len(result)} chars)")
        notify("Format", "JSON formatted successfully")
        return
    except json.JSONDecodeError:
        pass

    # Try XML
    try:
        import xml.dom.minidom
        dom = xml.dom.minidom.parseString(content)
        result = dom.toprettyxml(indent="  ")
        # Remove the XML declaration if it wasn't in the original
        if not content.strip().startswith("<?xml"):
            result = "\n".join(result.split("\n")[1:])
        result = result.strip()

        _push_undo(full_text, result)
        _replace_selection(result)

        TUI.action("🔧", "FMT", f"Formatted XML ({len(content)} → {len(result)} chars)")
        notify("Format", "XML formatted successfully")
        return
    except Exception:
        pass

    TUI.error("FMT: could not parse as JSON or XML")
    notify("Format Error", "FMT: could not parse as JSON or XML")


def handle_count(text: str, full_text: str, cmd_config: dict) -> None:
    """Word/char/line stats — notification only, no clipboard replacement."""
    content = text.strip()
    words = len(content.split())
    chars = len(content)
    lines = content.count('\n') + 1
    reading_min = max(1, round(words / 200))

    stats = f"Words: {words} | Chars: {chars} | Lines: {lines} | Reading time: ~{reading_min} min"
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


# ── PII patterns for REDACT ──
_PII_PATTERNS = [
    (_re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"), "[EMAIL]"),
    (_re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"), "[CARD]"),
    (_re.compile(r"\b(?:\+?\d{1,3}[-.\s]?)?(?:\(?\d{2,4}\)?[-.\s]?)?\d{3,4}[-.\s]?\d{4}\b"), "[PHONE]"),
    (_re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
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

    # Clean the expression: keep only math chars
    cleaned = _re.sub(r"[^0-9+\-*/().%^ ]", "", expr)
    cleaned = cleaned.replace("^", "**")
    if not cleaned.strip():
        return None

    try:
        # Parse as AST and validate — only allow math operations
        tree = ast.parse(cleaned, mode='eval')
        for node in ast.walk(tree):
            if isinstance(node, (ast.Expression, ast.BinOp, ast.UnaryOp, ast.Constant,
                                 ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow,
                                 ast.Mod, ast.FloorDiv, ast.USub, ast.UAdd)):
                continue
            return None  # Unsafe node type
        result = eval(compile(tree, "<calc>", "eval"))
        f = float(result)
        return str(int(f)) if f == int(f) else str(round(f, 10))
    except Exception:
        return None


def handle_calc(text: str, full_text: str, cmd_config: dict) -> None:
    """Safe math expression evaluator."""
    content = text.strip()
    result = _safe_eval_math(content)

    if result is None:
        TUI.error(f"CALC: could not evaluate \"{content[:60]}\"")
        notify("Calc Error", f"Could not evaluate: {content[:60]}")
        return

    _push_undo(full_text, result)
    _replace_selection(result)

    TUI.action("🧮", "CALC", f"\"{content[:40]}\" = {result}")
    notify("Calculator", f"{content[:40]} = {result}")


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
    """Escape special characters. Auto-detects context or uses explicit mode prefix."""
    content = text.strip()
    mode = None

    # Check for explicit mode prefix: html:, sql:, regex:
    for prefix in ("html:", "sql:", "regex:"):
        if content.lower().startswith(prefix):
            mode = prefix[:-1]
            content = content[len(prefix):].strip()
            break

    if mode is None:
        # Auto-detect context
        if "<" in content and ">" in content:
            mode = "html"
        elif "'" in content or ";" in content:
            mode = "sql"
        else:
            mode = "regex"

    if mode == "html":
        import html
        result = html.escape(content)
    elif mode == "sql":
        result = content.replace("'", "''").replace(";", "")
    elif mode == "regex":
        result = _re.escape(content)
    else:
        result = content

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
    """Named clipboard slots: save/load/list."""
    content = text.strip()

    # Parse sub-command
    parts = content.split(None, 1)
    sub = parts[0].lower() if parts else ""
    arg = parts[1].strip() if len(parts) > 1 else ""

    # Load existing clips
    clips = {}
    if _CLIPS_PATH.exists():
        try:
            clips = json.loads(_CLIPS_PATH.read_text())
        except Exception:
            pass

    if sub == "save" and arg:
        current = clipboard_paste()
        clips[arg] = current
        _CLIPS_PATH.write_text(json.dumps(clips, ensure_ascii=False, indent=2))
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


def handle_stack(text: str, full_text: str, cmd_config: dict) -> None:
    """Push current clipboard onto the stack."""
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

    # Mock mode
    if LLM_MODE == "mock":
        result = f"[MOCK] TONE→{style}: (LLM not configured)"
        _push_undo(full_text, result)
        _replace_selection(result)
        TUI.action("🎨", f"TONE→{style}", f"\"{result}\" [mock]")
        notify(f"Tone ({style})", result)
        return

    prompt = (
        f"Rewrite the following text in a {style} tone. "
        f"Return ONLY the rewritten text, nothing else:\n\n{body}"
    )
    cmd_model = cmd_config.get("model", "")

    TUI.status("🎨", f"Rewriting in {style} tone...", TUI.CYAN)
    notify(APP_NAME, f"Rewriting in {style} tone...")

    result = _llm_call(prompt, model=cmd_model)

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

    # Mock mode: return placeholder immediately, no API call
    if LLM_MODE == "mock":
        result = f"[MOCK] TRANS→{lang_code}: (LLM not configured)"
        _push_undo(full_text, result)
        _replace_selection(result)
        TUI.action("🌐", f"TRANS→{lang_code}", f"\"{result}\" [mock]")
        notify(f"Translated ({lang_code})", result)
        return

    prompt_template = cmd_config.get("llm_prompt", "Translate to {lang}: {text}")
    prompt = prompt_template.format(lang=lang_code, text=body)
    cmd_model = cmd_config.get("model", "")

    TUI.status("🌐", f"Translating to {lang_code} with LLM...", TUI.CYAN)
    notify(APP_NAME, f"Translating to {lang_code}...")

    result = _llm_call(prompt, model=cmd_model)

    _push_undo(full_text, result)
    _replace_selection(result)

    truncated = result[:80] + ("..." if len(result) > 80 else "")
    provider_tag = f" [{_last_llm_provider_used}]" if _last_llm_provider_used else ""
    TUI.action("🌐", f"TRANS→{lang_code}", f"\"{truncated}\"{provider_tag}")
    notify(f"Translated ({lang_code})", truncated)


# ============================================================
# WIKI / DEFINE — Web Lookup Commands
# ============================================================

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
        req = urllib.request.Request(url, headers={"User-Agent": "WatashiGPT/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

        extract = data.get("extract", "")
        title = data.get("title", query)

        if not extract:
            TUI.warn(f"WIKI: no results for \"{query}\"")
            notify("Wikipedia", f"No results for \"{query}\"")
            return

        # Truncate for notification (max ~300 chars)
        summary = extract[:300] + ("..." if len(extract) > 300 else "")
        TUI.action("📖", "WIKI", f"{title}: {summary[:80]}...")
        notify(f"Wikipedia: {title}", summary)

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
        req = urllib.request.Request(url, headers={"User-Agent": "WatashiGPT/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())

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
        TUI.action("📚", "DEFINE", f"{word}: {result[:80]}")
        notify(f"Define: {word}", result[:300])

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
}


# ============================================================
# History Log
# ============================================================

_HISTORY_PATH = Path.home() / ".watashigpt_history.jsonl"


def _log_history(command: str, input_text: str, output_text: str, duration_ms: int) -> None:
    """Append a JSON line to ~/.watashigpt_history.jsonl."""
    try:
        provider = _last_llm_provider_used or _llm_provider or "builtin"
        entry = json.dumps({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "command": command,
            "input": input_text[:500],
            "output": output_text[:500],
            "duration_ms": duration_ms,
            "provider": provider,
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
        if cmd_name in _BUILTIN_HANDLERS:
            _BUILTIN_HANDLERS[cmd_name](payload, full_text, cmd_config)
        elif cmd_config.get("llm_required"):
            handle_llm_command(payload, full_text, cmd_config)
        else:
            handle_llm_command(payload, full_text, cmd_config)

        duration = time.time() - start_time
        with _undo_lock:
            output = _undo_stack[-1]["replacement"] if _undo_stack else "(done)"
        TUI.activity_entry(cmd_name, payload, output, duration, is_llm=is_llm,
                           trigger=_popup_trigger)
        _log_history(cmd_name, payload, output, int(duration * 1000))

    except Exception as exc:
        duration = time.time() - start_time
        TUI.activity_entry(cmd_name, payload, str(exc), duration, is_error=True,
                           trigger=_popup_trigger)
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

    # Tier 3: LLM intent classification (live mode only)
    if LLM_MODE == "live" and _llm_ready:
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

        interaction_mode = CONFIG.get("interaction_mode", "both")
        commands = CONFIG.get("commands", {})

        # If prefix/both mode and text has a known prefix or chain → route directly
        if interaction_mode in ("prefix", "both"):
            if _resolve_prefix(text, commands) or _parse_chain(text, commands):
                global _popup_trigger
                _popup_trigger = "prefix"
                route(text)
                return

        # If popup/both mode and tkinter available → queue for main thread popup
        if interaction_mode in ("popup", "both"):
            if _TKINTER_AVAILABLE:
                _popup_queue.put(text)
                TUI.micro_log(f"Opening command picker...")
                return
            else:
                TUI.warn("tkinter unavailable — falling back to prefix routing")

        # Fallback: route normally (keyword/LLM classification)
        _popup_trigger = "prefix"
        route(text)

    except Exception as exc:
        TUI.error(f"Interceptor error: {exc}")
        TUI.micro_log(f"{TUI.RED}Error: {exc}{TUI.RESET}")
        notify(APP_NAME, f"Error: {exc}")


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
    """Dump the full activity log for the current session to a markdown file."""
    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    export_path = Path.home() / f"watashigpt_session_{ts}.md"

    lines = [
        f"# WatashiGPT Session Export",
        f"",
        f"- **Date**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- **Mode**: {LLM_MODE}",
    ]
    if LLM_MODE == "live":
        lines.append(f"- **Provider**: {_llm_provider}/{_llm_model}")
    lines.append("")
    lines.append("## Activity Log")
    lines.append("")

    # Read history from the JSONL file for this session
    session_start = datetime.fromtimestamp(_start_time).isoformat(timespec="seconds")
    try:
        if _HISTORY_PATH.exists():
            with open(_HISTORY_PATH, "r") as f:
                count = 0
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                        if entry.get("ts", "") >= session_start:
                            count += 1
                            lines.append(
                                f"| {entry['ts']} | `{entry['command']}` | "
                                f"{entry['input'][:60]} | {entry['output'][:60]} | "
                                f"{entry['duration_ms']}ms |"
                            )
                    except (json.JSONDecodeError, KeyError):
                        continue
                if count == 0:
                    lines.append("_No activity recorded this session._")
                else:
                    # Insert table header before entries
                    header_idx = lines.index("## Activity Log") + 2
                    lines.insert(header_idx, "| Time | Command | Input | Output | Duration |")
                    lines.insert(header_idx + 1, "|------|---------|-------|--------|----------|")
        else:
            lines.append("_No history file found._")
    except Exception as exc:
        lines.append(f"_Error reading history: {exc}_")

    lines.append("")

    try:
        export_path.write_text("\n".join(lines))
        TUI.micro_log(f"{TUI.GREEN}✓{TUI.RESET} Session exported → {TUI.CYAN}{export_path}{TUI.RESET}")
    except Exception as exc:
        TUI.error(f"Session export failed: {exc}")


# ============================================================
# Main Entry Point
# ============================================================

def main(keep_banner: bool = False) -> None:
    global _start_time
    _start_time = time.time()

    print("\033[2J\033[3J\033[H", end="", flush=True)

    TUI.banner()

    # Initialize usage counters
    for cmd_name in CONFIG.get("commands", {}):
        _usage_counts[cmd_name] = 0

    # Interactive LLM setup (only if not already configured)
    _llm_setup_prompt()

    # Init LLM
    _init_llm()

    # Environment box (rendered after LLM init so Mode is known)
    config_val = f"{TUI.DIM}{_CONFIG_PATH if _CONFIG_PATH.exists() else 'defaults (no config.yaml)'}{TUI.RESET}"
    if LLM_MODE == "live":
        mode_val = f"{TUI.GREEN}LIVE ({_llm_provider}){TUI.RESET}"
    else:
        mode_val = f"{TUI.YELLOW}MOCK{TUI.RESET}"
    TUI.box("Environment", [
        f"  {TUI.DIM}Session{TUI.RESET}    {TUI.CYAN}{_SESSION_TYPE}{TUI.RESET}",
        f"  {TUI.DIM}Display{TUI.RESET}    {TUI.CYAN}{_DISPLAY}{TUI.RESET}",
        f"  {TUI.DIM}Wayland{TUI.RESET}    {TUI.CYAN}{'Yes' if _IS_WAYLAND else 'No'}{TUI.RESET}",
        f"  {TUI.DIM}User{TUI.RESET}       {TUI.CYAN}{_SUDO_USER or os.environ.get('USER', '?')}{TUI.RESET}",
        f"  {TUI.DIM}Mode{TUI.RESET}       {mode_val}",
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

    # Register hotkeys
    keyboard.add_hotkey(HOTKEY, on_hotkey_triggered)
    keyboard.add_hotkey(UNDO_HOTKEY, on_undo_triggered)

    notify(
        "Action Middleware Active",
        f"{HOTKEY.upper()} to intercept | {UNDO_HOTKEY.upper()} to undo | Ctrl+C to exit",
    )

    TUI.separator()
    cmd_count = len(CONFIG.get("commands", {}))
    llm_label = f"LLM: {_llm_provider}" if LLM_MODE == "live" else "Mock mode"
    TUI.micro_log(f"{TUI.GREEN}✓{TUI.RESET} Listening for hotkeys...")
    TUI.micro_log(f"{cmd_count} commands loaded | {llm_label} | {HOTKEY.upper()} to intercept")
    TUI.micro_log(f"{TUI.DIM}/ = search  S = export session  Ctrl+C = exit{TUI.RESET}")
    print()

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

                # Check popup queue
                try:
                    popup_text = _popup_queue.get_nowait()
                    # Restore terminal for tkinter
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                    _handle_popup(popup_text)
                    # Restore cbreak
                    tty.setcbreak(fd)
                except queue.Empty:
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
        url = "https://api.github.com/repos/azimxxd/watashigpt/releases/latest"
        req = urllib.request.Request(url, headers={"User-Agent": "WatashiGPT"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
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
    history_path = Path.home() / ".watashigpt_history.jsonl"
    if not history_path.exists():
        print(f"{TUI.YELLOW}No history file found at {history_path}{TUI.RESET}")
        return

    entries = []
    try:
        with open(history_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if grep_filter:
                        cmd = entry.get("command", "")
                        if grep_filter.lower() not in cmd.lower():
                            continue
                    entries.append(entry)
                except json.JSONDecodeError:
                    continue
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
    parser.add_argument("--banner", action="store_true", help="Keep the full ASCII banner permanently")
    parser.add_argument("--history", action="store_true", help="Browse last 50 history entries")
    parser.add_argument("--grep", type=str, default=None, help="Filter history by command name (use with --history)")
    args = parser.parse_args()

    if args.install:
        install_systemd_service()
    elif args.history:
        show_history(grep_filter=args.grep)
    else:
        main(keep_banner=args.banner)
