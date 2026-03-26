# ActionFlow

Cross-platform OS-level text action middleware for Windows and Linux.

It now runs as a desktop background app first: a tray icon, GUI settings, history/log viewers, setup dialogs, command picker, and result windows sit on top of the existing hotkey + clipboard engine. It watches global hotkeys, captures the current text selection, routes that text through built-in or LLM-backed commands, and pastes the result back into the focused app. Pipelines like `TR:EN:|SUM:` are supported, along with undo, repeat, named clips, stack push/pop, quiet notifications, picker selection, and tray/background mode.

## Branch Purpose

This desktop-oriented line of development is intended to live on a separate branch so it does not disturb the original upstream baseline.

- recommended branch name: `windows-desktop`
- upstream baseline repository: `https://github.com/azimxxd/watashigpt`
- purpose: keep the Windows/Linux desktop GUI, tray, background runtime, and setup UX work isolated from the original console-first version

This branch adds:

- tray app / background desktop launcher
- GUI settings
- history and logs windows
- setup dialogs for LLM and image credentials
- windowless startup on Windows via `ActionFlow.pyw`
- startup/runtime logging via `.actionflow_startup.log` and `.actionflow.log`
- improved Windows runtime behavior with shared Linux support path

## Current State / MVP Status

This branch is now a desktop/background MVP rather than a pure console tool.

What is working:

- tray-first desktop app flow for Windows and Linux
- GUI/background startup plus legacy debug console mode
- global hotkeys, selection capture, clipboard backup/restore, and in-place replace flow
- command picker without prefix
- result windows for commands such as `COUNT`, `WIKI`, and `DEFINE`
- GUI settings, first-run setup, history view, logs view, and startup tracing

Current limitations:

- hotkey reliability still depends on OS permissions, keyboard backend behavior, and desktop session details
- Linux tray/hotkeys can vary between X11 and Wayland environments
- Windows `pythonw.exe` startup should be inspected through log files if tray startup fails
- this is a desktop-focused branch, not the untouched upstream baseline

LLM commands now run through a strict transform-only layer instead of ad-hoc chat prompts. Every LLM transform command follows the same contract:

- parse only the payload after the command prefix
- resolve command arguments such as target language, tone, or fill variables
- build a deterministic transform prompt
- strip assistant boilerplate from model output
- validate the output shape and retry once if needed
- return only the final transformed text

## What Changed

The project now has a split architecture instead of a Linux-only monolith:

```text
action-middleware/
  actionflow/
    app/
      main.py
      hotkeys.py
      tray.py
      runtime.py
    core/
      command_registry.py
      command_router.py
      pipeline.py
      clipboard_stack.py
      history.py
      text_ops.py
      config.py
      models.py
      llm/
        command_specs.py
        output_cleaner.py
        prompt_builder.py
        transform_executor.py
        validators.py
      commands/
        builtin.py
        text.py
        system.py
        external.py
        llm.py
    platform/
      base.py
      linux.py
      windows.py
  tests/
```

- `core/` contains command catalog, parsing, pipelines, history, stack/clip stores, transform execution, and pure text helpers.
- `platform/` contains Linux and Windows adapters for hotkeys, clipboard, active window detection, paste flow, notifications, and OS integration.
- `app/` is the runtime/orchestration layer and compatibility entrypoint.

## Supported Platforms

### Windows

- Global hotkeys via `keyboard`
- Clipboard read/write via `pyperclip` or Win32 clipboard API fallback
- Selected text capture via transactional `Ctrl+C` clipboard workflow with sequence detection, retries, and timeout handling
- Paste-back via transactional `Ctrl+V` flow with clipboard restore
- Foreground window detection via Win32 APIs
- Tray/notifications supported

### Linux

- Existing Linux behavior preserved
- Wayland/X11 utilities remain supported, but only inside `platform/linux.py`
- Clipboard flows still support `wl-clipboard`, `xclip`, and `wtype` fallback logic

## Commands

### Built-in text/system/external

