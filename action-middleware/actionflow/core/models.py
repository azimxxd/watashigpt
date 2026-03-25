from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AppContext:
    context_type: str = "unknown"
    window_title: str = ""
    app_name: str = ""

    TERMINAL = "terminal"
    BROWSER = "browser"
    IDE = "ide"
    CHAT = "chat"
    DOCS = "docs"
    UNKNOWN = "unknown"

    APP_PATTERNS = {
        "terminal": [
            "terminal", "konsole", "alacritty", "kitty", "wezterm",
            "gnome-terminal", "xterm", "foot", "tilix", "tmux",
            "powershell", "pwsh", "cmd.exe", "windows terminal",
        ],
        "browser": [
            "firefox", "chrome", "chromium", "brave", "vivaldi",
            "edge", "safari", "opera", "zen browser",
        ],
        "ide": [
            "code", "vscode", "jetbrains", "intellij", "pycharm",
            "webstorm", "clion", "rider", "neovim", "nvim", "vim",
            "emacs", "sublime", "zed", "cursor", "lapce",
        ],
        "chat": [
            "slack", "discord", "telegram", "teams", "signal",
            "whatsapp", "element",
        ],
        "docs": [
            "libreoffice", "google docs", "notion", "obsidian",
            "logseq", "typora", "marktext", "writer", "word",
        ],
    }


@dataclass
class TextAnalysis:
    language: str = "en"
    is_code: bool = False
    is_formal: bool = True
    length: int = 0
    has_errors: bool = False
    looks_like: str = "prose"
    code_language: str = ""


@dataclass
class DispatchContext:
    trigger: str = "prefix"
    app_context: AppContext | None = None
    text_analysis: TextAnalysis | None = None

