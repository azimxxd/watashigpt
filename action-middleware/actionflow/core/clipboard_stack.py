from __future__ import annotations

import json
from pathlib import Path
import re


_CLIP_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


class ClipboardStack:
    def __init__(self, max_items: int = 20):
        self._items: list[str] = []
        self._max_items = max_items

    def push(self, text: str) -> int:
        if len(self._items) >= self._max_items:
            raise OverflowError("Clipboard stack is full")
        self._items.append(text)
        return len(self._items)

    def pop(self) -> tuple[str, int]:
        if not self._items:
            raise IndexError("Clipboard stack is empty")
        item = self._items.pop()
        return item, len(self._items)

    def peek(self) -> str:
        if not self._items:
            raise IndexError("Clipboard stack is empty")
        return self._items[-1]

    def clear(self) -> None:
        self._items.clear()

    def __len__(self) -> int:
        return len(self._items)


class NamedClipsStore:
    def __init__(self, path: Path, max_clips: int = 100):
        self._path = path
        self._max_clips = max_clips

    @staticmethod
    def is_valid_name(name: str) -> bool:
        return bool(_CLIP_NAME_RE.match(name or ""))

    def load(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def save(self, clips: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as handle:
            json.dump(clips, handle, ensure_ascii=False, indent=2)
        try:
            self._path.chmod(0o600)
        except Exception:
            pass

    def save_clip(self, name: str, text: str) -> int:
        if not self.is_valid_name(name):
            raise ValueError("Clip name must be 1-64 characters using letters, numbers, dash, or underscore")
        clips = self.load()
        if len(clips) >= self._max_clips and name not in clips:
            raise OverflowError(f"Maximum {self._max_clips} saved clips reached")
        clips[name] = text
        self.save(clips)
        return len(clips)

    def get_clip(self, name: str) -> str:
        if not self.is_valid_name(name):
            raise ValueError("Invalid clip name")
        clips = self.load()
        if name not in clips:
            raise KeyError(name)
        return str(clips[name])

    def delete_clip(self, name: str) -> bool:
        if not self.is_valid_name(name):
            raise ValueError("Invalid clip name")
        clips = self.load()
        if name not in clips:
            return False
        del clips[name]
        self.save(clips)
        return True

    def clear(self) -> int:
        count = len(self.load())
        self.save({})
        return count

    def list_clips(self) -> list[tuple[str, str]]:
        clips = self.load()
        return sorted(clips.items(), key=lambda item: item[0].lower())
