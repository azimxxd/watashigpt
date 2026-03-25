from __future__ import annotations

from pathlib import Path

from actionflow.ui.qt_compat import QT_AVAILABLE


def load_log_text(log_path: Path, *, max_chars: int = 120_000) -> str:
    if not log_path.exists():
        return ""
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return text[-max_chars:]


if QT_AVAILABLE:  # pragma: no cover
    from PySide6.QtWidgets import QPlainTextEdit, QVBoxLayout, QWidget

    class LogsView(QWidget):
        def __init__(self, log_path: Path):
            super().__init__()
            self._log_path = log_path
            self._editor = QPlainTextEdit()
            self._editor.setReadOnly(True)
            layout = QVBoxLayout(self)
            layout.addWidget(self._editor)
            self.refresh()

        def refresh(self) -> None:
            self._editor.setPlainText(load_log_text(self._log_path))

else:
    class LogsView:
        def __init__(self, log_path: Path):
            self._log_path = log_path
            self.text = ""
            self.refresh()

        def refresh(self) -> None:
            self.text = load_log_text(self._log_path)
