# ActionFlow — Claude Code Instructions

## Project Overview

OS-level background assistant that intercepts selected text via global hotkeys, routes it through a 3-tier command system (prefix → keyword → LLM → fallback), and applies transformations. Single-file Python CLI with rich TUI, context-aware intelligence, and system tray support. 38+ commands (19 built-in + 19 LLM + personal commands).

**Developer: WatashiGPT**

## Project Structure

```
watashigpt/
├── action-middleware/
│   ├── main.py           # All application code (~3900 lines)
│   ├── config.yaml       # Commands, hotkeys, LLM settings (auto-saved on provider setup)
│   └── requirements.txt  # Python dependencies
└── README.md
```

## Running

```bash
# Install system deps (Wayland)
sudo apt-get install wl-clipboard libnotify-bin

# Install Python deps
pip install -r action-middleware/requirements.txt

# Run (requires root for keyboard access, -E preserves user session env vars)
cd action-middleware
sudo -E python main.py

# Keep full ASCII banner permanently
sudo -E python main.py --banner

# Browse history (last 50 entries)
python main.py --history
python main.py --history --grep TR   # filter by command

# Run without system tray icon
sudo -E python main.py --no-tray
```

## Testing

No automated test suite. Manual testing:
1. Run the app with `sudo -E python main.py`
2. Select `TEST:hello world` in any app, press `Ctrl+Alt+X`
3. Expect: `[TEST OK] "hello world" | session=... | wayland=... | llm=mock`

## Tech Stack

- **Python 3.10+** — single-file architecture
- **keyboard** — global hotkey detection (requires root on Linux)
- **pyperclip / plyer** — clipboard and notifications (macOS/Windows fallback)
- **pyyaml** — config parsing
- **openai** — LLM client (supports Groq, OpenAI, Gemini, OpenRouter, GitHub Models via base_url)
- **dateparser** — natural language date parsing (for `DATE:` command)
- **watchdog** — config hot-reload on file change
- **langdetect** — automatic language detection for text analysis
- **pystray / Pillow** — system tray icon (optional)

## Architecture & Conventions

- **Config-driven**: all commands defined in `config.yaml`, no hardcoding
- **LLM_MODE**: `"live"` or `"mock"` — set once at startup, governs all LLM behavior. In mock mode, LLM commands return `[MOCK] ...` placeholders without API calls. Built-in commands work in both modes.
- **3-tier routing**: prefix match → keyword match → LLM classification (live only) → fallback
- **Confidence gating**: LLM classifier returns a confidence score; if below `confidence_threshold` (default `0.7` in config.yaml), the command is NOT applied — user sees a notification suggesting to use the prefix directly
- **Threading**: main thread runs cbreak stdin loop + TUI; hotkey callbacks spawn worker threads
- **Handler pattern**: `_BUILTIN_HANDLERS` dict maps command names → `handle_<command>()` functions. LLM commands route through `handle_llm_command()` automatically.
- **Platform abstraction**: auto-detects Wayland vs X11, uses native clipboard commands
- **`_run_as_user()`**: runs subprocess commands as the real user when executing under `sudo`
- **Provider registry**: `_PROVIDER_BASE_URLS` and `_PROVIDER_DEFAULT_MODELS` dicts for clean multi-provider support
- **Config persistence**: `_save_llm_config()` writes provider/key/model to `config.yaml` so subsequent runs skip the interactive selector
- **Pipe chains**: `TR:|SUM: text` chains multiple commands, passing output of each step to the next
- **REPEAT tracking**: `dispatch()` stores `_last_command` for the `REPEAT:` command (skips tracking repeat itself)
- **Named clips**: `~/.actionflow_clips.json` persists named clipboard slots across restarts
- **Clipboard stack**: in-memory `_clipboard_stack` list for `STACK:`/`POP:` push/pop operations
- **Safe math eval**: `CALC:` uses `ast.parse()` + AST node whitelisting — never raw `eval()`
- **Undo**: CTRL+ALT+Z restores the original text by writing it to clipboard and pasting (not Ctrl+Z). Notification: "Undone · restored previous text"
- **History log**: `~/.actionflow_history.jsonl` with `ts`, `command`, `input`, `output`, `duration_ms`, `provider`, `app_context`, `text_length`, `text_language`, `trigger` fields
- **LLM fallback**: if primary provider errors/times out, auto-retry with secondary provider configured in `config.yaml` under `llm.fallback`
- **Per-command model override**: optional `model:` key per command in config.yaml overrides the global model
- **Notification level**: optional `notify:` field per command (`always` | `errors_only` | `never`) to suppress desktop notifications for noisy commands
- **Auto-update**: background thread checks GitHub releases on startup; shows micro-log notice if newer `__version__` available
- **`--history` CLI**: `python main.py --history [--grep CMD]` prints last 50 JSONL entries as formatted table
- **AppContext**: `detect_active_window()` identifies terminal/browser/IDE/chat/docs context via xdotool/kdotool/swaymsg
- **TextAnalysis**: `analyze_text()` heuristically detects language, code, formality, text type (no LLM)
- **Smart suggestions**: `get_smart_suggestions()` scores commands by app context + text type + learned patterns
- **PatternLearner**: reads history JSONL, computes usage-frequency weights per context; influences suggestion order after 20+ samples
- **Personal commands**: `personal_commands:` in config.yaml with few-shot examples, `[ME]` badge in popup, `handle_personal_command()` handler
- **Refinement dialog**: `RefinementDialog` (tkinter) shown after LLM commands for iterative refinement (max 3 iterations)
- **System tray**: `pystray` icon with color status (green=live, yellow=mock, grey=silent), right-click menu for history/settings/reload/exit
- **Silent mode**: `Ctrl+Alt+S` toggles notification suppression; `silent_mode:` in config.yaml