| Command | Aliases |
|---|---|
| `test` | `TEST:`, `PING:` |
| `fmt` | `FMT:`, `FORMAT:` |
| `count` | `COUNT:`, `STATS:` |
| `mock` | `MOCK:`, `SPONGE:` |
| `b64` | `B64:`, `BASE64:` |
| `decode` | `DECODE:`, `DB64:` |
| `hash` | `HASH:`, `SHA:` |
| `redact` | `REDACT:`, `PII:` |
| `calc` | `CALC:`, `MATH:` |
| `date` | `DATE:` |
| `escape` | `ESCAPE:`, `ESC:` |
| `sanitize` | `SANITIZE:`, `STRIP:`, `CLEAN:` |
| `password` | `PASSWORD:`, `PASSWD:`, `PW:` |
| `clip` | `CLIP:` |
| `stack` | `STACK:`, `PUSH:` |
| `pop` | `POP:` |
| `repeat` | `REPEAT:`, `AGAIN:` |
| `command` | `CMD:`, `RUN:`, `EXEC:` |
| `wiki` | `WIKI:` |
| `define` | `DEFINE:` |
| `image` | `IMG:`, `IMAGE:` |

`IMAGE:` uses the image backend configured in `image_generation`. The default path targets Pollinations via `https://gen.pollinations.ai/image`. If the provider responds with `401`, ActionFlow now fails honestly, prompts for an image API key when possible, stores it in `~/.actionflow_secrets.yaml`, and retries the request instead of pretending the command succeeded.

### LLM commands

| Command | Aliases |
|---|---|
| `translate` | `TR:<LANG>:` |
| `trans` | `TRANS:<LANG>:` |
| `summarize` | `SUM:`, `TLDR:` |
| `rewrite` | `RW:`, `REWRITE:` |
| `explain` | `EXP:`, `EXPLAIN:` |
| `tone` | `TONE:<style>:` |
| `bullets` | `BULLETS:`, `LIST:` |
| `title` | `TITLE:`, `HEADLINE:` |
| `tweet` | `TWEET:` |
| `email` | `EMAIL:` |
| `regex` | `REGEX:` |
| `docstring` | `DOCSTRING:`, `DOC:` |
| `review` | `REVIEW:`, `CR:` |
| `gitcommit` | `GITCOMMIT:`, `COMMIT:` |
| `meeting` | `MEETING:`, `NOTES:` |
| `todo` | `TODO:`, `ACTIONS:` |
| `eli5` | `ELI5:` |
| `haiku` | `HAIKU:` |
| `roast` | `ROAST:` |
| `fill` | `FILL:` |

## Transform-Only Semantics

These commands are handled as strict text transformations, not chat replies:

- `TR:<LANG>:` and `TRANS:<LANG>:` translate only the payload and return only the translation
- `RW:` rewrites only the payload, preserves the source language, and avoids assistant-style replies
- `SUM:` returns only a concise summary
- `EXP:` returns only the explanation
- `TONE:<style>:` changes only the tone while preserving meaning
- `BULLETS:` / `LIST:` return only a bullet list
- `TITLE:` / `HEADLINE:` return only the title
- `EMAIL:` returns only the email draft
- `REGEX:` returns only the regex
- `DOCSTRING:` returns only the docstring
- `REVIEW:` returns concise review bullets
- `GITCOMMIT:` returns only the commit message

Common assistant boilerplate such as `Sure`, `Here is the translation`, or `I'd be happy to help` is stripped automatically. If the cleaned output still fails the command contract, ActionFlow retries once with a stricter corrective prompt and then fails cleanly instead of inserting junk text.

## Command Notes

### `COUNT:` / `STATS:`

Returns realistic text stats:

- word count
- character count
- line count
- reading time based on word count instead of blindly rounding up to one minute

Examples:

```text
COUNT: hello
COUNT: a much longer paragraph with enough words to produce a real reading estimate
```

Short text now shows `< 5 sec`, `~10 sec`, `~20 sec`, or `< 1 min` when appropriate instead of always showing `~1 min`.

### `ESCAPE:` / `ESC:`

`ESCAPE:` is now explicit and predictable. By default it uses JSON-style string escaping, which is a good fit for safe insertion into code strings.

Supported modes:

- `ESCAPE:json: say "hello"`
- `ESCAPE:python: can't`
- `ESCAPE:regex: a+b?`
- `ESCAPE:html: <b>hello</b>`
- `ESCAPE:shell: hello world`
- `ESCAPE:sql: O'Reilly`

If no mode is provided, `json` is used.

### `CLIP:`

`CLIP:` is an internal named text store, not a replacement for `Ctrl+C`.

Supported operations:

```text
CLIP:save:draft: hello world
CLIP:load:draft
CLIP:list
CLIP:delete:draft
CLIP:clear
```

Behavior:

- `save` stores the given text under a stable name
- `load` inserts the saved text back into the focused app
- `list` shows saved clip names and sizes
- `delete` removes one named clip
- `clear` removes all saved clips

