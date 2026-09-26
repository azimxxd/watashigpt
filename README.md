# ActionFlow

Select text anywhere, press a hotkey, and rewrite / translate / summarize / format it in place —
with a free LLM or 20+ built-in tools. A Spotlight-style command palette on macOS, a Tk picker on Linux.

**by WatashiGPT**

![Python](https://img.shields.io/badge/Python-3.9+-blue)
![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Linux-green)

## How it works

1. Select text in any app
2. Press `⌃⌥X` (macOS) / `Ctrl+Alt+X` (Linux) — the command palette opens
3. Pick a command, or just **type an instruction** (“make it shorter, in English”) and press `↵`
4. AI results stream into a preview — `↵` replaces your selection, `⇥` regenerates,
   typing refines it, `⌘C` copies instead
5. `⌃⌥Z` undoes the last replacement

Prefer the keyboard? Start the selection with a prefix (`SUM: long text`, `TRANS:EN: привет`)
and the hotkey runs it immediately. Chain with `|`: `POL:|SUM: text`.

## Quick start

### macOS

```bash
cd action-middleware
./run.sh            # creates .venv on first run (uses uv if installed), then starts
```

No suitable Python? `curl -LsSf https://astral.sh/uv/install.sh | sh` — `run.sh` then
creates a Python 3.12 venv automatically.

On first launch:
1. Pick an LLM provider (free ones need no card) and paste the key — it's verified and saved to the Keychain.
2. Grant your terminal app **Accessibility** and **Input Monitoring**
   (System Settings → Privacy & Security), then restart the terminal.

| Permission | Why |
|------------|-----|
| Accessibility | Send ⌘C / ⌘V to the focused app, swallow the hotkey |
| Input Monitoring | Listen for the global hotkey |

Start at login (menu bar icon, no terminal window): `python main.py --install`
(logs: `~/Library/Logs/ActionFlow.log`, remove with `--uninstall`). The login agent runs Python
directly, so grant the two permissions to the Python path `--install` prints.

### Linux (X11 / Wayland)

```bash
sudo apt-get install wl-clipboard libnotify-bin python3-gi gir1.2-atspi-2.0   # Wayland
sudo apt-get install xclip xdotool libnotify-bin                              # X11
cd action-middleware && ./run.sh      # runs with sudo -E (the keyboard library reads /dev/input)
```

Service: `sudo -E python main.py --install` (API key goes in `/etc/actionflow.env`).
On GNOME Wayland a helper process uses the xdg-desktop-portal to paste.

Windows is not supported.

## LLM providers

All OpenAI-compatible; defaults checked September 2026. Reasoning/“thinking” is turned off or
down automatically — text edits need speed.

| Provider | Free | Default model | Key |
|----------|------|---------------|-----|
| **Groq** | ✓ | `openai/gpt-oss-120b` | [console.groq.com/keys](https://console.groq.com/keys) |
| **Google Gemini** | ✓ | `gemini-3.5-flash-lite` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| **Cerebras** | ✓ | `gpt-oss-120b` | [cloud.cerebras.ai](https://cloud.cerebras.ai) |
| **OpenRouter** | ✓ (`:free` models) | `qwen/qwen3.8-27b:free` | [openrouter.ai/keys](https://openrouter.ai/keys) |
| OpenAI | paid | `gpt-6-luna` | [platform.openai.com](https://platform.openai.com/api-keys) |
| Ollama / LM Studio | local | `qwen3.5:9b` / loaded model | — |

- **Backup provider** — setup offers a second free provider; it takes over when the first fails or hits a rate limit.
- **Keys** — env var (`ACTIONFLOW_API_KEY`) → `config.yaml` → system keychain. Save one: `python main.py --set-key groq`.
- **Check** — `python main.py --check` pings the configured providers and lists the models your key can use.
- **Any endpoint** — set `llm.base_url`; extra API params via `llm.request_options`.
- **Failures never touch your text** — you get an error and the selection stays as it was.
- Retired upstream: GitHub Models (July 2026), `gemini-2.0-flash` (auto-replaced).

## Commands

### Built-in (no LLM required)

| Prefix | Action | Notes |
|--------|--------|-------|
| `POL:` / `POLITE:` | Rewrite rude/blunt text politely | Phrase lookup, LLM fallback |
| `CMD:` / `RUN:` | Execute a command (no shell) | Binary allowlist + blocked exec flags |
| `TEST:` / `PING:` | Pipeline verification | |
| `FMT:` / `FORMAT:` | Pretty-print JSON / YAML / XML | `min:` / `sort:` modes |
| `COUNT:` / `STATS:` | Word/char/line stats + reading time | Shown in a popup, text unchanged |
| `MOCK:` / `SPONGE:` | Spongebob alternating caps | |
| `B64:` / `BASE64:` | Base64 encode | |
| `DECODE:` / `DB64:` | Base64 decode | Error notification on invalid |
| `HASH:` / `SHA:` | SHA256 hex digest | |
| `REDACT:` / `PII:` | Mask emails, phones, cards, IBANs, IPs, API keys | Regex-based |
| `CALC:` / `MATH:` | Safe math evaluator | Handles `15% of 340`, `sqrt(144)`, arithmetic |
| `DATE:` | Natural language date → ISO format | Uses `dateparser` |
| `ESCAPE:` / `ESC:` | Escape special characters | Auto-detects HTML/SQL/regex |
| `SANITIZE:` / `STRIP:` | Strip HTML/markdown/ANSI formatting | Auto-detects format type |
| `PASSWORD:` / `PW:` | Generate strong random password | |
| `REPEAT:` / `AGAIN:` | Re-run last command on current selection | |
| `CLIP:` | Named clipboard slots | `CLIP:save name` / `CLIP:load name` / `CLIP:list` |
| `STACK:` / `PUSH:` | Push clipboard onto stack | |
| `POP:` | Pop top item from clipboard stack | |
| `WIKI:` | Wikipedia article summary | Shown in a popup |
| `DEFINE:` | Dictionary word definition | Shown in a popup |
| `IMG:` / `IMAGE:` | Generate an image and paste it | Pollinations.ai — free without a key |

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
| `TRANS:lang:` | Translate (`TRANS:EN:`, `TRANS:Kazakh:`, `TRANS:Brazilian Portuguese:`) |

### Pipe Chains

Chain multiple commands by separating with `|`:

```
POL:|SUM: rude long text   →  rewrites politely, then summarizes
```

### Personal Commands

Define your own commands in `config.yaml` under `personal_commands:` with few-shot examples. They appear with a `[ME]` badge in the popup.

## Keys

| Key | Action |
|-----|--------|
| `⌃⌥X` / `Ctrl+Alt+X` | Process selection (palette or prefix command) |
| `⌃⌥Z` / `Ctrl+Alt+Z` | Undo last replacement |
| `⌃⌥S` / `Ctrl+Alt+S` | Toggle notifications |

In the palette: type to search or write an instruction · `↑↓` / `⌘1–9` pick · `↵` run ·
`esc` back/close. In the preview: `↵` replace (or apply the refinement you typed) · `⇥` retry ·
`⌘C` copy. In the terminal: `/` search commands · `S` export session to Markdown.

## Privacy

- Built-in commands never leave your machine; AI commands send the selection to *your* chosen provider.
- History (`~/.actionflow_history.jsonl`, mode 600, auto-trimmed) stores only text lengths unless `history.log_text: true`.
- API keys live in env vars or the keychain — never written to `config.yaml`.
- `CMD:` runs only allow-listed read-only binaries, without a shell.

## Development

```bash
cd action-middleware
pip install -r requirements-dev.txt
python -m pytest tests
```

```
action-middleware/
├── main.py                 # app: hotkeys → capture → dispatch → handlers → paste, main loop
├── actionflow/
│   ├── config.py           # defaults, config.yaml loading, comment-preserving saves
│   ├── llm.py              # providers, calls, streaming, fallback, API keys
│   ├── prompts.py          # every LLM prompt is built here
│   ├── setup_wizard.py     # first-run setup, --set-key, --check
│   ├── textops.py          # pure built-in transforms (calc, redact, format, …)
│   ├── analysis.py         # app context, text analysis, suggestions, PatternLearner
│   ├── palette.py          # command metadata + palette controller
│   ├── mac_ui.py           # macOS command palette (AppKit)
│   ├── platform_mac.py     # macOS hotkeys, clipboard, focus, notifications, menu bar
│   ├── platform_linux.py   # Linux X11/Wayland equivalents + systemd service
│   ├── paste_helper.py     # GNOME Wayland portal helper (runs as the user)
│   ├── tk_ui.py            # Linux Tk picker
│   ├── tui.py              # terminal output
│   ├── history.py          # history log, export, --history
│   └── service.py          # macOS LaunchAgent
├── config.yaml.example     # copied to config.yaml on first run
├── run.sh                  # launcher
└── tests/test_core.py
```