## TUI Features

- **Collapsible banner**: full ASCII art for 2s at startup, collapses to single-line header (`--banner` to keep)
- **Environment panel**: dim labels, bright cyan values, Mode row (LIVE/MOCK), Learning row (sample count)
- **LLM panel**: green border + provider info in live mode, yellow/gold border + MOCK MODE in mock
- **Commands panel**: `[LLM]`/`[FAST]` badges, live usage counters, dimmed rows with `[MOCK]` suffix for LLM commands in mock mode
- **Activity feed**: color-coded rows (cyan=built-in, magenta=LLM, red=error) with timestamps and duration
- **Keybindings panel**: dynamic undo stack count
- **Micro-log**: rolling 3-line status bar at bottom
- **Command search**: press `/` in TUI to fuzzy-search commands by name, keywords, prefixes, description
- **Session export**: press `S` to dump current session activity to `~/actionflow_session_<timestamp>.md`

## Code Style

- Type hints on function signatures
- Docstrings on key functions
- ANSI color codes for TUI output (via `TUI` class with thread-safe print locking)
- Section comments with separator lines to organize the single file
- Thread-safe undo stack with locking

## Key Commands — Built-in (no LLM, work in both modes)

| Prefix | Action | Notes |
|--------|--------|-------|
| `TR:` | Rude → professional text | Phrase lookup from config |
| `CMD:` / `RUN:` | Execute shell command | Dangerous pattern blocking |
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

## Adding New Commands

1. Add command definition to `config.yaml` with prefixes, keywords, and optionally `llm_required` + `llm_prompt`
2. For built-in (non-LLM) commands: add a `handle_<name>()` function and register in `_BUILTIN_HANDLERS`
3. LLM commands need only the config entry — they route through `handle_llm_command()` automatically
4. Commands with dynamic prefix parsing (like `TONE:style:` or `TRANS:lang:`) need custom handlers

## LLM Providers

Supported providers (configured via interactive selector or `config.yaml`):

| Provider | Base URL | Default Model |
|----------|----------|---------------|
| groq | `api.groq.com/openai/v1` | `llama-3.3-70b-versatile` |
| openai | (native) | `gpt-4o-mini` |
| gemini | `generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.0-flash` |
| openrouter | `openrouter.ai/api/v1` | `meta-llama/llama-3.3-70b-instruct` |
| github | `models.inference.ai.azure.com` | `gpt-4o-mini` |