### `STACK:` / `PUSH:` / `POP:`

These commands now behave as a strict LIFO stack.

Examples:

```text
STACK: first item
PUSH: second item
POP:
```

Behavior:

- `STACK:` / `PUSH:` adds text to the stack
- `POP:` returns the most recently pushed item
- empty stack errors are explicit instead of silently doing the wrong thing

### `CMD:` / `RUN:` / `EXEC:`

`CMD:` runs an allowlisted system command and returns the real stdout or stderr. Chained shell operators, pipes, and redirects are blocked.

Examples:

```text
CMD: echo hello
CMD: dir
CMD: ls
```

Notes:

- `dir` is supported on Windows
- `ls` is supported on Linux
- output is no longer mixed with stale results from previous commands

### `WIKI:`

`WIKI:` now tries to be useful on ambiguous queries.

Behavior:

- answers in the same language as the query text
- Russian query text prefers Russian Wikipedia results
- English query text uses English Wikipedia results
- if the query resolves cleanly, it returns a short summary
- if the query is ambiguous, it prefers a likely non-disambiguation page
- if ambiguity is still significant, it returns a short list of likely pages instead of raw disambiguation noise

Example:

```text
WIKI: Python
WIKI: питон
```

### `DEFINE:`

`DEFINE:` now prefers safe, common definitions and filters obviously offensive or slang-first meanings by default.

Behavior:

- answers in the same language as the query text
- Russian queries return Russian definitions
- English queries return English definitions
- shows the most useful safe definition first
- includes a short list of alternative normal meanings when helpful
- avoids surfacing weird or NSFW definitions as the default result

Example:

```text
DEFINE: python
DEFINE: питон
```

### `EXP:` / `EXPLAIN:`

`EXP:` is now a real explain-mode command. It should explain, simplify, and expand meaning when needed instead of merely echoing the source with tiny edits.

Example:

```text
EXP: HTTP cache
```

### `EMAIL:`

`EMAIL:` now aims for a clean, single-language draft.

Behavior:

- keeps the email in the source language unless you explicitly ask for another one
- avoids mixed-language subject/body output
- strips assistant-style framing

### `HAIKU:`

`HAIKU:` turns the source idea into a short haiku-style poem and returns only the poem, usually in three short lines.

Example:

```text
HAIKU: a calm morning while coding
```

### `FILL:`

`FILL:` fills template placeholders such as `{{name}}` and `{{role}}`.

Preferred syntax:

```text
FILL:name=Aldiyar|role=founder: My name is {{name}} and I am a {{role}}
```

Behavior:

- explicit assignments are applied deterministically
- unresolved placeholders are treated as an error when the command had enough information
- output is only the completed text

### Rewrite Examples

`RW:` is intended to do real paraphrasing, not just punctuation cleanup. The default rewrite strength is `strong`, which means short or underwritten text is allowed to expand slightly into a better-formed sentence while preserving meaning and language.

Examples:

- before: `RW:программирование это легко`
- after: `Программирование может быть гораздо проще, чем кажется на первый взгляд, особенно если изучать его последовательно и на понятных примерах.`
- before: `RW:Programming is easy`
- after: `Programming can be much easier to learn and practice than many people expect, especially when concepts are explained clearly and approached step by step.`

Rewrite strength can be configured in `config.yaml`:

```yaml
commands:
  rewrite:
    strength: strong
```

Supported levels are `light`, `normal`, and `strong`.

## Prefix Parsing

Explicit prefix commands bypass the picker and execute immediately. Examples:

```text
RW: rough draft
TRANS:JA: Good morning
TR:EN: Privet
TONE:formal: hey there
FILL:name=Aldiyar|role=founder: Hello {{name}} from {{role}}
```

The picker is only shown when the selected text does not already contain a recognized command prefix or pipeline.

## Pipelines

Examples:

```text
TR:EN:|SUM: grubyj dlinnyj chernovik
SANITIZE:|COUNT: <b>messy</b> text
TRANS:EN:|TONE:formal: privet brat
```

Each stage receives the output of the previous stage.

## Recommended End-User Launch Path

The primary UX is now a packaged desktop app, not a terminal command.

- Windows: launch `ActionFlow.exe`
- Linux: launch the packaged desktop app through a `.desktop` entry or launcher script
- `python main.py` and `python main.py --debug-console` remain available for development and debugging

