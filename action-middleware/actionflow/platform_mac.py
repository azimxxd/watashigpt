# ActionFlow — macOS backend
#
# Native implementations of everything main.py needs from the OS on macOS:
# clipboard, selection capture, Cmd+C/Cmd+V injection, frontmost-app detection
# and re-activation, notifications, global hotkeys and permission checks.
#
# Uses PyObjC (pyobjc-framework-Quartz / -Cocoa / -ApplicationServices) when
# available and falls back to pbcopy/pbpaste/osascript otherwise. All PyObjC
# imports are lazy so this module can be imported (e.g. by tests) without them.
#
# Permissions (System Settings → Privacy & Security), granted to the app that
# launches Python (Terminal, iTerm2, VS Code, ...):
#   - Accessibility     — posting Cmd+C / Cmd+V and swallowing the hotkey
#   - Input Monitoring  — listening for the global hotkey

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Callable

# ============================================================
# Key codes (ANSI layout — physical keys, independent of the active input
# source, so hotkeys keep working with e.g. a Russian layout enabled)
# ============================================================

KEYCODES: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "=": 24, "9": 25,
    "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33,
    "i": 34, "p": 35, "enter": 36, "return": 36, "l": 37, "j": 38, "'": 39,
    "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44, "n": 45, "m": 46, ".": 47,
    "tab": 48, "space": 49, "`": 50, "backspace": 51, "esc": 53, "escape": 53,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98,
    "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}

# CGEventFlags masks
FLAG_SHIFT = 0x00020000
FLAG_CTRL = 0x00040000
FLAG_ALT = 0x00080000
FLAG_CMD = 0x00100000
_MOD_MASK = FLAG_SHIFT | FLAG_CTRL | FLAG_ALT | FLAG_CMD

_MODIFIER_NAMES = {
    "ctrl": FLAG_CTRL, "control": FLAG_CTRL,
    "alt": FLAG_ALT, "option": FLAG_ALT, "opt": FLAG_ALT,
    "shift": FLAG_SHIFT,
    "cmd": FLAG_CMD, "command": FLAG_CMD, "super": FLAG_CMD, "win": FLAG_CMD,
}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """Parse 'ctrl+alt+x' into (modifier_flags, keycode). Raises ValueError."""
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"Empty hotkey: {spec!r}")
    flags = 0
    for mod in parts[:-1]:
        if mod not in _MODIFIER_NAMES:
            raise ValueError(f"Unknown modifier {mod!r} in hotkey {spec!r}")
        flags |= _MODIFIER_NAMES[mod]
    key = parts[-1]
    if key not in KEYCODES:
        raise ValueError(f"Unsupported key {key!r} in hotkey {spec!r}")
    return flags, KEYCODES[key]


def format_hotkey(spec: str) -> str:
    """Render 'ctrl+alt+x' with macOS modifier glyphs: ⌃⌥X."""
    glyphs = {"ctrl": "⌃", "control": "⌃", "alt": "⌥", "option": "⌥", "opt": "⌥",
              "shift": "⇧", "cmd": "⌘", "command": "⌘", "super": "⌘", "win": "⌘"}
    parts = [p.strip().lower() for p in spec.split("+") if p.strip()]
    return "".join(glyphs.get(p, "") for p in parts[:-1]) + (parts[-1].upper() if parts else "")


# ============================================================
# Availability / permissions
# ============================================================

def has_pyobjc() -> bool:
    try:
        from importlib import import_module
        import_module("Quartz")
        import_module("AppKit")
        return True
    except ImportError:
        return False


def is_accessibility_trusted() -> bool | None:
    """True/False if we can check, None if PyObjC is missing."""
    try:
        import ApplicationServices
        return bool(ApplicationServices.AXIsProcessTrusted())
    except ImportError:
        return None


def has_input_monitoring() -> bool | None:
    try:
        import Quartz
        return bool(Quartz.CGPreflightListenEventAccess())
    except (ImportError, AttributeError):
        return None


