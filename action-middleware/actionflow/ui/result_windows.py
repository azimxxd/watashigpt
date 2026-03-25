from __future__ import annotations

from actionflow.ui.qt_compat import QT_AVAILABLE


if QT_AVAILABLE:  # pragma: no cover
    from PySide6.QtWidgets import QMainWindow, QPlainTextEdit

    class ResultWindow(QMainWindow):
        def __init__(self, title: str, text: str):
            super().__init__()
            self.setWindowTitle(title)
            editor = QPlainTextEdit()
            editor.setReadOnly(True)
            editor.setPlainText(text)
            self.setCentralWidget(editor)
            self.resize(760, 520)

    class ResultWindowManager:
        def __init__(self):
            self._windows: list[ResultWindow] = []

        def show_result(self, title: str, text: str) -> None:
            window = ResultWindow(title, text)
            window.show()
            window.raise_()
            window.activateWindow()
            self._windows.append(window)

else:
    class ResultWindowManager:
        def __init__(self):
            self.shown_results: list[tuple[str, str]] = []

        def show_result(self, title: str, text: str) -> None:
            self.shown_results.append((title, text))