## Install / Setup

### Windows

```powershell
cd C:\Users\aldiy\Desktop\watashigpt-main\action-middleware
C:\Users\aldiy\AppData\Local\Programs\Python\Python312\Scripts\pip.exe install -r requirements.txt
```

If `python` or `pip` still point to Microsoft Store aliases, disable:

`Settings -> Apps -> Advanced app settings -> App execution aliases -> turn off python.exe and py.exe`

### Linux

```bash
cd action-middleware
pip install -r requirements.txt
sudo apt-get install wl-clipboard xclip libnotify-bin
```

Optional Wayland helper for more reliable paste:

```bash
sudo apt-get install wtype
```

## Distribution

Windows release output:

- `action-middleware/dist/ActionFlow/ActionFlow.exe`

Linux release output:

- `action-middleware/dist/ActionFlow/ActionFlow`
- `action-middleware/dist/linux/actionflow.desktop`
- `action-middleware/dist/linux/actionflow.png`

Build helpers:

- `action-middleware/scripts/build_windows.ps1`
- `action-middleware/scripts/build_windows.bat`
- `action-middleware/scripts/build_linux.sh`
- `action-middleware/scripts/install_linux_desktop.sh`

## Run

### Windows Packaged Desktop App

Build or distribute the packaged app, then launch:

```powershell
action-middleware\dist\ActionFlow\ActionFlow.exe
```

Expected MVP behavior:

- no console window
- tray icon appears
- first-run setup opens if credentials are missing
- `Settings`, `History`, and `Logs` are available from the tray or main window

### Linux Packaged Desktop App

Build and install the launcher:

```bash
cd action-middleware
./scripts/build_linux.sh
./scripts/install_linux_desktop.sh
```

Then launch ActionFlow from the desktop menu / application launcher.

Expected MVP behavior:

- no terminal is required for ordinary use
- tray/background app starts
- first-run setup opens in GUI if credentials are missing
- `Settings`, `History`, and `Logs` are available from the tray or main window

### Source / Development Launch Paths

#### Windows

```powershell
cd C:\Users\aldiy\Desktop\watashigpt-main\action-middleware
C:\Users\aldiy\AppData\Local\Programs\Python\Python312\python.exe main.py
```

For a no-console Windows launcher, use:

```powershell
cd C:\Users\aldiy\Desktop\watashigpt-main\action-middleware
C:\Users\aldiy\AppData\Local\Programs\Python\Python312\pythonw.exe ActionFlow.pyw
```

If the tray does not appear or startup fails, check:

- `~/.actionflow_startup.log` for launcher and tray startup steps
- the fallback main window, which now opens when tray startup is unavailable
- whether `pystray`, `Pillow`, and `tkinter` are available in the current Python install

#### Linux

```bash
cd action-middleware
python main.py
```

### Debug Console Mode

The legacy console-first runtime is still available when you want full terminal diagnostics:

```powershell
C:\Users\aldiy\AppData\Local\Programs\Python\Python312\python.exe main.py --debug-console
```

```bash
python main.py --debug-console
```

Optional console flags only apply in debug console mode:

```powershell
C:\Users\aldiy\AppData\Local\Programs\Python\Python312\python.exe main.py --no-tray
```

```powershell
C:\Users\aldiy\AppData\Local\Programs\Python\Python312\python.exe main.py --debug-console --banner
```

Explicit mock mode:

```powershell
C:\Users\aldiy\AppData\Local\Programs\Python\Python312\python.exe main.py --mock-llm
```

History:

```bash
python main.py --history
python main.py --history --grep TR
```

## Desktop App

Normal launch is now GUI/background-first, and packaged launch is the preferred end-user path.

What you get after startup:

- tray icon / system tray menu
- background hotkey listener
- GUI main window with `Status`, `Settings`, `History`, `Logs`, and `Commands / Help`
- GUI setup dialogs for missing LLM or image credentials
- GUI command picker when selected text has no explicit prefix
- GUI result windows for commands such as `COUNT`, `WIKI`, and `DEFINE`

Tray menu:

- `Open`
- `Quick Command`
- `Settings`
- `History`
- `Logs`
- `Restart Hotkeys`
- `About`
- `Exit`

Closing the main window does not exit the app. Full shutdown is done from tray `Exit`.

Startup tracing:

