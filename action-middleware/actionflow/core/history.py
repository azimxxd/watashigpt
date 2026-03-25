from __future__ import annotations

import json
import locale
import logging
from pathlib import Path
from datetime import datetime


def _history_logger() -> logging.Logger:
    return logging.getLogger("actionflow")


def _history_encoding_candidates() -> list[str]:
    candidates = ["utf-8", "utf-8-sig"]
    preferred = locale.getpreferredencoding(False) or ""
    for encoding in ("cp1251", "cp1252", preferred, "latin-1"):
        normalized = encoding.strip().lower()
        if normalized and normalized not in candidates:
            candidates.append(normalized)
    return candidates


def _write_history_entries_utf8(history_path: Path, entries: list[dict]) -> None:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with open(history_path, "w", encoding="utf-8", newline="\n") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _create_history_backup(history_path: Path) -> Path | None:
    if not history_path.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = history_path.with_name(f"{history_path.stem}.bak_{timestamp}{history_path.suffix}")
    try:
        backup_path.write_bytes(history_path.read_bytes())
        return backup_path
    except Exception:
        return None


def load_history_entries(history_path: Path, *, limit: int | None = 50) -> list[dict]:
    if not history_path.exists():
        return []

    logger = _history_logger()
    try:
        raw_bytes = history_path.read_bytes()
    except Exception as exc:
        logger.warning("Failed to read history file %s: %s", history_path, exc)
        return []

    if not raw_bytes:
        return []

    decoded_text = ""
    used_encoding = "utf-8"
    replacement_mode = False
    for encoding in _history_encoding_candidates():
        try:
            decoded_text = raw_bytes.decode(encoding)
            used_encoding = encoding
            break
        except UnicodeDecodeError:
            continue
    else:
        decoded_text = raw_bytes.decode("utf-8", errors="replace")
        used_encoding = "utf-8"
        replacement_mode = True
        logger.warning("History file %s had undecodable bytes; using replacement fallback", history_path)

    entries: list[dict] = []
    skipped_lines = 0
    for line in decoded_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            skipped_lines += 1
            continue
        if isinstance(entry, dict):
            entries.append(entry)

    needs_repair = used_encoding != "utf-8" or replacement_mode
    if needs_repair:
        backup_path = _create_history_backup(history_path)
        if backup_path is not None:
            logger.warning(
                "Recovered history file %s from %s and saved backup to %s",
                history_path,
                used_encoding if not replacement_mode else "utf-8 replacement mode",
                backup_path,
            )
        else:
            logger.warning("Recovered history file %s but could not create a backup", history_path)
        try:
            _write_history_entries_utf8(history_path, entries)
        except Exception as exc:
            logger.warning("Failed to rewrite history file %s as UTF-8: %s", history_path, exc)

    if skipped_lines:
        logger.warning("Skipped %s malformed history line(s) while reading %s", skipped_lines, history_path)

    return entries[-limit:] if limit is not None else entries


class PatternLearner:
    MIN_SAMPLES = 20
    DOMINATE_SAMPLES = 100

    def __init__(self, history_path: Path):
        self._history_path = history_path
        self._samples = 0
        self._context_counts: dict[str, dict[str, int]] = {}
        self._total_counts: dict[str, int] = {}

    @property
    def sample_count(self) -> int:
        return self._samples

    def load(self) -> None:
        self._context_counts.clear()
        self._total_counts.clear()
        self._samples = 0
        for entry in load_history_entries(self._history_path, limit=None):
            command = entry.get("command", "")
            if not command:
                continue
            context = entry.get("app_context", "unknown")
            self._samples += 1
            self._total_counts[command] = self._total_counts.get(command, 0) + 1
            self._context_counts.setdefault(context, {})
            self._context_counts[context][command] = self._context_counts[context].get(command, 0) + 1

    def get_scores(self, app_context: str) -> dict[str, float]:
        if self._samples < self.MIN_SAMPLES:
            return {}
        blend = min(1.0, (self._samples - self.MIN_SAMPLES) / max(1, self.DOMINATE_SAMPLES - self.MIN_SAMPLES))
        counts = self._context_counts.get(app_context, self._total_counts) or self._total_counts
        total = sum(counts.values()) or 1
        return {command: ((count / total) * 10.0) * blend for command, count in counts.items()}
