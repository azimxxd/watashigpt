"""Heuristics about the selection and the app it came from — no LLM involved.

AppContext: which kind of app (terminal/browser/IDE/chat/docs).
analyze_text(): language, code, formality, what the text looks like.
get_smart_suggestions(): ranks commands for the command picker.
PatternLearner: learns per-app preferences from the history log.
"""

from __future__ import annotations

import json
import re as _re
from dataclasses import dataclass
from pathlib import Path

from actionflow.config import CONFIG


class AppContext:
    """Detected context of the active application window."""
    TERMINAL = "terminal"
    BROWSER  = "browser"
    IDE      = "ide"
    CHAT     = "chat"
    DOCS     = "docs"
    UNKNOWN  = "unknown"

    APP_PATTERNS = {
        "terminal": ["terminal", "konsole", "alacritty", "kitty", "wezterm",
                      "gnome-terminal", "xterm", "foot", "tilix", "tmux",
                      "iterm", "warp", "ghostty", "hyper", "tabby"],
        "browser":  ["firefox", "chrome", "chromium", "brave", "vivaldi",
                      "edge", "safari", "opera", "zen browser", "arc", "orion",
                      "yandex"],
        "ide":      ["code", "vscode", "jetbrains", "intellij", "pycharm",
                      "webstorm", "clion", "rider", "neovim", "nvim", "vim",
                      "emacs", "sublime", "zed", "cursor", "lapce", "xcode",
                      "android studio", "nova", "bbedit", "windsurf"],
        "chat":     ["slack", "discord", "telegram", "teams", "signal",
                      "whatsapp", "element", "messages", "skype", "zoom"],
        "docs":     ["libreoffice", "google docs", "notion", "obsidian",
                      "logseq", "typora", "marktext", "writer", "word",
                      "pages", "notes", "bear", "craft", "textedit", "mail",
                      "outlook"],
    }

    def __init__(self, context_type: str = "unknown", window_title: str = "",
                 app_name: str = "", display_name: str = ""):
        self.context_type = context_type
        self.window_title = window_title
        self.app_name = app_name
        self.display_name = display_name  # e.g. "Telegram" (macOS)

    def __repr__(self) -> str:
        return f"AppContext({self.context_type}, app={self.app_name})"


# ============================================================
# Text Analysis — Heuristic Classification
# ============================================================

try:
    from langdetect import detect as _langdetect_detect
    from langdetect import DetectorFactory
    DetectorFactory.seed = 0
    _LANGDETECT_AVAILABLE = True
except ImportError:
    _LANGDETECT_AVAILABLE = False


@dataclass
class TextAnalysis:
    language: str = "en"
    is_code: bool = False
    is_formal: bool = True
    length: int = 0
    has_errors: bool = False
    looks_like: str = "prose"
    code_language: str = ""


_CODE_INDICATORS = [
    r'^\s*(def |class |import |from \w+ import|function |const |let |var )',
    r'[{};]\s*$',
    r'^\s*(public |private |protected |static |async |await )',
    r'^\s*#include|^\s*package |^\s*using ',
    r'[!=]=',
    r'->\s*\w+',
    r'^\s*<\w+[\s/>]',
]

_INFORMAL_MARKERS = frozenset([
    "lol", "omg", "wtf", "bruh", "nah", "gonna", "wanna", "gotta",
    "idk", "imo", "tbh", "lmao", "smh", "fr", "ngl", "asap", "pls",
    "plz", "thx", "ty", "np",
])