- GUI and windowless startup write to `~/.actionflow_startup.log`
- `python main.py` keeps console output visible and also writes startup steps there
- `pythonw.exe ActionFlow.pyw` has no console, so the startup log is the first place to inspect
- normal runtime activity such as hotkey registration, hotkey callbacks, selection capture, and command execution is written to `~/.actionflow.log`
- `Status` now reports `Ready` only when hotkeys are registered and background runtime polling is active
- `Partial` means the shell is up but hotkeys or runtime wiring need attention

## Build / Release Workflow

### Build Windows `.exe`

```powershell
cd action-middleware
scripts\build_windows.bat
```

or:

```powershell
cd action-middleware
powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
```

The Windows build flow:

- generates branded `.png` and `.ico` assets
- installs `PyInstaller` if needed
- builds the packaged desktop app
- writes output to `action-middleware/dist/ActionFlow/`

### Build Linux Desktop Launcher

```bash
cd action-middleware
./scripts/build_linux.sh
./scripts/install_linux_desktop.sh
```

The Linux build flow:

- generates branded icons
- builds the packaged desktop binary
- creates `dist/linux/actionflow.desktop`
- installs the desktop launcher into the user's applications directory

### Debug / Development

Use these only for development or investigation:

- `python main.py --debug-console`
- `python main.py`

## Logs, Config, History

### Windows

- config: `%APPDATA%\ActionFlow\config.yaml`
- secrets: `%APPDATA%\ActionFlow\actionflow_secrets.yaml`
- runtime log: `%LOCALAPPDATA%\ActionFlow\logs\actionflow.log`
- startup log: `%LOCALAPPDATA%\ActionFlow\logs\actionflow_startup.log`
- history: `%LOCALAPPDATA%\ActionFlow\actionflow_history.jsonl`

### Linux

- config: `~/.config/actionflow/config.yaml`
- secrets: `~/.config/actionflow/actionflow_secrets.yaml`
- runtime log: `~/.local/state/actionflow/logs/actionflow.log`
- startup log: `~/.local/state/actionflow/logs/actionflow_startup.log`
- history: `~/.local/state/actionflow/actionflow_history.jsonl`

## Hotkeys

Default hotkeys are in `action-middleware/config.yaml.example`:

- `Ctrl+Alt+X` intercept selected text
- `Ctrl+Alt+Z` undo last replacement
- `Ctrl+Alt+S` toggle silent mode

Copy `config.yaml.example` to `config.yaml` to customize providers, personal commands, hotkeys, or per-command settings.

## UI Modes

ActionFlow now has a centralized UX/notification layer with three modes:

- `silent`
  - default for MVP
  - console logs stay visible
  - no success toasts
  - no transient popup/toast notifications for normal command success
  - diagnostics go to the log file instead of user-facing noise
- `minimal`
  - console logs stay visible
  - only important user-facing notices
  - useful for setup problems, critical failures, and optional image-save alerts
- `debug`
  - full terminal diagnostics, activity feed, result popups, and noisy development feedback

Default UI config:

```yaml
ui:
  mode: silent
  show_success_notifications: false
  show_error_popups: false
  show_result_popups: false
  log_level: info
  notify_on_image_save: true
  log_path: ~/.actionflow.log
```

Notes:

- `Ctrl+Alt+S` toggles runtime UX between `silent` and `minimal`
- `debug` mode is best enabled in config when you want full diagnostics
- logs are written to `~/.actionflow.log` by default
- startup and runtime console logs remain visible in all modes
- picker UI still opens when no explicit command prefix is present
- command result UI such as `COUNT`, `WIKI`, `DEFINE`, and other result viewers remains enabled
- setup dialogs and picker UI remain enabled
- successful commands like `RW`, `TRANS`, `SUM`, `EMAIL`, `CMD`, and normal inline replacements now run quietly in MVP mode
- ordinary popup notifications are off by default; command result windows are not treated as notifications

## Settings And Tokens

Use tray `Settings` or the `Settings` tab in the main window to edit:

- LLM provider
- LLM model
- LLM API key
- image provider / model / image API key
- hotkeys
- UI mode
- log path
- launch at startup
- debug console preference

Token handling:

- API keys are entered through GUI fields with password masking
- keys are stored in `~/.actionflow_secrets.yaml`
- non-secret settings stay in `action-middleware/config.yaml`
- logs never print raw secrets

## First-Run Setup

On first launch, if an LLM provider or API key is missing, the app opens a setup dialog instead of failing silently.

The setup flow lets you:

- choose provider
- paste API key
- choose model
- optionally add image API key
- skip setup for now
- use mock mode temporarily

