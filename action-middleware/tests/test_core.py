"""Core logic tests — no hotkeys, clipboard, network or GUI involved.

Run from the action-middleware directory:  python -m pytest tests
"""
import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
import platform_mac  # noqa: E402


@pytest.fixture
def pasted(monkeypatch):
    """Capture replacements instead of touching the real clipboard/keyboard."""
    out: list[str] = []
    monkeypatch.setattr(main, "_replace_selection",
                        lambda text: out.append(text) if not main._chain_suppress_paste else None)
    monkeypatch.setattr(main, "notify", lambda *a, **k: None)
    monkeypatch.setattr(main, "_log_history", lambda *a, **k: None)
    monkeypatch.setattr(main, "_undo_stack", [])
    return out


# ── Config ─────────────────────────────────────────────────

def test_load_config_keeps_all_top_level_sections(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "confidence_threshold: 0.9\n"
        "silent_mode: true\n"
        "hotkeys: {intercept: ctrl+alt+q}\n"
        "personal_commands: {standup: {description: x}}\n"
        "context_priorities: {chat: [tweet]}\n"
        "command_security: {allowed_commands: [ls]}\n"
        "llm: {provider: groq}\n"
    )
    cfg = main.load_config(cfg_file)
    assert cfg["confidence_threshold"] == 0.9
    assert cfg["silent_mode"] is True
    assert cfg["personal_commands"] == {"standup": {"description": "x"}}
    assert cfg["context_priorities"] == {"chat": ["tweet"]}
    assert cfg["command_security"] == {"allowed_commands": ["ls"]}
    # merged sections keep defaults for unspecified keys
    assert cfg["hotkeys"] == {"intercept": "ctrl+alt+q", "undo": "ctrl+alt+z"}
    assert cfg["llm"]["provider"] == "groq" and cfg["llm"]["api_key"] == ""


def test_load_config_never_mutates_defaults(tmp_path):
    cfg = main.load_config(tmp_path / "missing.yaml")
    cfg["llm"]["api_key"] = "secret"
    cfg["commands"]["polite"]["prefixes"].append("X:")
    assert main._DEFAULT_CONFIG["llm"]["api_key"] == ""
    assert "X:" not in main._DEFAULT_CONFIG["commands"]["polite"]["prefixes"]