def request_permissions() -> None:
    """Trigger the system prompts for Accessibility and Input Monitoring."""
    try:
        import ApplicationServices
        opts = {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
        ApplicationServices.AXIsProcessTrustedWithOptions(opts)
    except Exception:
        pass
    try:
        import Quartz
        Quartz.CGRequestListenEventAccess()
    except Exception:
        pass


def open_privacy_settings(pane: str) -> None:
    """Open System Settings at a Privacy pane ('Accessibility' or 'ListenEvent')."""
    url = f"x-apple.systempreferences:com.apple.preference.security?Privacy_{pane}"
    subprocess.run(["open", url], capture_output=True, timeout=5)


# ============================================================
# Clipboard
# ============================================================

_UTF8_ENV = {**os.environ, "LC_CTYPE": "UTF-8"}


def _pasteboard():
    from AppKit import NSPasteboard
    return NSPasteboard.generalPasteboard()


def clipboard_change_count() -> int:
    try:
        return int(_pasteboard().changeCount())
    except ImportError:
        return -1


def clipboard_get() -> str:
    try:
        from AppKit import NSPasteboardTypeString
        value = _pasteboard().stringForType_(NSPasteboardTypeString)
        return str(value) if value is not None else ""
    except ImportError:
        proc = subprocess.run(["pbpaste"], capture_output=True, env=_UTF8_ENV, timeout=2)
        return proc.stdout.decode("utf-8", errors="replace") if proc.returncode == 0 else ""


def clipboard_set(text: str) -> bool:
    try:
        from AppKit import NSPasteboardTypeString
        pb = _pasteboard()
        pb.clearContents()
        return bool(pb.setString_forType_(text, NSPasteboardTypeString))
    except ImportError:
        proc = subprocess.run(["pbcopy"], input=text.encode("utf-8"),
                              capture_output=True, env=_UTF8_ENV, timeout=2)
        return proc.returncode == 0


def clipboard_set_image(path: str) -> bool:
    """Put an image file (PNG/JPEG/WebP…) on the clipboard so Cmd+V pastes it."""
    try:
        from AppKit import NSImage
        image = NSImage.alloc().initWithContentsOfFile_(path)
        if image is None:
            return False
        pb = _pasteboard()
        pb.clearContents()
        return bool(pb.writeObjects_([image]))
    except ImportError:
        kind = "JPEG picture" if path.lower().endswith((".jpg", ".jpeg")) else "«class PNGf»"
        script = [
            "on run argv",
            f"set the clipboard to (read (POSIX file (item 1 of argv)) as {kind})",
            "end run",
        ]
        args = ["osascript"] + [a for line in script for a in ("-e", line)] + [path]
        return subprocess.run(args, capture_output=True, timeout=5).returncode == 0


def clipboard_snapshot() -> list[dict] | str:
    """Capture every item/type on the clipboard (text, images, rich text...)."""
    try:
        pb = _pasteboard()
        items = []
        for item in pb.pasteboardItems() or []:
            entry = {}
            for t in item.types():
                data = item.dataForType_(t)
                if data is not None:
                    entry[str(t)] = data
            if entry:
                items.append(entry)
        return items
    except ImportError:
        return clipboard_get()


def clipboard_restore(snapshot: list[dict] | str) -> None:
    if isinstance(snapshot, str):
        clipboard_set(snapshot)
        return
    try:
        from AppKit import NSPasteboardItem
        pb = _pasteboard()
        pb.clearContents()
        new_items = []
        for entry in snapshot:
            item = NSPasteboardItem.alloc().init()
            for t, data in entry.items():
                item.setData_forType_(data, t)
            new_items.append(item)
        if new_items:
            pb.writeObjects_(new_items)
    except ImportError:
        pass


# ============================================================
# Keyboard injection
# ============================================================

def _modifiers_down() -> int:
    import Quartz
    return int(Quartz.CGEventSourceFlagsState(Quartz.kCGEventSourceStateHIDSystemState)) & _MOD_MASK


def wait_for_modifiers_released(timeout: float = 1.0) -> bool:
    """Block until the user lets go of the hotkey modifiers (so Cmd+C isn't
    seen as Ctrl+Opt+Cmd+C). Returns False on timeout."""
    try:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not _modifiers_down():
                return True
            time.sleep(0.01)
        return False
    except ImportError:
        time.sleep(0.15)
        return True


def send_key_combo(keycode: int, flags: int = FLAG_CMD) -> bool:
    """Post a key press with the given modifiers to the focused app."""
    try:
        import Quartz
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)
        down = Quartz.CGEventCreateKeyboardEvent(src, keycode, True)
        up = Quartz.CGEventCreateKeyboardEvent(src, keycode, False)
        Quartz.CGEventSetFlags(down, flags)
        Quartz.CGEventSetFlags(up, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
        time.sleep(0.02)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
        return True
    except ImportError:
        mods = []
        if flags & FLAG_CMD:
            mods.append("command down")
        if flags & FLAG_SHIFT:
            mods.append("shift down")
        if flags & FLAG_ALT:
            mods.append("option down")
        if flags & FLAG_CTRL:
            mods.append("control down")
        using = f" using {{{', '.join(mods)}}}" if mods else ""
        script = f'tell application "System Events" to key code {keycode}{using}'
        return subprocess.run(["osascript", "-e", script],
                              capture_output=True, timeout=3).returncode == 0


def send_copy() -> bool:
    return send_key_combo(KEYCODES["c"])


def send_paste() -> bool:
    return send_key_combo(KEYCODES["v"])


def capture_selection(timeout: float = 0.6) -> str:
    """Copy the current selection via Cmd+C and return it, leaving the user's
    clipboard exactly as it was. Returns '' when nothing was selected."""
    wait_for_modifiers_released()
    snapshot = clipboard_snapshot()
    before = clipboard_change_count()
    marker = None
    if before < 0:
        # No change counter without PyObjC — fall back to a marker string.
        marker = f"__ACTIONFLOW_MARKER_{time.time_ns()}__"
        clipboard_set(marker)
    send_copy()

    text = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.02)
        if marker is None:
            if clipboard_change_count() != before:
                time.sleep(0.02)  # let the source app finish writing all types
                text = clipboard_get()
                break
        else:
            current = clipboard_get()
            if current != marker:
                text = current
                break

    clipboard_restore(snapshot)
    return text


