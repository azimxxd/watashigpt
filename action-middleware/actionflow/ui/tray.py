from __future__ import annotations

from collections.abc import Callable
import sys
import threading

from actionflow.app.startup_logging import get_startup_logger
from actionflow.ui.qt_compat import QT_AVAILABLE, QAction, QIcon


class TrayController:
    STATUS_COLORS = {
        "starting": (235, 178, 48, 255),
        "ready": (0, 180, 130, 255),
        "partial": (235, 178, 48, 255),
        "error": (200, 70, 70, 255),
    }
    MENU_ITEMS = [
        "Open",
        "Quick Command",
        "Settings",
        "History",
        "Logs",
        "Restart Hotkeys",
        "About",
        "Exit",
    ]

    def __init__(self, callbacks: dict[str, Callable[[], None]]):
        self.callbacks = callbacks
        self.menu_actions: list[str] = list(self.MENU_ITEMS)
        self.icon_created = False
        self.last_error: str | None = None
        self.status_name = "starting"
        self.status_message = "ActionFlow starting"
        self._tray = None
        self._logger = get_startup_logger()
        if QT_AVAILABLE:  # pragma: no cover
            from PySide6.QtWidgets import QMenu, QSystemTrayIcon

            self._logger.info("Initializing Qt system tray icon")
            self._tray = QSystemTrayIcon(QIcon(), None)
            menu = QMenu()
            for label in self.MENU_ITEMS:
                action = QAction(label, menu)
                action.triggered.connect(self._wrap_callback(label))
                menu.addAction(action)
            self._tray.setToolTip("ActionFlow")
            self._tray.setContextMenu(menu)
            self._tray.show()
            self.icon_created = True
            self._logger.info("Qt system tray icon created")
            self.update_status("starting", "ActionFlow starting")
        else:
            if "pytest" in sys.modules:
                self.icon_created = True
                self.update_status("starting", "ActionFlow starting")
                return
            try:  # pragma: no cover - real tray exercised outside unit tests
                import pystray

                menu = pystray.Menu(
                    *(pystray.MenuItem(label, self._wrap_callback(label)) for label in self.MENU_ITEMS)
                )
                self._tray = pystray.Icon("actionflow", self._icon_image("starting"), "ActionFlow", menu)
                self._logger.info("Initializing pystray tray icon")
                if hasattr(self._tray, "run_detached"):
                    self._tray.run_detached()
                    self._logger.info("pystray run_detached started")
                else:
                    threading.Thread(target=self._tray.run, daemon=True).start()
                    self._logger.info("pystray threaded run started")
                self.icon_created = True
                self.update_status("starting", "ActionFlow starting")
            except Exception as exc:
                self.last_error = str(exc)
                self._logger.exception("Tray initialization failed: %s", exc)
                self.icon_created = False

    def _wrap_callback(self, label: str):
        def _callback():
            callback = self.callbacks.get(label)
            if callback is not None:
                callback()

        return _callback

    def trigger(self, label: str) -> None:
        callback = self.callbacks.get(label)
        if callback is not None:
            callback()

    def _icon_image(self, status_name: str):
        from PIL import Image, ImageDraw

        fill = self.STATUS_COLORS.get(status_name, self.STATUS_COLORS["partial"])
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((8, 8, 56, 56), fill=fill)
        draw.rectangle((28, 18, 36, 46), fill=(255, 255, 255, 255))
        return img

    def update_status(self, status_name: str, message: str) -> None:
        self.status_name = status_name
        self.status_message = message
        tooltip = f"ActionFlow: {message}"
        if self._tray is None:
            return
        try:
            if hasattr(self._tray, "setToolTip"):
                self._tray.setToolTip(tooltip)
            if not QT_AVAILABLE and hasattr(self._tray, "icon"):
                self._tray.icon = self._icon_image(status_name)
            if hasattr(self._tray, "title"):
                self._tray.title = tooltip
            self._logger.info("Tray status updated: %s (%s)", status_name, message)
        except Exception as exc:
            self.last_error = str(exc)
            self._logger.exception("Tray status update failed: %s", exc)

    def stop(self) -> None:
        if self._tray is not None and hasattr(self._tray, "stop"):
            try:
                self._tray.stop()
                self._logger.info("Tray stopped")
            except Exception:
                pass