def test_load_config_invalid_yaml_falls_back(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("- just\n- a list\n")
    assert main.load_config(cfg_file)["commands"].keys() == main._DEFAULT_CONFIG["commands"].keys()


def test_example_config_is_valid():
    cfg = main.load_config(main._CONFIG_EXAMPLE_PATH)
    assert len(cfg["commands"]) > 30
    for name, cmd in cfg["commands"].items():
        assert cmd.get("prefixes"), name
        if cmd.get("llm_required") and name not in ("tone",):
            assert "{text}" in cmd.get("llm_prompt", "{text}"), name
    unsafe = {"python", "node", "env", "printenv", "curl", "find"}
    assert not unsafe & set(cfg["command_security"]["allowed_commands"])


# ── Routing ────────────────────────────────────────────────

COMMANDS = {
    "tone": {"prefixes": ["TONE:"]},
    "summarize": {"prefixes": ["SUM:", "TLDR:"]},
    "polite": {"prefixes": ["POL:"]},
    "b64": {"prefixes": ["B64:"]},
    "mock": {"prefixes": ["MOCK:"]},
}


@pytest.mark.parametrize("text,expected", [
    ("SUM: hello", ("summarize", "hello")),
    ("sum:hello", ("summarize", "hello")),
    ("   TLDR:  two spaces", ("summarize", " two spaces")),
    ("TONE:casual: hey", ("tone", "casual: hey")),
    ("nothing here", None),
])
def test_resolve_prefix(text, expected):
    match = main._resolve_prefix(text, COMMANDS)
    assert (match[0], match[1]) == expected if expected else match is None


def test_resolve_prefix_prefers_longest():
    cmds = {"a": {"prefixes": ["TR:"]}, "b": {"prefixes": ["TRANS:"]}}
    assert main._resolve_prefix("TRANS: hi", cmds)[0] == "b"


def test_parse_chain():
    chain = main._parse_chain("POL:|SUM: some text", COMMANDS)
    assert [name for name, _ in chain] == ["polite", "summarize"]
    assert main._extract_chain_payload("POL:|SUM: some text", COMMANDS) == "some text"
    assert main._parse_chain("SUM: a | b", COMMANDS) is None


def test_chain_passes_output_and_keeps_single_undo_point(pasted, monkeypatch):
    monkeypatch.setattr(main, "CONFIG", {**main.CONFIG, "commands": COMMANDS})
    main.route("MOCK:|B64: hello")
    expected = base64.b64encode(b"HeLlO").decode()
    assert pasted == [expected]
    assert main._undo_stack == [{"original": "MOCK:|B64: hello", "replacement": expected}]


def test_chain_stops_when_step_produces_no_text(pasted, monkeypatch):
    cmds = {**COMMANDS, "count": {"prefixes": ["COUNT:"]}}
    monkeypatch.setattr(main, "CONFIG", {**main.CONFIG, "commands": cmds})
    main.route("COUNT:|B64: hello")
    assert pasted == []  # B64 of a stale value must never be pasted
    while not main._result_queue.empty():
        main._result_queue.get_nowait()


# ── LLM failure handling ───────────────────────────────────

class _FailingClient:
    class chat:
        class completions:
            @staticmethod
            def create(**kwargs):
                raise ConnectionError("network down")


def test_llm_failure_never_replaces_text(pasted, monkeypatch):
    monkeypatch.setattr(main, "LLM_MODE", "live")
    monkeypatch.setattr(main, "_llm_ready", True)
    monkeypatch.setattr(main, "_llm_client", _FailingClient)
    monkeypatch.setattr(main, "_llm_fallback_ready", False)
    cmd = {"llm_required": True, "llm_prompt": "Summarize: {text}", "prefixes": ["SUM:"]}
    assert main.dispatch("summarize", "some text", "SUM: some text", cmd) is None
    assert pasted == []
    assert main._undo_stack == []


def test_llm_call_raises_after_fallback_fails(monkeypatch):
    monkeypatch.setattr(main, "_llm_ready", True)
    monkeypatch.setattr(main, "_llm_client", _FailingClient)
    monkeypatch.setattr(main, "_llm_fallback_ready", True)
    monkeypatch.setattr(main, "_llm_fallback_client", _FailingClient)
    with pytest.raises(main.LLMError):
        main._llm_call("hi")


def test_format_prompt():
    vars_ = {"text": "hello", "lang": "JP"}
    assert main._format_prompt("To {lang}: {text}", vars_) == "To JP: hello"
    assert main._format_prompt("Fill {{placeholder}}: {text}", vars_) == "Fill {placeholder}: hello"
    assert main._format_prompt("{unknown} {text}", vars_) == "{unknown} hello"
    assert main._format_prompt("stray { brace {text}", vars_) == "stray { brace hello"
    assert main._format_prompt("{0} {text}", vars_) == "{0} hello"


# ── Built-in handlers ──────────────────────────────────────

@pytest.mark.parametrize("expr,expected", [
    ("2+2", "4"),
    ("15% of 340", "51"),
    ("what is 15% of 340", "51"),
    ("15% of 340 + 1", "52"),
    ("2**3 + 1", "9"),
    ("2^10", "1024"),
    ("sqrt(144) + 1", "13"),
    ("sin(0)", "0"),
    ("asin(1)", "1.5707963268"),
    ("the answer is 5 * 3", "15"),
    ("1/0", None),
    ("2**1000", None),
])
def test_safe_eval_math(expr, expected):
    assert main._safe_eval_math(expr) == expected


def test_redact():
    out: list[str] = []
    main_redact = main.handle_redact
    orig = main._replace_selection
    main._replace_selection = out.append
    try:
        main_redact("mail bob@example.com card 4111 1111 1111 1111", "", {})
    finally:
        main._replace_selection = orig
        main._undo_stack.clear()
    assert out == ["mail [EMAIL] card [CARD]"]


@pytest.mark.parametrize("command", [
    "python -c 'print(1)'",
    "/bin/ls",
    "rm -rf /tmp/x",
    "git -c core.pager=sh status",
    "git --config-env=core.pager=X log",
    "env",
])
def test_cmd_blocks_unsafe(command, monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_run_as_user", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(main, "notify", lambda *a, **k: None)
    monkeypatch.setattr(main, "_CMD_ALLOWED_COMMANDS", list(main._CMD_DEFAULT_ALLOWED))
    main.handle_command(command, command, {})
    assert calls == []


def test_smart_suggestions_rank_code_commands():
    cmds = {n: {} for n in ("docstring", "review", "explain", "summarize", "polite")}
    ctx = main.AppContext("ide", "code", "code")
    ta = main.analyze_text("def foo():\n    return 1\n")
    ranked = [name for name, _cfg, _star in main.get_smart_suggestions(ctx, ta, cmds)]
    assert ranked[0] == "docstring"
    assert set(ranked) == set(cmds)


# ── macOS helpers (pure functions, run on any OS) ──────────

def test_parse_hotkey():
    flags, key = platform_mac.parse_hotkey("ctrl+alt+x")
    assert key == platform_mac.KEYCODES["x"]
    assert flags == platform_mac.FLAG_CTRL | platform_mac.FLAG_ALT
    assert platform_mac.parse_hotkey("cmd+shift+space")[1] == 49
    with pytest.raises(ValueError):
        platform_mac.parse_hotkey("hyper+x")
    with pytest.raises(ValueError):
        platform_mac.parse_hotkey("ctrl+ü")


def test_format_hotkey():
    assert platform_mac.format_hotkey("ctrl+alt+x") == "⌃⌥X"
    assert platform_mac.format_hotkey("cmd+shift+f5") == "⌘⇧F5"