# ============================================================
# Frontmost application
# ============================================================

def frontmost_app() -> tuple[str, int, str] | None:
    """Return (name, pid, bundle_id) of the frontmost application."""
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        return (str(app.localizedName() or ""), int(app.processIdentifier()),
                str(app.bundleIdentifier() or ""))
    except ImportError:
        script = ('tell application "System Events" to get '
                  '{name, unix id} of first application process whose frontmost is true')
        proc = subprocess.run(["osascript", "-e", script], capture_output=True,
                              text=True, timeout=2)
        if proc.returncode != 0:
            return None
        name, _, pid = proc.stdout.strip().rpartition(", ")
        return (name, int(pid), "") if pid.isdigit() else None


def activate_app(pid: int) -> bool:
    """Bring the app with this PID to the front."""
    try:
        from AppKit import NSRunningApplication, NSApplicationActivateIgnoringOtherApps
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None:
            return False
        return bool(app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps))
    except ImportError:
        script = ('tell application "System Events" to set frontmost of '
                  f'(first application process whose unix id is {int(pid)}) to true')
        return subprocess.run(["osascript", "-e", script],
                              capture_output=True, timeout=3).returncode == 0


def activate_self() -> None:
    """Make this process frontmost so a Tk popup receives keyboard input."""
    activate_app(os.getpid())


def hide_dock_icon() -> None:
    """Run as an accessory app: no Dock icon, but windows can still take focus.
    Call after Tk has created its NSApplication."""
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory)
    except ImportError:
        pass


# ============================================================
# Notifications / misc
# ============================================================

def notify(title: str, message: str) -> bool:
    """Show a Notification Center banner. Text is passed via argv, never
    interpolated into the AppleScript source."""
    script = [
        "on run argv",
        "display notification (item 2 of argv) with title (item 1 of argv)",
        "end run",
    ]
    args = ["osascript"] + [a for line in script for a in ("-e", line)]
    args += [title[:200], message[:500]]
    return subprocess.run(args, capture_output=True, timeout=3).returncode == 0


def open_path(path: str) -> bool:
    return subprocess.run(["open", path], capture_output=True, timeout=5).returncode == 0


# ============================================================
# Global hotkeys — Quartz event tap
# ============================================================

