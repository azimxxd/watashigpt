# WatashiGPT — Claude Code Instructions

## Project Overview

OS-level background assistant that intercepts selected text via global hotkeys, routes it through a 3-tier command system (prefix → keyword → LLM → fallback), and applies transformations. Single-file Python CLI with rich TUI.

## Project Structure

```
watashigpt/
├── action-middleware/
│   ├── main.py           # All application code (~1740 lines)
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

## Architecture & Conventions

- **Config-driven**: all commands defined in `config.yaml`, no hardcoding
- **LLM_MODE**: `"live"` or `"mock"` — set once at startup, governs all LLM behavior. In mock mode, LLM commands return `[MOCK] ...` placeholders without API calls. Built-in commands work in both modes.
- **3-tier routing**: prefix match → keyword match → LLM classification (live only) → fallback
- **Threading**: main thread runs cbreak stdin loop + TUI; hotkey callbacks spawn worker threads
- **Handler pattern**: `_BUILTIN_HANDLERS` dict maps command names → `handle_<command>()` functions
- **Platform abstraction**: auto-detects Wayland vs X11, uses native clipboard commands
- **`_run_as_user()`**: runs subprocess commands as the real user when executing under `sudo`
- **Provider registry**: `_PROVIDER_BASE_URLS` and `_PROVIDER_DEFAULT_MODELS` dicts for clean multi-provider support
- **Config persistence**: `_save_llm_config()` writes provider/key/model to `config.yaml` so subsequent runs skip the interactive selector

## TUI Features

- **Collapsible banner**: full ASCII art for 2s at startup, collapses to single-line header (`--banner` to keep)
- **Environment panel**: dim labels, bright cyan values, Mode row (LIVE/MOCK)
- **LLM panel**: green border + provider info in live mode, yellow/gold border + MOCK MODE in mock
- **Commands panel**: `[LLM]`/`[FAST]` badges, live usage counters, dimmed rows with `[MOCK]` suffix for LLM commands in mock mode
- **Activity feed**: color-coded rows (cyan=built-in, magenta=LLM, red=error) with timestamps and duration
- **Keybindings panel**: dynamic undo stack count
- **Micro-log**: rolling 3-line status bar at bottom
- **Command search**: press `/` in TUI to fuzzy-search commands by name, keywords, prefixes, description
- **Session export**: press `S` to dump current session activity to `~/watashigpt_session_<timestamp>.md`

## Code Style

- Type hints on function signatures
- Docstrings on key functions
- ANSI color codes for TUI output (via `TUI` class with thread-safe print locking)
- Section comments with separator lines to organize the single file
- Thread-safe undo stack with locking

## Key Commands

| Prefix | Action | LLM Required |
|--------|--------|-------------|
| `TR:` | Rude → professional text | No |
| `CMD:` / `RUN:` | Execute shell command | No |
| `TEST:` / `PING:` | Pipeline verification | No |
| `FMT:` / `FORMAT:` | Auto-format JSON | No |
| `COUNT:` / `STATS:` | Word/char/line stats | No |
| `MOCK:` / `SPONGE:` | Spongebob alternating caps | No |
| `B64:` / `BASE64:` | Base64 encode | No |
| `DECODE:` / `DB64:` | Base64 decode | No |
| `HASH:` / `SHA:` | SHA256 hex digest | No |
| `SUM:` / `TLDR:` | Summarize text | Yes |
| `RW:` / `REWRITE:` | Rewrite professionally | Yes |
| `EXP:` / `EXPLAIN:` | Explain in simple terms | Yes |
| `TRANS:` | Translate to target language | Yes |

## Adding New Commands

1. Add command definition to `config.yaml` with prefixes, keywords, and optionally `llm_required` + `llm_prompt`
2. For built-in (non-LLM) commands: add a `handle_<name>()` function and register in `_BUILTIN_HANDLERS`
3. LLM commands need only the config entry — they route through `handle_llm_command()` automatically

## LLM Providers

Supported providers (configured via interactive selector or `config.yaml`):

| Provider | Base URL | Default Model |
|----------|----------|---------------|
| groq | `api.groq.com/openai/v1` | `llama-3.3-70b-versatile` |
| openai | (native) | `gpt-4o-mini` |
| gemini | `generativelanguage.googleapis.com/v1beta/openai/` | `gemini-2.0-flash` |
| openrouter | `openrouter.ai/api/v1` | `meta-llama/llama-3.3-70b-instruct` |
| github | `models.inference.ai.azure.com` | `gpt-4o-mini` |
