from actionflow.core.clipboard_flow import ClipboardCaptureConfig, capture_selection_via_clipboard, paste_with_clipboard_restore
from actionflow.platform.base import ClipboardSnapshot


class FakeClipboard:
    def __init__(self, text: str = "", sequence: int = 1):
        self.text = text
        self.sequence = sequence
        self.restore_calls = 0

    def copy(self, text: str) -> None:
        self.text = text
        self.sequence += 1

    def paste(self) -> str:
        return self.text

    def snapshot(self) -> ClipboardSnapshot:
        return ClipboardSnapshot(text=self.text, sequence=self.sequence)

    def restore(self, snapshot: ClipboardSnapshot | None) -> None:
        if snapshot is None:
            return
        self.restore_calls += 1
        self.text = snapshot.text
        self.sequence += 1

    def get_sequence(self) -> int | None:
        return self.sequence


class FakeHotkeys:
    def __init__(self, clipboard: FakeClipboard, copy_payloads: list[str]):
        self.clipboard = clipboard
        self.copy_payloads = copy_payloads
        self.copy_calls = 0
        self.paste_calls = 0

    def release_modifiers(self) -> None:
        return None

    def send_copy(self, alternate: bool = False) -> None:
        idx = min(self.copy_calls, len(self.copy_payloads) - 1)
        self.clipboard.copy(self.copy_payloads[idx])
        self.copy_calls += 1

    def send_paste(self) -> None:
        self.paste_calls += 1


class NoOpCopyHotkeys(FakeHotkeys):
    def send_copy(self, alternate: bool = False) -> None:
        self.copy_calls += 1


class DelayedCopyHotkeys(FakeHotkeys):
    def __init__(self, clipboard: FakeClipboard, copy_payloads: list[str], delay_calls: int):
        super().__init__(clipboard, copy_payloads)
        self.delay_calls = delay_calls
        self.pending_payload: str | None = None

    def send_copy(self, alternate: bool = False) -> None:
        idx = min(self.copy_calls, len(self.copy_payloads) - 1)
        payload = self.copy_payloads[idx]
        self.copy_calls += 1
        if self.copy_calls <= self.delay_calls:
            self.pending_payload = payload
            return
        self.clipboard.copy(payload)

    def release_modifiers(self) -> None:
        if self.pending_payload is not None and self.copy_calls >= self.delay_calls:
            self.clipboard.copy(self.pending_payload)
            self.pending_payload = None
        return None


class FakeWindowManager:
    def __init__(self):
        self.restore_calls = 0

    def restore_focus(self, target) -> None:
        self.restore_calls += 1


def test_capture_succeeds_when_clipboard_text_before_and_after_is_identical():
    clipboard = FakeClipboard(text="same text", sequence=10)
    hotkeys = FakeHotkeys(clipboard, ["same text"])
    windows = FakeWindowManager()

    captured, snapshot = capture_selection_via_clipboard(
        clipboard,
        hotkeys,
        windows=windows,
        focus_target=123,
        config=ClipboardCaptureConfig(timeout=0.05, retries=1, initial_delay=0.0, poll_interval=0.0, settle_delay=0.0),
    )

    assert captured == "same text"
    assert snapshot.text == "same text"
    assert clipboard.text == "same text"
    assert windows.restore_calls >= 1


def test_capture_succeeds_when_clipboard_starts_with_same_selected_text():
    clipboard = FakeClipboard(text="IMAGE:apple", sequence=20)
    hotkeys = FakeHotkeys(clipboard, ["IMAGE:apple"])

    captured, snapshot = capture_selection_via_clipboard(
        clipboard,
        hotkeys,
        config=ClipboardCaptureConfig(timeout=0.05, retries=1, initial_delay=0.0, poll_interval=0.0, settle_delay=0.0),
    )

    assert captured == "IMAGE:apple"
    assert snapshot.text == "IMAGE:apple"
    assert clipboard.text == "IMAGE:apple"


def test_capture_retries_and_times_out_cleanly():
    clipboard = FakeClipboard(text="original", sequence=1)
    hotkeys = NoOpCopyHotkeys(clipboard, ["original", "original"])

    captured, snapshot = capture_selection_via_clipboard(
        clipboard,
        hotkeys,
        windows=None,
        focus_target=None,
        config=ClipboardCaptureConfig(timeout=0.01, retries=2, initial_delay=0.0, retry_delay=0.0, poll_interval=0.0, settle_delay=0.0),
    )

    assert captured == ""
    assert snapshot.text == "original"
    assert clipboard.text == "original"
    assert hotkeys.copy_calls == 2


def test_capture_retries_after_delayed_copy():
    clipboard = FakeClipboard(text="original", sequence=1)
    hotkeys = DelayedCopyHotkeys(clipboard, ["HASH:hello"], delay_calls=1)

    captured, snapshot = capture_selection_via_clipboard(
        clipboard,
        hotkeys,
        config=ClipboardCaptureConfig(
            timeout=0.02,
            retries=3,
            initial_delay=0.0,
            retry_delay=0.0,
            poll_interval=0.0,
            settle_delay=0.0,
            arm_settle_delay=0.0,
        ),
    )

    assert captured == "HASH:hello"
    assert snapshot.text == "original"
    assert hotkeys.copy_calls >= 2


def test_paste_restores_user_clipboard_after_successful_replace():
    clipboard = FakeClipboard(text="user clipboard", sequence=3)
    hotkeys = FakeHotkeys(clipboard, ["ignored"])
    windows = FakeWindowManager()

    snapshot = paste_with_clipboard_restore(
        clipboard,
        hotkeys,
        windows,
        focus_target=456,
        text="replacement",
        config=ClipboardCaptureConfig(settle_delay=0.0, paste_settle_delay=0.0),
    )

    assert snapshot.text == "user clipboard"
    assert hotkeys.paste_calls == 1
    assert clipboard.text == "user clipboard"
    assert clipboard.restore_calls == 1
