# ActionFlow — Claude Code Instructions

## Project Overview

Hotkey text assistant: select text anywhere → hotkey → command palette (or prefix like `SUM:`) →
built-in transform or LLM → result pasted in place. macOS (native AppKit palette, menu bar,
LaunchAgent) and Linux (X11/Wayland, Tk picker, systemd). Windows is not supported.

**Developer: WatashiGPT**

## Layout

```
action-middleware/
├── main.py              # orchestration: state, hotkey intercept, dispatch/router, handlers, main loop
├── actionflow/          # everything else, one concern per module (see README "Development")
├── config.yaml.example  # copied to config.yaml (gitignored) on first run
├── run.sh               # launcher (creates .venv)
└── tests/test_core.py   # pytest — pure logic, no GUI/hotkeys/network
```

## Running & testing

```bash
cd action-middleware
./run.sh                            # macOS: no sudo. Linux: runs sudo -E
python main.py --check              # test LLM provider(s), list models
python main.py --set-key groq       # store a key in the keychain
python -m pytest tests              # after: pip install -r requirements-dev.txt
python -m pyflakes main.py actionflow/*.py   # catches undefined names in untestable Linux paths
```

macOS needs Accessibility + Input Monitoring for the terminal app. Manual E2E: select
`TEST:hello` anywhere, press ⌃⌥X → `[TEST OK] "hello" | session=... | llm=...`.

## Architecture & conventions

- **Modules** (`actionflow/`): `config` (CONFIG object — mutate, never rebind), `llm` (module
  state `llm.MODE/ready/provider/model/fallback_*`; always access as `llm.X`, never
  `from llm import X`), `prompts` (all prompt text — add prompt logic here), `textops` (pure
  transforms; handlers in main.py only do undo/paste/notify), `analysis`, `palette`, `history`,
  `setup_wizard`, `service`, `tui`, `tk_ui`, `mac_ui`, `platform_mac`, `platform_linux`.
- **Platform facades** in main.py (`clipboard_copy`, `_send_paste_keys`, `detect_active_window`,
  `_focus_window`, `notify`, …) call `mac.*` or `linux.*`. OS code never goes in main.py.
  Keep PyObjC imports lazy in `platform_mac` so tests import anywhere.
- **Flow**: hotkey thread → `_do_intercept()` (holds `_job_lock`: one selection at a time) →
  prefix? `route()` → `dispatch()` : queue palette for the main thread → `_handle_popup()` →
  releases `_job_lock`.
- **dispatch() contract**: returns replacement text, `""` if nothing replaced, `None` on failure
  (errors notified, never re-raised). Chains use the return value and pop intermediate undo
  entries. Per-command `notify:` level is reset after each dispatch.
- **Handlers**: `_BUILTIN_HANDLERS` maps names → `handle_<name>(text, full_text, cmd_config)`;
  other commands go to `handle_llm_command()` using `llm_prompt` from config.
- **LLM**: `llm.call()` / `llm.stream()` try primary then fallback, raise `LLMError` — never paste
  placeholders over user text. With no LLM, AI commands notify and do nothing (`_llm_unavailable`).
  `request_options()` turns reasoning down per provider; `<think>` blocks are stripped.
  Provider defaults live in `llm.PROVIDERS`; retired ones in `RETIRED_PROVIDERS/MODELS`.
- **macOS palette**: `mac_ui.CommandPalette` (non-activating NSPanel) is UI only;
  `palette.PaletteController` returns `run` / `stream` / `submenu` / `message` per item.
  Accepted previews go through `_commit_generated()`. AppKit/Tk only on the main thread —
  use `_run_on_main()` from worker threads.
- **Config writes**: `config.save_values()` / `save_nested()` edit lines in place to keep the
  user's comments. API keys: env → config → keyring (`ActionFlow`, `llm:<provider>`,
  `image:<provider>`); never written to config.yaml.
- **Privacy**: history stores lengths only unless `history.log_text`; `SENSITIVE_COMMANDS` never
  stored; passwords are not shown in notifications.
- **Security**: `CMD:` = allowlist (`command_security.allowed_commands`) + `_CMD_BLOCKED_ARGS`,
  no shell, cwd=$HOME. Without a popup, keyword/LLM routing is opt-in (`smart_routing`).

## Code style

