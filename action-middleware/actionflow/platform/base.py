from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

from actionflow.core.models import AppContext


@dataclass
class ClipboardSnapshot:
    text: str
    sequence: int | None = None


class ClipboardManager(ABC):
    @abstractmethod
    def copy(self, text: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def paste(self) -> str:
        raise NotImplementedError

    def snapshot(self) -> ClipboardSnapshot:
        return ClipboardSnapshot(text=self.paste(), sequence=self.get_sequence())

    def restore(self, snapshot: ClipboardSnapshot | None) -> None:
        if snapshot is None:
            return
        self.copy(snapshot.text)

    def get_sequence(self) -> int | None:
        return None

    def get_primary_selection(self) -> str:
        return ""

    def copy_image(self, image_path: str) -> bool:
        return False


class HotkeyManager(ABC):
    @abstractmethod
    def add_hotkey(self, hotkey: str, callback) -> None:
        raise NotImplementedError

    def clear_hotkeys(self) -> None:
        return None

    @abstractmethod
    def release_modifiers(self) -> None:
        raise NotImplementedError

    @abstractmethod
    def send_copy(self, alternate: bool = False) -> None:
        raise NotImplementedError

    @abstractmethod
    def send_paste(self) -> None:
        raise NotImplementedError


class WindowManager(ABC):
    @abstractmethod
    def detect_active_window(self) -> AppContext:
        raise NotImplementedError

    def capture_focus_target(self) -> Any:
        return None

    def restore_focus(self, target: Any) -> None:
        return None


class SystemIntegration(ABC):
    @abstractmethod
    def run_process(self, cmd: list[str], **kwargs):
        raise NotImplementedError

    @abstractmethod
    def notify(self, title: str, message: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def open_path(self, path: str) -> None:
        raise NotImplementedError


@dataclass
class PlatformServices:
    clipboard: ClipboardManager
    hotkeys: HotkeyManager
    windows: WindowManager
    system: SystemIntegration
