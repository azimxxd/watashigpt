# Для тупого алдияра
1. Чтобы запустить: в консоли пишешь cd /home/azamat/projects/watashigpt/action-middleware
2. Потом пишешь sudo -E /home/azamat/projects/watashigpt/.venv/bin/python main.py

# Action Middleware

OS-level background assistant that intercepts selected text via a global hotkey, processes it by prefix, and replaces it in-place.

![Python](https://img.shields.io/badge/Python-3.10+-blue)
![Platform](https://img.shields.io/badge/Platform-Linux-green)
![Wayland](https://img.shields.io/badge/Wayland-Supported-purple)
![X11](https://img.shields.io/badge/X11-Supported-orange)

## How It Works

1. Select any text in any application
2. Press `Ctrl+Alt+X`
3. The text is captured, processed based on its prefix, and replaced in-place
4. Press `Ctrl+Alt+Z` to undo

```
  ╭──────────────────────────────────────╮
  │  ▄▀█ █▀▀ ▀█▀ █ █▀█ █▄░█            │
  │  █▀█ █▄▄ ░█░ █ █▄█ █░▀█            │
  │                                      │
  │  █▀▄▀█ █ █▀▄ █▀▄ █░░ █▀▀ █░█░█     │
  │  █░▀░█ █ █▄▀ █▄▀ █▄▄ ██▄ ▀▄▀▄▀     │
  ╰──────────────────────────────────────╯
```

## Prefixes

| Prefix | Action | Description |
|--------|--------|-------------|
| `TR:` | Corporate Translator | Converts toxic/rude phrases into polite professional alternatives |
| `CMD:` | Terminal Magic | Executes a shell command silently and shows result via notification |
| `TEST:` | Pipeline Test | Verifies the full capture → process → replace pipeline works |

### TR: Examples

| You type | Gets replaced with |
|----------|-------------------|
| `TR:fix this garbage` | "Please review the code for potential improvements." |
| `TR:this is broken` | "I've identified an issue that needs attention." |
| `TR:who wrote this` | "Could we discuss the approach taken here?" |
| `TR:are you serious` | "Could you help me understand the reasoning behind this?" |
| `TR:do it yourself` | "I'd appreciate your help with this task." |
| `TR:this is taking forever` | "Could we discuss the timeline and priorities?" |
| `TR:just ship it` | "Let's discuss what an acceptable MVP looks like." |
| `TR:stop wasting my time` | "I want to make sure we're using everyone's time effectively." |

27 phrases supported across categories: frustration, blame, refusal, deadline pressure, and meetings.

### CMD: Examples

```
CMD:echo hello world       → notification with "hello world"
CMD:date                   → notification with current date
CMD:ls ~/Documents         → notification with directory listing
```

### TEST: Examples

```
TEST:hello world           → replaced with: [TEST OK] "hello world" | session=wayland | wayland=True
```

## Keybindings

| Key | Action |
|-----|--------|
| `Ctrl+Alt+X` | Intercept selected text |
| `Ctrl+Alt+Z` | Undo last replacement |
| `Ctrl+C` | Exit application |

## Requirements

### System packages

```bash
# Wayland (most modern Linux desktops)
sudo apt-get install wl-clipboard libnotify-bin

# X11
sudo apt-get install xclip libnotify-bin
```

### Python dependencies

```bash
pip install -r action-middleware/requirements.txt
```

- `keyboard>=0.13.5` — global hotkey detection (requires root on Linux)
- `pyperclip>=1.8.2` — clipboard (macOS/Windows fallback)
- `plyer>=2.1.0` — notifications (macOS/Windows fallback)

## Usage

The `keyboard` library requires root privileges on Linux for global hotkey access. Use `sudo -E` to preserve your display environment variables:

```bash
sudo -E python action-middleware/main.py
```

The `-E` flag preserves `DISPLAY`, `WAYLAND_DISPLAY`, and `DBUS_SESSION_BUS_ADDRESS` so clipboard and notification tools can connect to your session.

## Architecture

```
Hotkey (Ctrl+Alt+X)
    │
    ├─ Callback fires on keyboard listener thread
    │  └─ Spawns worker thread (non-blocking — prevents listener deadlock)
    │
    ├─ Worker thread:
    │  ├─ 0.5s delay (wait for physical key release)
    │  ├─ Read primary selection (Wayland) or simulate Ctrl+C (X11)
    │  ├─ Route by prefix (TR: / CMD: / TEST:)
    │  ├─ Process text through handler
    │  └─ Replace selection (clipboard + uinput Ctrl+V)
    │
    └─ TUI output (thread-safe, timestamped, color-coded)
```

### Platform handling

| Component | Wayland | X11 |
|-----------|---------|-----|
| Read selection | `wl-paste --primary` | `keyboard` Ctrl+C + `xclip` |
| Write clipboard | `wl-copy` | `xclip` |
| Simulate keys | `keyboard` lib (`/dev/uinput`) | `keyboard` lib (`/dev/uinput`) |
| Notifications | `notify-send` | `notify-send` |

### Key design decisions

- **Non-blocking callbacks**: Hotkey callbacks spawn threads and return immediately. The `keyboard` library uses a single listener thread — blocking it deadlocks all hotkey detection.
- **Primary selection on Wayland**: Instead of simulating Ctrl+C (which requires display server cooperation), Wayland's primary selection provides highlighted text directly.
- **uinput for keystroke simulation**: The `keyboard` library writes to `/dev/uinput` at kernel level, bypassing Wayland's input injection restrictions.
- **`sudo -E` with `_run_as_user`**: The script runs as root (for `/dev/input` access) but clipboard/notification commands run as the original user (who owns the display session).

## Project Structure

```
watashigpt/
├── action-middleware/
│   ├── main.py              # All application code
│   └── requirements.txt     # Python dependencies
├── .gitignore
└── README.md
```

## Roadmap

- **Phase 2**: Replace prefix-based routing with LLM intent classification (send text to AI API, receive `{"intent": "translate", "payload": "..."}`)
- Custom phrase dictionaries (user-editable config file)
- More action prefixes (summarize, rewrite, explain)
- System tray icon with status indicator
