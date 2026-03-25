from __future__ import annotations


def register_hotkeys(manager, config: dict, on_intercept, on_undo, on_silent) -> None:
    hotkeys = config.get("hotkeys", {})
    manager.add_hotkey(hotkeys.get("intercept", "ctrl+alt+x"), on_intercept)
    manager.add_hotkey(hotkeys.get("undo", "ctrl+alt+z"), on_undo)
    manager.add_hotkey(hotkeys.get("silent_toggle", "ctrl+alt+s"), on_silent)

