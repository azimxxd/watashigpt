# WatashiGPT — Claude Code Instructions

## Project Overview

OS-level background assistant that intercepts selected text via global hotkeys, routes it through a 3-tier command system (prefix → keyword → LLM → fallback), and applies transformations. Single-file Python CLI with rich TUI.

## Project Structure

```
watashigpt/
├── action-middleware/
│   ├── main.py           # All application code (~900 lines)
│   ├── config.yaml       # Commands, hotkeys, LLM settings
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
```

## Testing

No automated test suite. Manual testing:
1. Run the app with `sudo -E python main.py`
2. Select `TEST:hello world` in any app, press `Ctrl+Alt+X`
3. Expect: `[TEST OK] "hello world" | session=... | wayland=...`

## Tech Stack

- **Python 3.10+** — single-file architecture
- **keyboard** — global hotkey detection (requires root on Linux)
- **pyperclip / plyer** — clipboard and notifications (macOS/Windows fallback)
- **pyyaml** — config parsing
- **openai** — LLM client (supports both Groq and OpenAI via base_url)

## Architecture & Conventions

- **Config-driven**: all commands defined in `config.yaml`, no hardcoding
- **3-tier routing**: prefix match → keyword match → LLM classification → fallback
- **Threading**: main thread runs keyboard listener + TUI; hotkey callbacks spawn worker threads
- **Handler pattern**: `_BUILTIN_HANDLERS` dict maps command names → `handle_<command>()` functions
- **Platform abstraction**: auto-detects Wayland vs X11, uses native clipboard commands
- **`_run_as_user()`**: runs subprocess commands as the real user when executing under `sudo`

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
| `SUM:` / `TLDR:` | Summarize text | Yes |
| `RW:` / `REWRITE:` | Rewrite professionally | Yes |
| `EXP:` / `EXPLAIN:` | Explain in simple terms | Yes |

## Adding New Commands

1. Add command definition to `config.yaml` with prefixes, keywords, and optionally `llm_required` + `llm_prompt`
2. For built-in (non-LLM) commands: add a `handle_<name>()` function and register in `_BUILTIN_HANDLERS`
3. LLM commands need only the config entry — they route through `handle_llm_command()` automatically