If setup is skipped, the app keeps running in background mode and affected commands honestly report that setup is still required.

## Logs And History

- `History` tab shows recent commands with input/output summaries and status
- `Logs` tab shows the recent log file contents
- default log file: `~/.actionflow.log`
- history file: `~/.actionflow_history.jsonl`

## Autostart

The `Launch at startup` setting writes a platform-specific autostart entry:

- Windows: Startup folder batch launcher
- Linux: `~/.config/autostart/actionflow.desktop`

## LLM Setup

LLM-backed commands no longer default to fake placeholders. The runtime now has three honest states:

- `LLM ready`: provider + model + API key are available, so commands call the real API
- `LLM setup required`: no working key/backend is configured yet, so the first LLM command opens setup
- `LLM mock mode (explicit)`: only enabled via config, env, or `--mock-llm`

You can configure LLM access in three ways:

1. `ACTIONFLOW_API_KEY` environment variable
2. `config.yaml` for provider/model and `~/.actionflow_secrets.yaml` for the key
3. first-run setup prompt triggered by the first LLM command

Example config:

```yaml
llm:
  mode: auto
  provider: groq
  model: llama-3.3-70b-versatile
```

Explicit mock mode options:

- CLI: `python main.py --mock-llm`
- env: `ACTIONFLOW_LLM_MODE=mock`
- config: `llm.mode: mock`

To verify that real LLM calls are active, start the app and look for `LLM ready` in the header/status box.

Image backend example:

```yaml
image_generation:
  provider: pollinations
  base_url: https://gen.pollinations.ai/image
  model: flux
```

Optional image key sources:

- `ACTIONFLOW_IMAGE_API_KEY`
- `POLLINATIONSAI_API_KEY`
- `~/.actionflow_secrets.yaml` after the interactive image setup prompt

## Selection Capture

On Windows, ActionFlow captures the current selection by:

1. snapshotting the current clipboard text and clipboard sequence number
2. sending `Ctrl+C` to the focused app
3. waiting for a clipboard sequence update instead of only checking whether the text changed
4. reading the copied text once the clipboard reports a real update
5. restoring the user clipboard after capture and after replacement

This avoids the old failure mode where capture was treated as failed just because the copied text matched the previous clipboard text.

## Tests

```bash
cd action-middleware
C:\Users\aldiy\AppData\Local\Programs\Python\Python312\Scripts\pip.exe install -r requirements-dev.txt
C:\Users\aldiy\AppData\Local\Programs\Python\Python312\python.exe -m pytest
```

Linux can run the same test suite with `python -m pytest`.

Regression suite for transform commands:

```bash
cd action-middleware
C:\Users\aldiy\AppData\Local\Programs\Python\Python312\python.exe run_regression_suite.py
```

Covered areas:

- registry
- pipeline parsing/execution
- history learner
- stack/clip store
- LLM state resolution and setup decisions
- clipboard capture / paste-restore flow
- transform command parsing
- transform output cleaning / validation / retry
- explicit prefix bypass for picker
- safe math evaluator
- text helper commands
- platform service stubs

## Safety Notes

- `CALC:` uses an AST-based safe evaluator, not raw `eval`.
- `CMD:` / `RUN:` / `EXEC:` remain explicitly allowlisted and platform execution stays behind the platform/system layer.
- Clipboard capture restores and reuses clipboard state carefully, especially on Windows where selection capture uses `Ctrl+C`.
- API keys are read from env/config/secrets, never echoed back in the TUI, and are redacted from config logging helpers.
- Linux-only tools are isolated in `platform/linux.py`; the shared core no longer depends on `xclip`, `wl-copy`, `xdotool`, `swaymsg`, or `wtype`.

## Known Limitations

- Windows text capture still depends on target apps honoring `Ctrl+C`; some privileged or custom-rendered apps may block it.
- Hotkey changes in config are loaded from file, but active listeners still require app restart to rebind.
- If the OpenAI-compatible backend package is missing or the provider rejects the key, setup will fail honestly instead of silently falling back to placeholders.
- Linux service installation is still `systemd`-only; Windows has no autostart installer yet.
- `keyboard` may need elevation on some locked-down Windows or Linux environments.
- Some commands such as `review`, `meeting`, or `eli5` intentionally allow multi-line output, but they still use the same no-chat transform contract.

## Entry Points

- Compatibility launcher: `action-middleware/main.py`
- Package entry: `actionflow.app.main`