def analyze_text(text: str) -> TextAnalysis:
    """Analyze text using heuristics (no LLM). Fast, runs on every intercept."""
    result = TextAnalysis()
    stripped = text.strip()
    result.length = len(stripped)

    # --- Language detection ---
    if _LANGDETECT_AVAILABLE:
        try:
            result.language = _langdetect_detect(stripped[:500])
        except Exception:
            result.language = "en"

    # --- Code detection ---
    code_line_count = 0
    lines = stripped.split('\n')
    sample = lines[:30]
    for line in sample:
        for pattern in _CODE_INDICATORS:
            if _re.search(pattern, line):
                code_line_count += 1
                break
    code_ratio = code_line_count / max(len(sample), 1)
    result.is_code = code_ratio > 0.3

    # Code language heuristic
    if result.is_code:
        if _re.search(r'\bdef\b.*:\s*$|^\s*import\s+\w+|from\s+\w+\s+import', stripped, _re.MULTILINE):
            result.code_language = "python"
        elif _re.search(r'\bfunction\b|\bconst\b|\blet\b|\bconsole\.', stripped):
            result.code_language = "javascript"
        elif _re.search(r'\bfn\b|\blet\s+mut\b|\bimpl\b', stripped):
            result.code_language = "rust"
        elif _re.search(r'\bfunc\b.*\{|package\s+\w+|:=', stripped):
            result.code_language = "go"

    # --- Formality ---
    words_lower = stripped.lower().split()
    informal_count = sum(1 for w in words_lower if w.strip('.,!?') in _INFORMAL_MARKERS)
    result.is_formal = informal_count < 2

    # --- "Looks like" classification ---
    if result.is_code:
        result.looks_like = "code"
    elif stripped.startswith('{') and stripped.endswith('}'):
        result.looks_like = "json"
    elif _re.match(r'https?://', stripped):
        result.looks_like = "url"
    elif _re.search(r'^(diff --git|@@\s)', stripped, _re.MULTILINE):
        result.looks_like = "commit_diff"
    elif _re.search(r'^\s*[-*]\s', stripped, _re.MULTILINE) and stripped.count('\n') > 2:
        result.looks_like = "list"
    elif _re.search(r'(action items|next steps|attendees|agenda)', stripped.lower()):
        result.looks_like = "meeting_notes"
    elif _re.search(r'(traceback|error|exception|stack trace)', stripped.lower()):
        result.looks_like = "error"
    elif _re.search(r'^\d{4}-\d{2}-\d{2}.*\[', stripped, _re.MULTILINE):
        result.looks_like = "log"
    elif _re.search(r'(dear\s+\w+[,\n]|(?:hi|hello)\s+\w+[,\n]|^subject:\s|^re:\s)', stripped.lower()[:200], _re.MULTILINE):
        result.looks_like = "email_draft"

    # --- Basic error detection ---
    if not result.is_code and result.language == "en":
        if '  ' in stripped or _re.search(r'\.\s+[a-z]', stripped):
            result.has_errors = True

    return result


# ============================================================
# Smart Command Suggestions
# ============================================================

_DEFAULT_CONTEXT_PRIORITIES: dict[str, list[str]] = {
    "terminal":  ["command", "explain", "regex", "docstring", "review"],
    "browser":   ["summarize", "polite", "rewrite", "bullets", "title"],
    "ide":       ["docstring", "review", "explain", "gitcommit", "regex", "fmt"],
    "chat":      ["rewrite", "tone", "polite", "tweet"],
    "docs":      ["rewrite", "summarize", "bullets", "title", "meeting"],
    "unknown":   ["summarize", "rewrite", "explain", "fmt", "polite"],
}

_TEXT_TYPE_PRIORITIES: dict[str, list[str]] = {
    "code":          ["docstring", "review", "explain", "fmt", "gitcommit"],
    "json":          ["fmt", "explain", "redact"],
    "commit_diff":   ["gitcommit", "review", "summarize"],
    "list":          ["bullets", "todo", "summarize"],
    "meeting_notes": ["meeting", "todo", "summarize", "bullets"],
    "error":         ["explain", "review"],
    "log":           ["explain", "summarize", "redact"],
    "email_draft":   ["email", "rewrite", "tone"],
    "url":           ["wiki", "summarize"],
    "prose":         ["summarize", "rewrite", "polite", "bullets", "title"],
}