Type hints on signatures, docstrings on non-obvious functions, section banners in main.py,
`from __future__ import annotations` everywhere (Python 3.9 support).

## Adding commands

1. Add it to `config.yaml.example` (prefixes, keywords, description, optional `llm_required` +
   `llm_prompt` with `{text}` and context vars).
2. Built-in: pure logic in `textops.py`, a thin `handle_<name>()` in main.py registered in
   `_BUILTIN_HANDLERS`, a `COMMAND_META` entry (title, SF Symbol, tint) in `palette.py`, tests.
3. LLM commands need only the config entry (+ `COMMAND_META` for a nice palette row).

## Key Commands — Built-in (no LLM, work in both modes)

| Prefix | Action | Notes |
|--------|--------|-------|
| `POL:` / `POLITE:` | Rewrite rude/blunt text politely | Phrase lookup + LLM fallback |
| `CMD:` / `RUN:` | Execute a command (no shell) | `command_security.allowed_commands` + `_CMD_BLOCKED_ARGS` |
| `TEST:` / `PING:` | Pipeline verification | |
| `FMT:` / `FORMAT:` | Auto-format JSON/XML | JSON first, XML fallback |
| `COUNT:` / `STATS:` | Word/char/line stats + reading time | Notification only, no clipboard |
| `MOCK:` / `SPONGE:` | Spongebob alternating caps | |
| `B64:` / `BASE64:` | Base64 encode | |
| `DECODE:` / `DB64:` | Base64 decode | Error notification on invalid |
| `HASH:` / `SHA:` | SHA256 hex digest | Also shows in notification |
| `REDACT:` / `PII:` | Mask PII (emails, phones, cards, IPs) | Regex-based → `[EMAIL]`, `[PHONE]`, `[CARD]`, `[IP]` |
| `CALC:` / `MATH:` | Safe math evaluator | Handles `15% of 340`, `sqrt(144)`, arithmetic |
| `DATE:` | Natural language date → ISO format | Uses `dateparser` library |
| `ESCAPE:` / `ESC:` | Escape special characters | Auto-detects HTML/SQL/regex, or use `ESCAPE:html:` prefix |
| `SANITIZE:` / `STRIP:` | Strip HTML/markdown/ANSI formatting | Auto-detects format type |
| `PASSWORD:` / `PW:` | Generate strong random password | Length configurable in config, shows first 4 chars |
| `REPEAT:` / `AGAIN:` | Re-run last command on current selection | |
| `CLIP:` | Named clipboard slots | `CLIP:save name` / `CLIP:load name` / `CLIP:list` |
| `STACK:` / `PUSH:` | Push clipboard onto stack | Shows stack depth |
| `POP:` | Pop top item from clipboard stack | |
| `WIKI:` | Wikipedia article summary | Notification only, no clipboard |
| `DEFINE:` | Dictionary word definition | Notification only, no clipboard |

## Key Commands — LLM (disabled/mocked in MOCK mode)

| Prefix | Action |
|--------|--------|
| `SUM:` / `TLDR:` | Summarize text |
| `RW:` / `REWRITE:` | Rewrite professionally |
| `EXP:` / `EXPLAIN:` | Explain in simple terms |
| `TONE:` | Dynamic tone rewriting (`TONE:casual:`, `TONE:formal:`, etc.) |
| `BULLETS:` / `LIST:` | Convert text to bullet list |
| `TITLE:` / `HEADLINE:` | Generate short headline |
| `TWEET:` | Shorten to 280 chars |
| `EMAIL:` | Generate email from rough notes |
| `REGEX:` | Generate regex from description |
| `DOCSTRING:` / `DOC:` | Generate code docstring (auto-detects language) |
| `REVIEW:` / `CR:` | Quick code review |
| `GITCOMMIT:` / `COMMIT:` | Generate conventional commit message |
| `MEETING:` / `NOTES:` | Structure meeting notes (Summary/Decisions/Actions/Follow-ups) |
| `TODO:` / `ACTIONS:` | Extract action items as checklist |
| `ELI5:` | Explain like I'm 5 |
| `HAIKU:` | Rewrite as haiku |
| `ROAST:` | Light roast of selected text |
| `FILL:` | Fill `{{placeholder}}` markers from context |
| `TRANS:` | Translate to target language (`TRANS:JP:`, `TRANS:ES:`, etc.) |