class HotkeyListener:
    """Global hotkeys via a CGEventTap running on its own CFRunLoop thread.

    Matches on physical key codes + exact modifier set, so hotkeys work in any
    keyboard layout. Matching key presses are swallowed (not delivered to the
    focused app) when the tap has Accessibility access; otherwise the tap falls
    back to listen-only mode (Input Monitoring is enough).
    """

    def __init__(self) -> None:
        self._bindings: dict[tuple[int, int], Callable[[], None]] = {}
        self._tap = None
        self._loop = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self.listen_only = False
        self.error = ""

    def add_hotkey(self, spec: str, callback: Callable[[], None]) -> None:
        self._bindings[parse_hotkey(spec)] = callback

    def _callback(self, proxy, event_type, event, refcon):
        import Quartz
        if event_type in (Quartz.kCGEventTapDisabledByTimeout,
                          Quartz.kCGEventTapDisabledByUserInput):
            Quartz.CGEventTapEnable(self._tap, True)
            return event
        if event_type != Quartz.kCGEventKeyDown:
            return event
        keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
        flags = int(Quartz.CGEventGetFlags(event)) & _MOD_MASK
        callback = self._bindings.get((flags, int(keycode)))
        if callback is None:
            return event
        if not Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat):
            # Never block the tap — macOS disables slow taps.
            threading.Thread(target=callback, daemon=True).start()
        return None  # swallow (ignored in listen-only mode)

    def _run(self) -> None:
        import Quartz
        mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown)
        for option in (Quartz.kCGEventTapOptionDefault, Quartz.kCGEventTapOptionListenOnly):
            self._tap = Quartz.CGEventTapCreate(
                Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                option, mask, self._callback, None,
            )
            if self._tap is not None:
                self.listen_only = option == Quartz.kCGEventTapOptionListenOnly
                break
        if self._tap is None:
            self.error = ("could not create event tap — grant Input Monitoring and "
                          "Accessibility to your terminal app, then restart it")
            self._ready.set()
            return

        source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        self._loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(self._loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)
        self._ready.set()
        Quartz.CFRunLoopRun()

    def start(self) -> bool:
        """Start listening. Returns False (and sets .error) on failure."""
        try:
            from importlib import import_module
            import_module("Quartz")
        except ImportError:
            self.error = "PyObjC not installed — run: pip install -r requirements.txt"
            return False
        self._thread = threading.Thread(target=self._run, name="hotkeys", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3)
        return self._tap is not None

    def stop(self) -> None:
        if self._loop is not None:
            import Quartz
            Quartz.CFRunLoopStop(self._loop)


# ============================================================
# Menu bar (NSStatusItem)
# ============================================================

_menu_target_class = None


def _menu_target_cls():
    """NSObject subclass that forwards menu clicks to Python callbacks.
    Defined lazily (and once — ObjC class names are process-global)."""
    global _menu_target_class
    if _menu_target_class is None:
        from Foundation import NSObject

        class ActionFlowMenuTarget(NSObject):
            def onItem_(self, sender):
                callback = self.callbacks.get(int(sender.tag()))
                if callback is not None:
                    try:
                        callback()
                    except Exception as exc:  # never let an exception reach AppKit
                        print(f"Menu action failed: {exc}")

        _menu_target_class = ActionFlowMenuTarget
    return _menu_target_class


class StatusBar:
    """Menu bar icon with a dropdown menu.

    Must be created on the main thread. Clicks are delivered while the main
    thread pumps Cocoa events (Tk's update() does that). Callbacks run on the
    main thread — keep them short or hand work to a queue.
    """

    SYMBOL = "wand.and.stars"

    def __init__(self, tooltip: str = "ActionFlow") -> None:
        from AppKit import NSMenu, NSStatusBar, NSVariableStatusItemLength
        self._item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self._menu = NSMenu.alloc().init()
        self._menu.setAutoenablesItems_(False)
        self._item.setMenu_(self._menu)
        self._target = _menu_target_cls().alloc().init()
        self._target.callbacks = {}
        self._next_tag = 1
        button = self._item.button()
        button.setToolTip_(tooltip)
        self._set_symbol(self.SYMBOL)

    def _set_symbol(self, name: str) -> None:
        from AppKit import NSImage
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, "ActionFlow")
        button = self._item.button()
        if image is not None:
            image.setTemplate_(True)  # adapts to light/dark menu bar
            button.setImage_(image)
        else:
            button.setTitle_("AF")

    def add_item(self, title: str, callback: Callable[[], None] | None = None,
                 key: str = ""):
        """Add a menu item; without a callback it is a disabled label."""
        from AppKit import NSMenuItem
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            title, "onItem:" if callback else None, key)
        if callback is not None:
            tag = self._next_tag
            self._next_tag += 1
            item.setTag_(tag)
            item.setTarget_(self._target)
            self._target.callbacks[tag] = callback
        else:
            item.setEnabled_(False)
        self._menu.addItem_(item)
        return item

    def add_separator(self) -> None:
        from AppKit import NSMenuItem
        self._menu.addItem_(NSMenuItem.separatorItem())

    @staticmethod
    def set_title(item, title: str) -> None:
        item.setTitle_(title)

    @staticmethod
    def set_checked(item, checked: bool) -> None:
        item.setState_(1 if checked else 0)

    def set_dimmed(self, dimmed: bool) -> None:
        """Grey out the icon (used for silent mode)."""
        self._item.button().setAppearsDisabled_(dimmed)

    def remove(self) -> None:
        from AppKit import NSStatusBar
        NSStatusBar.systemStatusBar().removeStatusItem_(self._item)
