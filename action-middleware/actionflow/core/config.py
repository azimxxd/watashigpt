from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

from actionflow.app.paths import example_config_path, runtime_log_path, user_config_path

EXAMPLE_CONFIG_PATH = example_config_path()
USER_CONFIG_PATH = user_config_path()

BASE_CONFIG: dict[str, Any] = {
    "hotkeys": {"intercept": "ctrl+alt+x", "undo": "ctrl+alt+z", "silent_toggle": "ctrl+alt+s"},
    "confidence_threshold": 0.7,
    "interaction_mode": "both",
    "silent_mode": True,
    "ui": {
        "mode": "silent",
        "show_success_notifications": False,
        "show_error_popups": False,
        "show_result_popups": False,
        "log_level": "info",
        "notify_on_image_save": True,
        "log_path": str(runtime_log_path()),
        "error_dedupe_window_seconds": 8.0,
    },
    "platform": {"clipboard_backend": "auto", "window_backend": "auto"},
    "app": {"launch_at_startup": False, "debug_console": False},
    "command_security": {
        "allowed_commands": [
            "ls", "cat", "grep", "find", "git", "echo", "date", "python", "node",
            "curl", "wc", "head", "tail", "sort", "uniq", "diff", "file", "stat",
            "whoami", "hostname", "uname", "env", "printenv", "which", "type",
        ]
    },
    "commands": {},
    "personal_commands": {},
    "llm": {"provider": "", "api_key": "", "model": ""},
}


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = yaml.safe_load(handle) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_example_config() -> dict[str, Any]:
    return _load_yaml(EXAMPLE_CONFIG_PATH)


def merge_config(defaults: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(defaults)
    for key, value in override.items():
        if key in ("hotkeys", "platform", "llm", "command_security", "ui", "app") and isinstance(value, dict):
            merged[key] = {**merged.get(key, {}), **value}
        elif key == "commands":
            merged_commands = deepcopy(merged.get("commands", {}))
            for command_name, command_config in (value or {}).items():
                if isinstance(command_config, dict) and isinstance(merged_commands.get(command_name), dict):
                    merged_commands[command_name] = {**merged_commands[command_name], **command_config}
                else:
                    merged_commands[command_name] = deepcopy(command_config)
            merged["commands"] = merged_commands
        elif key == "personal_commands":
            merged_personal = deepcopy(merged.get("personal_commands", {}))
            for command_name, command_config in (value or {}).items():
                if isinstance(command_config, dict) and isinstance(merged_personal.get(command_name), dict):
                    merged_personal[command_name] = {**merged_personal[command_name], **command_config}
                else:
                    merged_personal[command_name] = deepcopy(command_config)
            merged["personal_commands"] = merged_personal
        else:
            merged[key] = value
    return merged


def build_default_config() -> dict[str, Any]:
    return merge_config(BASE_CONFIG, load_example_config())


DEFAULT_CONFIG = build_default_config()


def load_config(config_path: Path) -> dict[str, Any]:
    if not config_path.exists():
        return deepcopy(DEFAULT_CONFIG)
    try:
        user_cfg = _load_yaml(config_path)
        return merge_config(DEFAULT_CONFIG, user_cfg)
    except Exception:
        return deepcopy(DEFAULT_CONFIG)


def save_config(config: dict[str, Any], config_path: Path | None = None) -> Path:
    target_path = config_path or USER_CONFIG_PATH
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, default_flow_style=False, allow_unicode=True, sort_keys=False)
    return target_path
