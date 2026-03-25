from __future__ import annotations

import sys

try:
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QAction, QIcon
    from PySide6.QtWidgets import QApplication

    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False

    class QApplication:  # pragma: no cover - lightweight fallback for tests
        _instance = None

        def __init__(self, argv=None):
            QApplication._instance = self
            self.argv = list(argv or [])
            self._quit = False

        @classmethod
        def instance(cls):
            return cls._instance

        def setQuitOnLastWindowClosed(self, value: bool) -> None:
            return None

        def exec(self) -> int:
            return 0

        def quit(self) -> None:
            self._quit = True

    class QTimer:  # pragma: no cover
        def __init__(self):
            self._callback = None
            self.interval_ms = 0

        @property
        def timeout(self):
            return self

        def connect(self, callback):
            self._callback = callback

        def start(self, interval_ms: int) -> None:
            self.interval_ms = interval_ms

        def trigger(self) -> None:
            if self._callback is not None:
                self._callback()

    class QAction:  # pragma: no cover
        def __init__(self, text: str, parent=None):
            self.text = text
            self._callback = None

        @property
        def triggered(self):
            return self

        def connect(self, callback):
            self._callback = callback

        def trigger(self):
            if self._callback is not None:
                self._callback()

    class QIcon:  # pragma: no cover
        def __init__(self, *args, **kwargs):
            pass


def create_application(argv: list[str] | None = None) -> QApplication:
    app = QApplication.instance()
    if app is not None:
        return app
    created = QApplication(argv or sys.argv)
    if hasattr(created, "setQuitOnLastWindowClosed"):
        created.setQuitOnLastWindowClosed(False)
    return created
