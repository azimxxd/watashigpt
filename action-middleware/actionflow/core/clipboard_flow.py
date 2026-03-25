from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from actionflow.platform.base import ClipboardManager, ClipboardSnapshot, HotkeyManager, WindowManager


@dataclass(frozen=True)
class ClipboardCaptureConfig:
    timeout: float = 1.2
    retries: int = 3
    initial_delay: float = 0.06
    retry_delay: float = 0.08
    poll_interval: float = 0.03
    settle_delay: float = 0.05
    paste_settle_delay: float = 0.12
    arm_clipboard: bool = True
    arm_settle_delay: float = 0.04


def _clipboard_changed(
    before: ClipboardSnapshot,
    current_text: str,
    current_sequence: int | None,
) -> bool:
    if before.sequence is not None and current_sequence is not None:
        return current_sequence != before.sequence
    return current_text != before.text


def _log_event(logger: Callable[[str], None] | None, message: str) -> None:
    if logger is not None:
        logger(message)


def capture_selection_via_clipboard(
    clipboard: ClipboardManager,
    hotkeys: HotkeyManager,
    windows: WindowManager | None = None,
    focus_target=None,
    config: ClipboardCaptureConfig | None = None,
    logger: Callable[[str], None] | None = None,
) -> tuple[str, ClipboardSnapshot]:
    config = config or ClipboardCaptureConfig()
    snapshot = clipboard.snapshot()
    armed_snapshot = snapshot

    if config.arm_clipboard:
        marker = f"__ACTIONFLOW_CAPTURE_{time.time_ns()}__"
        clipboard.copy(marker)
        time.sleep(config.arm_settle_delay)
        armed_snapshot = clipboard.snapshot()
        _log_event(
            logger,
            f"Capture armed: sequence={armed_snapshot.sequence} marker_len={len(marker)}",
        )

    for attempt in range(config.retries):
        if windows is not None and focus_target is not None:
            windows.restore_focus(focus_target)
        hotkeys.release_modifiers()
        time.sleep(config.initial_delay if attempt == 0 else config.retry_delay)
        alternate_copy = attempt > 0
        hotkeys.send_copy(alternate=alternate_copy)
        _log_event(
            logger,
            f"Copy signal sent: attempt={attempt + 1}/{config.retries} alternate={alternate_copy}",
        )

        deadline = time.time() + config.timeout
        last_sequence = clipboard.get_sequence()
        while time.time() < deadline:
            current_text = clipboard.paste()
            current_sequence = clipboard.get_sequence()
            sequence_changed = (
                armed_snapshot.sequence is not None
                and current_sequence is not None
                and current_sequence != armed_snapshot.sequence
            )
            text_changed = current_text != armed_snapshot.text
            if sequence_changed or text_changed:
                _log_event(
                    logger,
                    f"Clipboard update observed: sequence_changed={sequence_changed} text_changed={text_changed}",
                )
                time.sleep(config.settle_delay)
                confirmed_text = clipboard.paste()
                confirmed_sequence = clipboard.get_sequence()
                confirmed_sequence_changed = (
                    armed_snapshot.sequence is not None
                    and confirmed_sequence is not None
                    and confirmed_sequence != armed_snapshot.sequence
                )
                confirmed_text_changed = confirmed_text != armed_snapshot.text
                if (confirmed_sequence_changed or confirmed_text_changed) and confirmed_text != armed_snapshot.text:
                    _log_event(
                        logger,
                        f"Captured selection: len={len(confirmed_text)} sequence={confirmed_sequence}",
                    )
                    clipboard.restore(snapshot)
                    return confirmed_text, snapshot
            last_sequence = current_sequence
            time.sleep(config.poll_interval)

        _log_event(
            logger,
            f"Capture attempt timed out: attempt={attempt + 1}/{config.retries} last_sequence={last_sequence} armed_sequence={armed_snapshot.sequence}",
        )

    clipboard.restore(snapshot)
    _log_event(logger, "Selection capture timed out: no clipboard update produced selected text")
    return "", snapshot


def paste_with_clipboard_restore(
    clipboard: ClipboardManager,
    hotkeys: HotkeyManager,
    windows: WindowManager | None,
    focus_target,
    text: str,
    config: ClipboardCaptureConfig | None = None,
) -> ClipboardSnapshot:
    config = config or ClipboardCaptureConfig()
    snapshot = clipboard.snapshot()
    clipboard.copy(text)
    if windows is not None and focus_target is not None:
        windows.restore_focus(focus_target)
    time.sleep(config.settle_delay)
    hotkeys.send_paste()
    time.sleep(config.paste_settle_delay)
    clipboard.restore(snapshot)
    return snapshot
