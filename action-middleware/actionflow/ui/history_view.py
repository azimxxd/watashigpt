from __future__ import annotations

from pathlib import Path

from actionflow.core.history import load_history_entries
from actionflow.ui.qt_compat import QT_AVAILABLE


if QT_AVAILABLE:  # pragma: no cover - exercised through integration tests
    from PySide6.QtWidgets import QPlainTextEdit, QVBoxLayout, QWidget

    class HistoryView(QWidget):
        def __init__(self, history_path: Path):
            super().__init__()
            self._history_path = history_path
            self._editor = QPlainTextEdit()
            self._editor.setReadOnly(True)
            layout = QVBoxLayout(self)
            layout.addWidget(self._editor)
            self.refresh()

        def refresh(self) -> None:
            entries = load_history_entries(self._history_path)
            lines: list[str] = []
            for entry in entries:
                lines.append(
                    f"{entry.get('ts', '?')[:19]}  {entry.get('command', '?')}"
                    f"  [{entry.get('status', 'success')}]"
                )
                lines.append(f"IN:  {str(entry.get('input', '')).replace(chr(10), ' ')[:120]}")
                lines.append(f"OUT: {str(entry.get('output', '')).replace(chr(10), ' ')[:120]}")
                lines.append("")
            self._editor.setPlainText("\n".join(lines).strip())

else:
    class HistoryView:
        def __init__(self, history_path: Path):
            self._history_path = history_path
            self.entries: list[dict] = []
            self.refresh()

        def refresh(self) -> None:
            self.entries = load_history_entries(self._history_path)
