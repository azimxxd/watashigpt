from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from typing import Any

from .commands import ALL_COMMANDS
from .config import DEFAULT_CONFIG


class CommandRegistry:
    def __init__(self) -> None:
        self._handlers: dict[str, Callable] = {}

    @property
    def handlers(self) -> dict[str, Callable]:
        return self._handlers

    def register(self, name: str, handler: Callable) -> None:
        self._handlers[name] = handler

    def register_many(self, mapping: dict[str, Callable]) -> None:
        self._handlers.update(mapping)

    @staticmethod
    def register_personal_commands(commands: dict[str, Any], personal_commands: dict[str, Any]) -> dict[str, Any]:
        merged = deepcopy(commands)
        for pc_name, pc_config in personal_commands.items():
            if not isinstance(pc_config, dict):
                continue
            trigger = pc_config.get("trigger", f"{pc_name.upper()}:")
            cmd_key = f"personal_{pc_name}"
            merged[cmd_key] = {
                "prefixes": [trigger],
                "keywords": [pc_name],
                "description": pc_config.get("description", f"Personal: {pc_name}"),
                "llm_required": True,
                "_personal": True,
                "examples": pc_config.get("examples", []),
                "model": pc_config.get("model", ""),
            }
        return merged


def build_default_commands() -> dict[str, Any]:
    return deepcopy(DEFAULT_CONFIG.get("commands", {}))


def build_alias_map(commands: dict[str, Any] | None = None) -> dict[str, str]:
    commands = commands or build_default_commands()
    alias_map: dict[str, str] = {}
    for command_name, config in commands.items():
        for prefix in config.get("prefixes", []):
            alias_map[prefix.upper()] = command_name
    return alias_map


def get_required_command_names() -> set[str]:
    return set(ALL_COMMANDS.keys())


def missing_required_commands(commands: dict[str, Any] | None = None) -> set[str]:
    commands = commands or build_default_commands()
    return get_required_command_names() - set(commands.keys())
