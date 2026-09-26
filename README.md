# ActionFlow

OS-level background assistant that intercepts selected text via global hotkeys, routes it through a 3-tier command system (prefix → keyword → LLM classification → fallback), and applies transformations in-place. Single-file Python CLI with rich TUI, context-aware intelligence, command picker popup, and system tray support.

**by WatashiGPT**

![Python](https://img.shields.io/badge/Python-3.9+-blue)
![Platform](https://img.shields.io/badge/Platform-Linux%20%7C%20macOS-green)
![Wayland](https://img.shields.io/badge/Wayland-Supported-purple)
![X11](https://img.shields.io/badge/X11-Supported-orange)

## How It Works

1. Select any text in any application
2. Press `Ctrl+Alt+X` (`⌃⌥X` on macOS) — a command picker popup appears
3. Pick a command (or type a prefix like `POL:hello`) — the text is processed and replaced in-place
4. Press `Ctrl+Alt+Z` (`⌃⌥Z`) to undo

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
| `CMD:` / `RUN:` | Execute a command (no shell) | Binary allowlist + blocked exec flags |
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
| Ollama (local, no key) | `llama3.2` |
| LM Studio (local, no key) | `local-model` |

Any other OpenAI-compatible endpoint works via `llm.base_url` in `config.yaml`.

- **Mock mode**: runs without any LLM provider — built-in commands work, LLM commands return `[MOCK]` placeholders
- **Confidence gating**: LLM classifier confidence below threshold (default `0.7`) skips the command with a notification
- **Fallback**: if primary provider errors, auto-retries with secondary provider from `config.yaml`
- **Failure safety**: if every provider fails, your text is left untouched and you get an error notification
- **Tuning**: `llm.max_tokens` (default 2048), `llm.temperature`, `llm.timeout` (seconds)
- **Per-command model override**: optional `model:` key per command in config

## macOS

No root, no system packages. Needs Python 3.9+ with **Tk 8.6+** for the command picker
(Apple's `/usr/bin/python3` ships Tk 8.5, which hangs — use python.org or Homebrew Python).

```bash
cd action-middleware
./run.sh            # creates .venv on first run (uses uv if installed), then starts
```

No suitable Python? `curl -LsSf https://astral.sh/uv/install.sh | sh` — `run.sh` then
creates a Python 3.12 venv with a bundled Tk automatically.

On first launch grant your terminal app (Terminal, iTerm2, VS Code, …) two permissions in
**System Settings → Privacy & Security**, then restart the terminal:

| Permission | Why |
|------------|-----|
| **Accessibility** | Send ⌘C / ⌘V to the focused app, swallow the hotkey |
| **Input Monitoring** | Listen for the global hotkey |

**Command palette** — a native Spotlight-style panel (AppKit, not Tk): vibrancy, SF Symbols,
light/dark mode. It never steals focus from the app you're typing in, so results are pasted
straight back.

| Key | In the list | In the result preview |
|-----|-------------|-----------------------|
| type | search commands — or write any instruction (“make it shorter, in English”) | refine the result |
| `↵` | run command / instruction | replace the selection (or apply the refinement) |
| `↑` `↓` / `⌘1`–`⌘9` | navigate / pick | — |
| `⇥` | — | regenerate |
| `⌘C` | — | copy instead of replacing |
| `esc` | close | back to the list |

AI results stream into the preview first — nothing touches your text until you press `↵`.

**Menu bar icon** (✨): mode, silent toggle, recent history, open/reload config, quit.

**API keys** are stored in the macOS Keychain — the first-run setup offers it, or:

```bash
python main.py --set-key groq                 # any provider name
python main.py --set-key image:pollinations
```

**Start at login** (runs in the menu bar, no terminal window):

```bash
python main.py --install      # LaunchAgent, logs → ~/Library/Logs/ActionFlow.log
python main.py --uninstall
```

The login agent runs Python directly, so grant Accessibility + Input Monitoring to the
Python binary path that `--install` prints (in addition to your terminal).

How it works on macOS: a Quartz event tap matches hotkeys by physical key (works with any
keyboard layout, e.g. Russian), the selection is captured with ⌘C while the full clipboard
(including images and rich text) is snapshotted and restored, the source app is re-activated
by PID via `NSRunningApplication`, and notifications go through Notification Center.

## Linux

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

`requirements.txt` uses platform markers, so the same file works on Linux and macOS.

| Package | Purpose |
|---------|---------|
| `keyboard` | Global hotkey detection (Linux, requires root) |
| `pyobjc-framework-Quartz` / `-ApplicationServices` | Hotkeys, key injection, clipboard, app focus (macOS) |
| `pyyaml` | Config parsing |
| `openai` | LLM client (supports all providers via base_url) |
| `watchdog` | Config hot-reload on file change |
| `dateparser` | Natural language date parsing |
| `langdetect` | Automatic language detection |
| `pystray` | System tray icon |
| `Pillow` | System tray icon rendering |
| `dbus-python` | D-Bus session bus (paste helper, used by system Python) |
| `PyGObject` | GLib mainloop + AT-SPI accessibility (paste helper) |

## Usage (Linux)

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
- **Privacy**: history stores only text lengths unless `history.log_text: true`; API keys live in env/keychain, never in `config.yaml`
- **Explicit intent**: without the popup, only prefixed text is processed (`smart_routing: true` enables keyword/LLM guessing)

## Project Structure

```
watashigpt/
├── action-middleware/
│   ├── main.py              # Application code (~5400 lines)
│   ├── platform_mac.py      # macOS backend (hotkeys, clipboard, focus, notifications)
│   ├── mac_ui.py            # macOS command palette + result view (AppKit)
│   ├── paste_helper.py      # Linux: portal paste + AT-SPI window detection (runs as user)
│   ├── config.yaml.example  # Example config — copied to config.yaml on first run
│   ├── requirements.txt     # Python dependencies (platform markers)
│   ├── requirements-dev.txt # + pytest
│   ├── run.sh               # Launcher: creates the venv, then starts the app
│   └── tests/               # pytest suite: python -m pytest tests
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