def get_smart_suggestions(
    app_ctx: AppContext,
    text_analysis: TextAnalysis,
    commands: dict,
    pattern_scores: dict[str, float] | None = None,
    max_starred: int = 3,
) -> list[tuple[str, dict, bool]]:
    """Return ordered list of (cmd_name, cmd_config, is_starred).
    First `max_starred` entries have is_starred=True."""

    scores: dict[str, float] = {}

    # 1. Context-type base score
    ctx_cmds = CONFIG.get("context_priorities", {}).get(
        app_ctx.context_type,
        _DEFAULT_CONTEXT_PRIORITIES.get(app_ctx.context_type, [])
    )
    for i, cmd_name in enumerate(ctx_cmds):
        if cmd_name in commands:
            scores[cmd_name] = scores.get(cmd_name, 0) + max(0, 10 - i)

    # 2. Text-type score
    text_cmds = _TEXT_TYPE_PRIORITIES.get(text_analysis.looks_like, [])
    for i, cmd_name in enumerate(text_cmds):
        if cmd_name in commands:
            scores[cmd_name] = scores.get(cmd_name, 0) + max(0, 8 - i)

    # 3. Language-specific boost
    if text_analysis.language != "en":
        if "trans" in commands:
            scores["trans"] = scores.get("trans", 0) + 5

    # 4. Code-specific boost
    if text_analysis.is_code:
        for cmd in ["docstring", "review", "explain", "fmt"]:
            if cmd in commands:
                scores[cmd] = scores.get(cmd, 0) + 3

    # 5. Informality boost
    if not text_analysis.is_formal:
        if "polite" in commands:
            scores["polite"] = scores.get("polite", 0) + 4
        if "rewrite" in commands:
            scores["rewrite"] = scores.get("rewrite", 0) + 3

    # 6. PatternLearner scores
    if pattern_scores:
        for cmd_name, learned_score in pattern_scores.items():
            if cmd_name in commands:
                scores[cmd_name] = scores.get(cmd_name, 0) + learned_score

    # Sort by score descending
    sorted_cmds = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    result: list[tuple[str, dict, bool]] = []
    starred_count = 0
    seen: set[str] = set()
    for cmd_name, score in sorted_cmds:
        if cmd_name not in commands:
            continue
        is_starred = starred_count < max_starred and score > 0
        if is_starred:
            starred_count += 1
        result.append((cmd_name, commands[cmd_name], is_starred))
        seen.add(cmd_name)

    # Append remaining commands alphabetically
    for cmd_name in sorted(commands):
        if cmd_name not in seen:
            result.append((cmd_name, commands[cmd_name], False))

    return result


# ============================================================
# Pattern Learner
# ============================================================

class PatternLearner:
    """Learns command preferences from history. Reads JSONL, computes
    per-context usage-frequency weights."""

    MIN_SAMPLES = 20
    DOMINATE_SAMPLES = 100

    def __init__(self, history_path: Path):
        self._history_path = history_path
        self._samples: int = 0
        self._context_counts: dict[str, dict[str, int]] = {}  # app_context → {cmd: count}
        self._total_counts: dict[str, int] = {}

    def load(self) -> None:
        """Read history file and compute frequency tables."""
        self._context_counts.clear()
        self._total_counts.clear()
        self._samples = 0
        if not self._history_path.exists():
            return
        try:
            with open(self._history_path, "r") as f:
                for line in f:
                    try:
                        entry = json.loads(line.strip())
                        cmd = entry.get("command", "")
                        if not cmd:
                            continue
                        ctx = entry.get("app_context", "unknown")
                        self._samples += 1
                        self._total_counts[cmd] = self._total_counts.get(cmd, 0) + 1
                        if ctx not in self._context_counts:
                            self._context_counts[ctx] = {}
                        self._context_counts[ctx][cmd] = self._context_counts[ctx].get(cmd, 0) + 1
                    except (json.JSONDecodeError, KeyError):
                        continue
        except Exception:
            pass

    def get_scores(self, app_context: str) -> dict[str, float]:
        """Return command → score dict based on learned patterns."""
        if self._samples < self.MIN_SAMPLES:
            return {}

        blend = min(1.0, (self._samples - self.MIN_SAMPLES) /
                    max(1, self.DOMINATE_SAMPLES - self.MIN_SAMPLES))

        counts = self._context_counts.get(app_context, self._total_counts)
        if not counts:
            counts = self._total_counts

        total = sum(counts.values()) or 1
        scores: dict[str, float] = {}
        for cmd, count in counts.items():
            scores[cmd] = (count / total) * blend * 15
        return scores

    @property
    def sample_count(self) -> int:
        return self._samples
