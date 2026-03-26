# ActionFlow

OS-level background assistant that intercepts selected text via global hotkeys, routes it through a 3-tier command system (prefix → keyword → LLM classification → fallback), and applies transformations in-place. Single-file Python CLI with rich TUI, context-aware intelligence, command picker popup, and system tray support.

**by WatashiGPT**

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![Platform](https://img.shields.io/badge/Platform-Linux-green)
![Wayland](https://img.shields.io/badge/Wayland-Supported-purple)
![X11](https://img.shields.io/badge/X11-Supported-orange)

## How It Works

1. Select any text in any application
2. Press `Ctrl+Alt+X` — a command picker popup appears
3. Pick a command (or type a prefix like `POL:hello`) — the text is processed and replaced in-place
4. Press `Ctrl+Alt+Z` to undo

```
  ╭──────────────────────────────────────╮
  │  ▄▀█ █▀▀ ▀█▀ █ █▀█ █▄░█            │
  │  █▀█ █▄▄ ░█░ █ █▄█ █░▀█            │
  │                                      │
  │  █▀▀ █░░ █▀█ █░█░█                  │
  │  █▀░ █▄▄ █▄█ ▀▄▀▄▀                  │
  ╰──────────────────────────────────────╯
```

## Commands

### Built-in (no LLM required)

| Prefix | Action | Notes |
|--------|--------|-------|
| `POL:` / `POLITE:` | Rewrite rude/blunt text politely | Phrase lookup, LLM fallback |
| `CMD:` / `RUN:` | Execute shell command | Dangerous pattern blocking |
| `TEST:` / `PING:` | Pipeline verification | |
| `FMT:` / `FORMAT:` | Auto-format JSON/XML | JSON first, XML fallback |
| `COUNT:` / `STATS:` | Word/char/line stats + reading time | Notification only, no clipboard |
| `MOCK:` / `SPONGE:` | Spongebob alternating caps | |
| `B64:` / `BASE64:` | Base64 encode | |
| `DECODE:` / `DB64:` | Base64 decode | Error notification on invalid |
| `HASH:` / `SHA:` | SHA256 hex digest | |
| `REDACT:` / `PII:` | Mask PII (emails, phones, cards, IPs) | Regex-based |
| `CALC:` / `MATH:` | Safe math evaluator | Handles `15% of 340`, `sqrt(144)`, arithmetic |
| `DATE:` | Natural language date → ISO format | Uses `dateparser` |
| `ESCAPE:` / `ESC:` | Escape special characters | Auto-detects HTML/SQL/regex |
| `SANITIZE:` / `STRIP:` | Strip HTML/markdown/ANSI formatting | Auto-detects format type |
| `PASSWORD:` / `PW:` | Generate strong random password | |
| `REPEAT:` / `AGAIN:` | Re-run last command on current selection | |
| `CLIP:` | Named clipboard slots | `CLIP:save name` / `CLIP:load name` / `CLIP:list` |
| `STACK:` / `PUSH:` | Push clipboard onto stack | |
| `POP:` | Pop top item from clipboard stack | |
| `WIKI:` | Wikipedia article summary | Notification only |
| `DEFINE:` | Dictionary word definition | Notification only |
| `IMG:` / `IMAGE:` | Generate image from description | Via Pollinations.ai |

### LLM Commands (require a configured provider)

| Prefix | Action |
|--------|--------|
| `SUM:` / `TLDR:` | Summarize text |
| `RW:` / `REWRITE:` | Rewrite professionally |
| `EXP:` / `EXPLAIN:` | Explain in simple terms |
| `TONE:style:` | Dynamic tone rewriting (`TONE:casual:`, `TONE:formal:`, etc.) |
| `BULLETS:` / `LIST:` | Convert to bullet list |
| `TITLE:` / `HEADLINE:` | Generate short headline |
| `TWEET:` | Shorten to 280 chars |
| `EMAIL:` | Generate email from rough notes |
| `REGEX:` | Generate regex from description |
| `DOCSTRING:` / `DOC:` | Generate code docstring |
| `REVIEW:` / `CR:` | Quick code review |
| `GITCOMMIT:` / `COMMIT:` | Generate conventional commit message |
| `MEETING:` / `NOTES:` | Structure meeting notes |
| `TODO:` / `ACTIONS:` | Extract action items as checklist |
| `ELI5:` | Explain like I'm 5 |
| `HAIKU:` | Rewrite as haiku |
| `ROAST:` | Light roast of selected text |
| `FILL:` | Fill `{{placeholder}}` markers from context |
| `TRANS:lang:` | Translate to language (`TRANS:JP:`, `TRANS:ES:`, etc.) |

### Pipe Chains

Chain multiple commands by separating with `|`:

```
POL:|SUM: rude long text   →  rewrites politely, then summarizes
```

### Personal Commands

Define your own commands in `config.yaml` under `personal_commands:` with few-shot examples. They appear with a `[ME]` badge in the popup.

## Keybindings

| Key | Action |
|-----|--------|
| `Ctrl+Alt+X` | Intercept selected text → open command picker |
| `Ctrl+Alt+Z` | Undo last replacement |
| `Ctrl+Alt+S` | Toggle silent mode (suppress notifications) |
| `Ctrl+C` | Exit application |

### TUI Keys

| Key | Action |
|-----|--------|
| `/` | Fuzzy search commands |
| `S` | Export current session to markdown |

## LLM Providers

Configured via interactive selector at first startup, or directly in `config.yaml`.

| Provider | Default Model |
|----------|---------------|
| Groq | `llama-3.3-70b-versatile` |
| OpenAI | `gpt-4o-mini` |
| Gemini | `gemini-2.0-flash` |
| OpenRouter | `meta-llama/llama-3.3-70b-instruct` |
| GitHub Models | `gpt-4o-mini` |

- **Mock mode**: runs without any LLM provider — built-in commands work, LLM commands return `[MOCK]` placeholders
- **Confidence gating**: LLM classifier confidence below threshold (default `0.7`) skips the command with a notification
- **Fallback**: if primary provider errors, auto-retries with secondary provider from `config.yaml`
- **Per-command model override**: optional `model:` key per command in config

## Requirements

### System packages

```bash
# Wayland (GNOME/KDE/Sway)
sudo apt-get install wl-clipboard libnotify-bin python3-gi gir1.2-atspi-2.0

# X11
sudo apt-get install xclip xdotool libnotify-bin
```

### Python dependencies

```bash
pip install -r action-middleware/requirements.txt
```

| Package | Purpose |
|---------|---------|
| `keyboard` | Global hotkey detection (requires root on Linux) |
| `pyperclip` | Clipboard (macOS/Windows fallback) |
| `plyer` | Notifications (macOS/Windows fallback) |
| `pyyaml` | Config parsing |
| `openai` | LLM client (supports all providers via base_url) |
| `watchdog` | Config hot-reload on file change |
| `dateparser` | Natural language date parsing |
| `langdetect` | Automatic language detection |
| `pystray` | System tray icon |
| `Pillow` | System tray icon rendering |
| `dbus-python` | D-Bus session bus (paste helper, used by system Python) |
| `PyGObject` | GLib mainloop + AT-SPI accessibility (paste helper) |

## Usage

```bash
cd action-middleware

# Optional: set API keys before launch
export ACTIONFLOW_API_KEY="your_llm_key"
export ACTIONFLOW_IMAGE_API_KEY="your_image_key"   # optional, for IMG: command

# Run (requires root for keyboard access, -E preserves session env vars)
sudo -E python main.py

# Keep full ASCII banner permanently
sudo -E python main.py --banner

# Run without system tray icon
sudo -E python main.py --no-tray

# Browse history (last 50 entries)
python main.py --history
python main.py --history --grep TR    # filter by command
```

## Architecture

```
Hotkey (Ctrl+Alt+X)
    │
    ├─ Callback fires on keyboard listener thread
    │  └─ Spawns worker thread (non-blocking)
    │
    ├─ Worker thread:
    │  ├─ Read selection (wl-paste --primary on Wayland / Ctrl+C on X11)
    │  ├─ Detect app context via AT-SPI / xdotool (terminal/browser/IDE/chat/docs)
    │  ├─ Analyze text (language, code, formality, type)
    │  └─ Queue popup for main thread (or execute prefix command directly)
    │
    ├─ Main thread:
    │  ├─ Show command picker popup (tkinter Toplevel)
    │  ├─ Smart suggestions ranked by context + learned patterns
    │  ├─ Route: prefix match → keyword → LLM classify → fallback
    │  ├─ Process text through handler
    │  └─ Replace selection (clipboard + portal Ctrl+V paste)
    │
    ├─ Paste helper (separate user-space process):
    │  ├─ xdg-desktop-portal RemoteDesktop session for key injection
    │  ├─ AT-SPI window detection (focused window app/PID/title)
    │  ├─ D-Bus window activation (refocus source window after popup)
    │  └─ Clipboard read/write via wl-copy/wl-paste
    │
    └─ TUI output (thread-safe, timestamped, color-coded)
```

### Key design decisions

- **Config-driven**: all commands defined in `config.yaml`, no hardcoding
- **3-tier routing**: prefix match → keyword match → LLM classification → fallback
- **Non-blocking callbacks**: hotkey callbacks spawn threads and return immediately
- **Primary selection on Wayland**: reads highlighted text directly via `wl-paste --primary`
- **Portal-based paste**: uses xdg-desktop-portal RemoteDesktop to inject Ctrl+V — works on GNOME Wayland without wtype or uinput
- **AT-SPI window tracking**: detects focused window via accessibility bus, activates via D-Bus — works on GNOME 45+ where Shell.Eval is disabled
- **`sudo -E` with `_run_as_user()`**: runs as root for `/dev/input` access but clipboard/notification commands run as the original user
- **Pattern learning**: `PatternLearner` reads history, computes usage-frequency weights per app context after 20+ samples
- **Safe math eval**: `CALC:` uses `ast.parse()` + AST node whitelisting — never raw `eval()`

## Project Structure

```
watashigpt/
├── action-middleware/
│   ├── main.py              # All application code (~5100 lines)
│   ├── paste_helper.py      # Portal paste + AT-SPI window detection (runs as user)
│   ├── config.yaml.example  # Example config with all commands and settings
│   └── requirements.txt     # Python dependencies
├── .gitignore
└── README.md
```

## TUI

- **Collapsible banner**: full ASCII art for 2s at startup, collapses to single-line header (`--banner` to keep)
- **Environment panel**: mode (LIVE/MOCK), learning sample count
- **LLM panel**: green border in live mode, yellow in mock
- **Commands panel**: `[LLM]`/`[FAST]` badges, live usage counters
- **Activity feed**: color-coded rows with timestamps and duration
- **Micro-log**: rolling 3-line status bar
- **System tray**: color status icon (green=live, yellow=mock, grey=silent), right-click menu
