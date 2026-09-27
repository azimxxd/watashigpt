"""Configuration: defaults, loading config.yaml, and comment-preserving saves."""

from __future__ import annotations

import copy
import re
import sys
from pathlib import Path

import yaml
from actionflow.product import writing_commands

RESOURCE_DIR = Path(__file__).resolve().parent.parent
APP_DIR = (Path.home() / "Library" / "Application Support" / "ActionFlow"
           if getattr(sys, "frozen", False) and sys.platform == "darwin"
           else RESOURCE_DIR)
CONFIG_PATH = APP_DIR / "config.yaml"
CONFIG_EXAMPLE_PATH = RESOURCE_DIR / "config.yaml.example"

DEFAULT_CONFIG = {
    "hotkeys": {"intercept": "ctrl+alt+x", "undo": "ctrl+alt+z"},
    "commands": {
        "polite": {
            "prefixes": ["POL:", "POLITE:"],
            "keywords": ["polite", "rephrase", "professional", "corporatize"],
            "description": "Rewrite rude/blunt text politely",
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
    "image_api": {"provider": "pollinations", "api_key": "", "model": "flux"},
}

# Sections merged key-by-key with defaults; every other top-level key
# (commands, personal_commands, context_priorities, ...) is taken as-is.
MERGED_SECTIONS = ("hotkeys", "llm", "image_api")


def load_config(path: Path | None = None) -> dict:
    """Load config.yaml, falling back to defaults if missing or invalid.

    Always returns a fresh deep copy so runtime mutations (API keys, personal
    commands) never leak into DEFAULT_CONFIG.
    """
    path = path or CONFIG_PATH
    if not path.exists() and path == CONFIG_PATH and CONFIG_EXAMPLE_PATH.exists():
        path = CONFIG_EXAMPLE_PATH  # full command set until the user creates a config
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    cfg["commands"] = writing_commands(cfg["commands"])
    if not path.exists():
        return cfg
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_cfg = yaml.safe_load(f) or {}
        if not isinstance(user_cfg, dict):
            raise ValueError("top level must be a mapping")
    except Exception as exc:
        print(f"  Warning: Failed to load config.yaml: {exc}")
        print("  Falling back to defaults.")
        return cfg

    for key, value in user_cfg.items():
        if key in MERGED_SECTIONS and isinstance(value, dict):
            cfg[key] = {**cfg.get(key, {}), **value}
        elif value is not None:
            cfg[key] = value
    cfg["commands"] = writing_commands(cfg["commands"])
    return cfg


# The one live config object. Never rebind it — mutate in place, so every
# module that imported it sees the same data.
CONFIG = load_config()


def ensure_user_config() -> bool:
    """First run: copy config.yaml.example → config.yaml. Returns True if created."""
    if CONFIG_PATH.exists() or not CONFIG_EXAMPLE_PATH.exists():
        return False
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CONFIG_PATH.open("x", encoding="utf-8") as target:
        CONFIG_PATH.chmod(0o600)
        target.write(CONFIG_EXAMPLE_PATH.read_text(encoding="utf-8"))
    return True


def _set_path(text: str, path: list[str], value) -> str | None:
    """Set a scalar at `path` (e.g. ["llm", "fallback", "model"]) in YAML
    source, keeping comments and layout (2-space indentation). Adds the leaf
    key if its parent exists. Returns None when the layout is too unusual."""
    lines = text.splitlines(keepends=True)
    rendered = yaml.safe_dump(value, default_flow_style=True, allow_unicode=True).strip()
    if rendered.endswith("\n..."):
        rendered = rendered[: -len("\n...")].strip()
    start, end = 0, len(lines)  # current search window (the parent's block)
    for depth, key in enumerate(path):
        indent = "  " * depth
        pattern = re.compile(rf"^{indent}{re.escape(key)}:(\s*)([^#\n]*?)(\s*#.*)?(\r?\n)?$")
        found = None
        for i in range(start, end):
            line = lines[i]
            if line.strip() and not line.startswith(indent) and depth:
                break
            if pattern.match(line) and not line[len(indent):].startswith(" "):
                found = i
                break
        if found is None:
            if depth == len(path) - 1 and (depth == 0 or start > 0):
                lines.insert(start, f"{indent}{key}: {rendered}\n")
                return "".join(lines)
            return None
        if depth == len(path) - 1:
            m = pattern.match(lines[found])
            lines[found] = f"{indent}{key}: {rendered}{m.group(3) or ''}{m.group(4) or chr(10)}"
            return "".join(lines)
        # narrow the window to this key's block
        start = found + 1
        end = start
        while end < len(lines) and (not lines[end].strip() or lines[end].startswith("  " * (depth + 1))
                                    or lines[end].lstrip().startswith("#")):
            end += 1
    return None


def save_nested(path: list[str], values: dict) -> None:
    """Persist values under a (possibly nested) section of config.yaml without
    destroying comments; falls back to a full rewrite for unusual layouts.
    Raises OSError / yaml.YAMLError on failure."""
    text = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else ""
    edited: str | None = text
    for key, value in values.items():
        edited = _set_path(edited, path + [key], value) if edited is not None else None
    try:
        parsed = yaml.safe_load(edited) if edited is not None else None
        node = parsed
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        valid = isinstance(node, dict) and all(node.get(k) == v for k, v in values.items())
    except yaml.YAMLError:
        valid = False
    if not valid:
        data = (yaml.safe_load(text) or {}) if text else {}
        node = data
        for key in path:
            node = node.setdefault(key, {}) if isinstance(node.get(key), dict) else node.setdefault(key, {})
            if not isinstance(node, dict):
                raise ValueError(f"config.yaml: {'.'.join(path)} is not a mapping")
        node.update(values)
        edited = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    CONFIG_PATH.write_text(edited, encoding="utf-8")


def save_values(section: str, values: dict) -> None:
    """Persist `section.<key>` values (see save_nested)."""
    save_nested([section], values)
