"""Core logic tests — no hotkeys, clipboard, network or GUI involved.

Run from the action-middleware directory:  python -m pytest tests
"""
import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402
from actionflow import config as af_config  # noqa: E402
from actionflow.analysis import get_smart_suggestions
from actionflow import platform_mac  # noqa: E402


@pytest.fixture
def pasted(monkeypatch):
    """Capture replacements instead of touching the real clipboard/keyboard."""
    out: list[str] = []
    monkeypatch.setattr(main, "_replace_selection",
                        lambda text, **kwargs: out.append(text) if not main._chain_suppress_paste else None)
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
    assert af_config.DEFAULT_CONFIG["llm"]["api_key"] == ""
    assert "X:" not in af_config.DEFAULT_CONFIG["commands"]["polite"]["prefixes"]


def test_load_config_invalid_yaml_falls_back(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("- just\n- a list\n")
    assert main.load_config(cfg_file)["commands"].keys() == main.product.writing_commands(af_config.DEFAULT_CONFIG["commands"]).keys()


def test_example_config_is_valid():
    cfg = main.load_config(af_config.CONFIG_EXAMPLE_PATH)
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
    monkeypatch.setattr(main.llm, "MODE", "live")
    monkeypatch.setattr(main.llm, "ready", True)
    monkeypatch.setattr(main.llm, "client", _FailingClient)
    monkeypatch.setattr(main.llm, "fallback_ready", False)
    cmd = {"llm_required": True, "llm_prompt": "Summarize: {text}", "prefixes": ["SUM:"]}
    assert main.dispatch("summarize", "some text", "SUM: some text", cmd) is None
    assert pasted == []
    assert main._undo_stack == []


def test_llm_call_raises_after_fallback_fails(monkeypatch):
    monkeypatch.setattr(main.llm, "ready", True)
    monkeypatch.setattr(main.llm, "client", _FailingClient)
    monkeypatch.setattr(main.llm, "fallback_ready", True)
    monkeypatch.setattr(main.llm, "fallback_client", _FailingClient)
    with pytest.raises(main.llm.LLMError):
        main.llm.call("hi")


def test_format_prompt():
    vars_ = {"text": "hello", "lang": "JP"}
    assert main.prompts.format_prompt("To {lang}: {text}", vars_) == "To JP: hello"
    assert main.prompts.format_prompt("Fill {{placeholder}}: {text}", vars_) == "Fill {placeholder}: hello"
    assert main.prompts.format_prompt("{unknown} {text}", vars_) == "{unknown} hello"
    assert main.prompts.format_prompt("stray { brace {text}", vars_) == "stray { brace hello"
    assert main.prompts.format_prompt("{0} {text}", vars_) == "{0} hello"


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
    assert main.textops.calc(expr) == expected


def test_redact_handler():
    out: list[str] = []
    main_redact = main.handle_redact
    orig = main._replace_selection
    main._replace_selection = lambda text, **kwargs: out.append(text)
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
    ranked = [name for name, _cfg, _star in get_smart_suggestions(ctx, ta, cmds)]
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


# ── API keys / history / autostart ─────────────────────────

def test_resolve_api_key_order(monkeypatch):
    store = {"llm:groq": "from-keychain"}
    monkeypatch.setattr(main.llm, "secret_get", lambda account: store.get(account, ""))
    monkeypatch.delenv("ACTIONFLOW_API_KEY", raising=False)
    assert main.llm.resolve_api_key("llm", "groq") == "from-keychain"
    assert main.llm.resolve_api_key("llm", "Groq ", "from-config") == "from-config"
    monkeypatch.setenv("ACTIONFLOW_API_KEY", "from-env")
    assert main.llm.resolve_api_key("llm", "groq", "from-config") == "from-env"
    monkeypatch.delenv("ACTIONFLOW_API_KEY")
    assert main.llm.resolve_api_key("llm", "openai") == ""


def test_history_hides_text_by_default(tmp_path, monkeypatch):
    path = tmp_path / "history.jsonl"
    monkeypatch.setattr(main.history, "HISTORY_PATH", path)
    monkeypatch.setitem(main.CONFIG, "history", {})
    main._log_history("summarize", "my secret text", "short", 5)
    monkeypatch.setitem(main.CONFIG, "history", {"log_text": True})
    main._log_history("summarize", "visible", "out", 5)
    main._log_history("password", "x", "hunter2", 5)
    lines = [__import__("json").loads(l) for l in path.read_text().splitlines()]
    assert (lines[0]["input"], lines[0]["output"]) == ("[14 chars]", "[5 chars]")
    assert (lines[1]["input"], lines[1]["output"]) == ("visible", "out")
    assert lines[2]["output"] == "[REDACTED]"


def test_launch_agent_plist():
    import plistlib
    data = plistlib.loads(plistlib.dumps(main.service.launch_agent_plist(Path(main.__file__))))
    assert data["Label"] == "com.watashigpt.actionflow"
    assert data["ProgramArguments"][1].endswith("main.py")
    assert data["KeepAlive"] == {"SuccessfulExit": False}


# ── Command palette controller (macOS UI logic, no AppKit needed) ──

def _controller(text="some text here"):
    cmds = main.load_config(af_config.CONFIG_EXAMPLE_PATH)["commands"]
    ctx = main.AppContext("chat", "telegram", "telegram", "Telegram")
    sugg = get_smart_suggestions(ctx, main.analyze_text(text), cmds)
    return main.palette.PaletteController(text, cmds, sugg, prompt_for=main._llm_prompt_for), cmds


def test_palette_items_sections_and_search(monkeypatch, tmp_path):
    monkeypatch.setattr(main.preferences, 'PATH', tmp_path / 'preferences.json')
    monkeypatch.setattr(main.llm, 'ready', True)
    ctl, cmds = _controller()
    rows = ctl.items('', None)
    assert [r['id'] for r in rows] == ['proofread','clarify','shorten','trans','new_action','settings']
    assert ctl.items('fix', None)[0]['id'] == 'proofread'
    assert all(r['id'] != 'b64' for r in ctl.items('b64',None))
    sentence = ctl.items('make it shorter',None)
    assert sentence[0]['id'] == 'custom' and sentence[0]['instruction'] == 'make it shorter'
    assert ctl.items('zzzz',None)[0]['id'] == 'custom'


def test_palette_actions(monkeypatch):
    ctl, _ = _controller("hello world")
    monkeypatch.setattr(main.llm, "MODE", "mock")
    monkeypatch.setattr(main.llm, "ready", False)
    assert ctl.activate({"id": "summarize", "title": "Summarize", "icon": "x", "tint": "purple"}, "")["kind"] == "submenu"
    assert ctl.activate({"id": "b64"}, "")["kind"] == "run"
    assert ctl.activate({"id": "count"}, "")["kind"] == "run"
    assert ctl.activate({"id": "tone"}, "")["kind"] == "submenu"

    monkeypatch.setattr(main.llm, "MODE", "live")
    monkeypatch.setattr(main.llm, "ready", True)
    seen = {}
    monkeypatch.setattr(main.llm, "stream", lambda prompt, model="": seen.setdefault("prompt", prompt) and iter(["ok"]))
    action = ctl.activate({"id": "tone:casual", "title": "Casual"}, "")
    assert action["kind"] == "stream" and action["cmd_name"] == "tone"
    list(action["factory"]())
    assert "casual tone" in seen["prompt"] and "hello world" in seen["prompt"]
    custom = ctl.activate({"id": "custom", "instruction": "in French"}, "in French")
    assert custom["kind"] == "stream"
    assert ctl.items("", "trans")[0]["id"].startswith("language:")
    assert ctl.items("Kazakh", "trans")[0]["id"] == "language:Kazakh"
    assert ctl.items("Klingon", "trans")[-1]["id"] == "language:Klingon"
    prompt, _ = main.prompts.prompt_for("trans", {"llm_prompt": "Translate to {lang}: {text}"},
                                        "Brazilian Portuguese: bom dia")
    assert prompt.endswith("Translate to Brazilian Portuguese: bom dia")


def test_polite_phrase_runs_instantly():
    ctl, _ = _controller("this sucks")
    assert ctl.activate({"id": "polite", "title": "Make Polite", "icon": "x", "tint": "pink"}, "")["kind"] == "run"


def test_llm_stream_falls_back_before_first_token(monkeypatch):
    class Chunk:
        def __init__(self, t):
            self.choices = [type("C", (), {"delta": type("D", (), {"content": t})()})()]

    class Good:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    return iter([Chunk("Hel"), Chunk("lo")])

    monkeypatch.setattr(main.llm, "ready", True)
    monkeypatch.setattr(main.llm, "client", _FailingClient)
    monkeypatch.setattr(main.llm, "fallback_ready", True)
    monkeypatch.setattr(main.llm, "fallback_client", Good)
    assert "".join(main.llm.stream("hi")) == "Hello"


# ── textops ────────────────────────────────────────────────

from actionflow import textops  # noqa: E402


@pytest.mark.parametrize("text,expected", [
    ("mail bob@example.com card 4111 1111 1111 1111", "mail [EMAIL] card [CARD]"),
    ("call +7 701 123 4567 now", "call [PHONE] now"),
    ("order 12345678 shipped", "order 12345678 shipped"),          # not a phone
    ("server 192.168.1.10", "server [IP]"),
    ("key sk-proj-abcdefghijklmnopqrstuv", "key [API_KEY]"),
])
def test_redact(text, expected):
    assert textops.redact(text)[0] == expected


def test_escape_and_sanitize():
    assert textops.escape("sql: it's; fine") == ("sql", "it''s; fine")
    assert textops.escape("<b>") == ("html", "&lt;b&gt;")
    assert textops.sanitize("# Title\nUse C# and **bold** #tag") == "Title\nUse C# and bold #tag"
    assert textops.sanitize("\x1b[31mred\x1b[0m") == "red"


def test_b64_roundtrip_and_strict_decode():
    assert textops.b64_decode(textops.b64_encode("Привет")) == "Привет"
    with pytest.raises(ValueError):
        textops.b64_decode("this is not base64!!")


def test_format_structured():
    assert textops.format_structured('{"b":1,"a":[1,2]}')[1] == '{\n  "b": 1,\n  "a": [\n    1,\n    2\n  ]\n}'
    assert textops.format_structured('min: {"a": 1}') == ("Minified JSON", '{"a":1}')
    assert textops.format_structured("<a><b>x</b></a>")[1] == "<a>\n  <b>x</b>\n</a>"
    with pytest.raises(ValueError):
        textops.format_structured("just words")


def test_password_and_mocking_case():
    pw = textops.generate_password(16)
    assert len(pw) == 16 and any(c.isdigit() for c in pw) and any(c.isupper() for c in pw)
    assert textops.mocking_case("hello world") == "HeLlO wOrLd"


def test_history_rotation_and_export(tmp_path, monkeypatch):
    path = tmp_path / "h.jsonl"
    monkeypatch.setattr(main.history, "HISTORY_PATH", path)
    monkeypatch.setattr(main.history, "_MAX_LINES", 10)
    monkeypatch.setattr(main.history, "_KEEP_LINES", 4)
    monkeypatch.setitem(main.CONFIG, "history", {"log_text": True})
    for i in range(12):
        main.history.log("cmd", f"in|{i}\nx", "out", 1)
    main.history.rotate()
    assert len(path.read_text().splitlines()) == 4
    monkeypatch.setattr(main.history.Path, "home", lambda: tmp_path)
    md = main.history.export_session(__import__("datetime").datetime(2000, 1, 1), "mock").read_text()
    assert "in\\|11 x" in md and md.count("\n| ") == 5  # header + 4 rows, pipes escaped


# ── Providers / config saving ──────────────────────────────

def test_provider_defaults_are_current():
    p = main.llm.PROVIDERS
    assert "github" not in p and "github" in main.llm.RETIRED_PROVIDERS
    assert all(p[n].free for n in ("groq", "gemini", "cerebras", "openrouter"))
    assert p["gemini"].default_model != "gemini-2.0-flash"
    with pytest.raises(ValueError, match="retired"):
        main.llm.make_client("github", "key")


def test_retired_model_is_replaced():
    _client, model = main.llm.make_client("gemini", "dummy-key", "gemini-2.0-flash")
    assert model == main.llm.RETIRED_MODELS["gemini-2.0-flash"]


def test_request_options_turn_reasoning_down(monkeypatch):
    opts = main.llm.request_options
    assert opts("groq", "openai/gpt-oss-120b")["include_reasoning"] is False
    assert opts("groq", "qwen/qwen3.8-27b")["reasoning_effort"] == "none"
    assert opts("gemini", "gemini-2.5-flash")["reasoning_effort"] == "none"
    assert opts("openrouter", "x:free")["reasoning"]["exclude"] is True
    assert opts("openai", "gpt-6-luna") == {}
    monkeypatch.setitem(main.CONFIG["llm"], "request_options", {"top_p": 0.9})
    assert opts("openai", "gpt-6-luna") == {"top_p": 0.9}


def test_strip_thinking_stream():
    f = lambda parts: "".join(main.llm._without_thinking(iter(parts)))
    assert f(["<thi", "nk>plan", "</think>", "Hello"]) == "Hello"
    assert f(["Hel", "lo"]) == "Hello"
    assert main.llm.strip_thinking("<think>x</think>\nDone") == "Done"


def test_config_save_keeps_comments(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("# top comment\nllm:\n  provider: ''  # pick one\n  model: ''\n"
                    "  fallback:\n    provider: ''\ncommands: {}\n")
    monkeypatch.setattr(af_config, "CONFIG_PATH", path)
    af_config.save_values("llm", {"provider": "groq", "model": "openai/gpt-oss-120b"})
    af_config.save_nested(["llm", "fallback"], {"provider": "gemini", "model": "gemini-3.5-flash-lite"})
    text = path.read_text()
    assert "# top comment" in text and "# pick one" in text
    data = __import__("yaml").safe_load(text)
    assert data["llm"]["provider"] == "groq" and data["llm"]["model"] == "openai/gpt-oss-120b"
    assert data["llm"]["fallback"] == {"provider": "gemini", "model": "gemini-3.5-flash-lite"}


def test_prompts_keep_the_text_language():
    cmds = main.load_config(af_config.CONFIG_EXAMPLE_PATH)["commands"]
    summarize, _ = main.prompts.prompt_for("summarize", cmds["summarize"], "Привет")
    assert summarize.startswith(main.prompts.LANGUAGE_RULE)
    trans, _ = main.prompts.prompt_for("trans", cmds["trans"], "EN: Привет")
    assert main.prompts.LANGUAGE_RULE not in trans
    assert main.llm.tidy("- a  \n- b  ") == "- a\n- b"


def test_first_config_creates_private_writable_directory(tmp_path, monkeypatch):
    path = tmp_path / "Application Support" / "ActionFlow" / "config.yaml"
    example = tmp_path / "defaults.yaml"
    example.write_text("llm: {provider: ''}\n")
    monkeypatch.setattr(af_config, "CONFIG_PATH", path)
    monkeypatch.setattr(af_config, "CONFIG_EXAMPLE_PATH", example)
    assert af_config.ensure_user_config()
    assert path.read_text() == example.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    path.write_text("llm: {provider: local}\n")
    assert not af_config.ensure_user_config()
    assert "local" in path.read_text()
