from __future__ import annotations

from dataclasses import dataclass
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys
import threading
import time
from typing import Callable


_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


@dataclass(frozen=True)
class NotificationSettings:
    ui_mode: str = "silent"
    show_success_notifications: bool = False
    show_error_popups: bool = False
    show_result_popups: bool = False
    log_level: str = "info"
    notify_on_image_save: bool = True
    log_path: Path = Path.home() / ".actionflow.log"
    error_dedupe_window_seconds: float = 8.0


class NotificationManager:
    def __init__(self) -> None:
        self._settings = NotificationSettings()
        self._runtime_mode = self._settings.ui_mode
        self._sender: Callable[[str, str], None] | None = None
        self._logger: logging.Logger | None = None
        self._logger_path: Path | None = None
        self._logger_level: str | None = None
        self._recent_messages: dict[str, float] = {}
        self._lock = threading.Lock()
        self._ensure_logger()

    @property
    def settings(self) -> NotificationSettings:
        return self._settings

    @property
    def mode(self) -> str:
        return self._runtime_mode

    def configure(self, settings: NotificationSettings) -> None:
        self._settings = settings
        self._runtime_mode = settings.ui_mode
        self._ensure_logger()
        self.log_info(
            "UI configured: mode=%s success_notifications=%s result_popups=%s log=%s",
            settings.ui_mode,
            settings.show_success_notifications,
            settings.show_result_popups,
            settings.log_path,
        )

    def configure_from_config(self, config: dict) -> None:
        ui_cfg = config.get("ui", {}) if isinstance(config.get("ui", {}), dict) else {}
        legacy_silent = bool(config.get("silent_mode", False))
        mode = str(ui_cfg.get("mode", "silent")).strip().lower() or "silent"
        if legacy_silent and "mode" not in ui_cfg:
            mode = "silent"
        if mode not in {"silent", "minimal", "debug"}:
            mode = "silent"

        log_path_value = ui_cfg.get("log_path", str(Path.home() / ".actionflow.log"))
        log_path = Path(log_path_value).expanduser()

        settings = NotificationSettings(
            ui_mode=mode,
            show_success_notifications=bool(ui_cfg.get("show_success_notifications", False)),
            show_error_popups=bool(ui_cfg.get("show_error_popups", False)),
            show_result_popups=bool(ui_cfg.get("show_result_popups", False)),
            log_level=str(ui_cfg.get("log_level", "info")).strip().lower() or "info",
            notify_on_image_save=bool(ui_cfg.get("notify_on_image_save", True)),
            log_path=log_path,
            error_dedupe_window_seconds=float(ui_cfg.get("error_dedupe_window_seconds", 8.0)),
        )
        self.configure(settings)

    def set_sender(self, sender: Callable[[str, str], None] | None) -> None:
        self._sender = sender

    def set_runtime_mode(self, mode: str) -> None:
        normalized = (mode or "").strip().lower()
        if normalized not in {"silent", "minimal", "debug"}:
            normalized = self._settings.ui_mode
        self._runtime_mode = normalized
        self.log_info("UI runtime mode changed to %s", normalized)

    def log_debug(self, message: str, *args) -> None:
        self._ensure_logger()
        if self._logger:
            self._logger.debug(message, *args)

    def log_info(self, message: str, *args) -> None:
        self._ensure_logger()
        if self._logger:
            self._logger.info(message, *args)

    def log_warning(self, message: str, *args) -> None:
        self._ensure_logger()
        if self._logger:
            self._logger.warning(message, *args)

    def log_error(self, message: str, *args, exc_info=None) -> None:
        self._ensure_logger()
        if self._logger:
            self._logger.error(message, *args, exc_info=exc_info)

    def sanitize_for_log(self, text: str) -> str:
        return _ANSI_RE.sub("", text or "")

    def should_print_terminal(self, kind: str) -> bool:
        stream = getattr(sys, "stdout", None)
        return bool(stream is not None and getattr(stream, "write", None))

    def should_show_result_popup(self, *, critical: bool = False, special_ui: bool = False) -> bool:
        return True

    def should_send_notification(
        self,
        *,
        level: str,
        critical: bool = False,
        success: bool = False,
        image_saved: bool = False,
    ) -> bool:
        if image_saved:
            return self._settings.notify_on_image_save
        if critical:
            return True
        if self.mode == "debug":
            return True
        if success:
            return self._settings.show_success_notifications and self.mode != "silent"
        if level == "error":
            return self.mode in {"minimal", "debug"}
        if level == "warning":
            return self.mode == "minimal"
        return False

    def notify_info(
        self,
        title: str,
        message: str,
        *,
        critical: bool = False,
        success: bool = False,
        image_saved: bool = False,
        dedupe_key: str | None = None,
    ) -> bool:
        return self._notify(
            "info",
            title,
            message,
            critical=critical,
            success=success,
            image_saved=image_saved,
            dedupe_key=dedupe_key,
        )

    def notify_warning(
        self,
        title: str,
        message: str,
        *,
        critical: bool = False,
        dedupe_key: str | None = None,
    ) -> bool:
        return self._notify("warning", title, message, critical=critical, dedupe_key=dedupe_key)

    def notify_error(
        self,
        title: str,
        message: str,
        *,
        critical: bool = False,
        dedupe_key: str | None = None,
    ) -> bool:
        return self._notify("error", title, message, critical=critical, dedupe_key=dedupe_key)

    def debug_status(self, message: str) -> None:
        self.log_debug("%s", self.sanitize_for_log(message))

    def _notify(
        self,
        level: str,
        title: str,
        message: str,
        *,
        critical: bool = False,
        success: bool = False,
        image_saved: bool = False,
        dedupe_key: str | None = None,
    ) -> bool:
        sanitized_title = self.sanitize_for_log(title)
        sanitized_message = self.sanitize_for_log(message)
        if dedupe_key is None and level in {"warning", "error"}:
            dedupe_key = f"{level}:{sanitized_title}:{sanitized_message}"
        log_method = {
            "info": self.log_info,
            "warning": self.log_warning,
            "error": self.log_error,
        }.get(level, self.log_info)
        log_method("%s: %s", sanitized_title, sanitized_message)

        if not self.should_send_notification(
            level=level,
            critical=critical,
            success=success,
            image_saved=image_saved,
        ):
            return False

        with self._lock:
            if dedupe_key and self.mode != "debug":
                now = time.time()
                last_seen = self._recent_messages.get(dedupe_key, 0.0)
                if now - last_seen < self._settings.error_dedupe_window_seconds:
                    self.log_debug("Suppressed duplicate notification: %s", dedupe_key)
                    return False
                self._recent_messages[dedupe_key] = now

        if self._sender is None:
            return False
        self._sender(title, message)
        return True

    def _ensure_logger(self) -> None:
        if (
            self._logger is not None
            and self._logger_path == self._settings.log_path
            and self._logger_level == self._settings.log_level
        ):
            return

        logger = logging.getLogger("actionflow")
        logger.setLevel(getattr(logging, self._settings.log_level.upper(), logging.INFO))
        logger.propagate = False
        logger.handlers.clear()

        self._settings.log_path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            self._settings.log_path,
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        handler.setFormatter(formatter)
        logger.addHandler(handler)

        self._logger = logger
        self._logger_path = self._settings.log_path
        self._logger_level = self._settings.log_level


notifications = NotificationManager()
