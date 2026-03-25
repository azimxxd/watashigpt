from actionflow.app.hotkeys import register_hotkeys


class FakeManager:
    def __init__(self):
        self.calls = []

    def add_hotkey(self, hotkey: str, callback) -> None:
        self.calls.append((hotkey, callback))


def test_hotkey_registration_uses_expected_bindings():
    manager = FakeManager()
    callbacks = {
        "intercept": lambda: None,
        "undo": lambda: None,
        "silent": lambda: None,
    }
    register_hotkeys(
        manager,
        {"hotkeys": {"intercept": "ctrl+alt+x", "undo": "ctrl+alt+z", "silent_toggle": "ctrl+alt+s"}},
        callbacks["intercept"],
        callbacks["undo"],
        callbacks["silent"],
    )
    assert [call[0] for call in manager.calls] == ["ctrl+alt+x", "ctrl+alt+z", "ctrl+alt+s"]


def test_hotkey_callback_can_fire():
    manager = FakeManager()
    fired = {"count": 0}

    def on_intercept():
        fired["count"] += 1

    register_hotkeys(
        manager,
        {"hotkeys": {"intercept": "ctrl+alt+x", "undo": "ctrl+alt+z", "silent_toggle": "ctrl+alt+s"}},
        on_intercept,
        lambda: None,
        lambda: None,
    )
    manager.calls[0][1]()
    assert fired["count"] == 1
