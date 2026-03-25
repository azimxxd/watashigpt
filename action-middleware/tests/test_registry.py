from actionflow.core.command_registry import build_alias_map, build_default_commands, missing_required_commands
from actionflow.core.config import merge_config


def test_registry_contains_all_required_commands():
    commands = build_default_commands()
    assert not missing_required_commands(commands)


def test_aliases_are_centralized_and_resolved():
    alias_map = build_alias_map()
    assert alias_map["TR:"] == "translate"
    assert alias_map["RUN:"] == "command"
    assert alias_map["PING:"] == "test"
    assert alias_map["TLDR:"] == "summarize"
    assert alias_map["CR:"] == "review"


def test_command_overrides_preserve_existing_aliases():
    merged = merge_config(
        {"commands": {"rewrite": {"prefixes": ["RW:"], "keywords": ["rewrite"], "description": "old"}}},
        {"commands": {"rewrite": {"description": "new"}}},
    )
    assert merged["commands"]["rewrite"]["prefixes"] == ["RW:"]
    assert merged["commands"]["rewrite"]["description"] == "new"
