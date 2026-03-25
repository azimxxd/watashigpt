from __future__ import annotations

from pathlib import Path

from actionflow.ui.history_view import HistoryView
from actionflow.ui.logs_view import LogsView
from actionflow.ui.qt_compat import QT_AVAILABLE
from actionflow.ui.settings_window import SettingsWindow


def build_commands_help(commands: dict[str, dict]) -> str:
    lines: list[str] = []
    for name, config in sorted(commands.items()):
        prefixes = ", ".join(config.get("prefixes", []))
        description = str(config.get("description", "")).strip()
        lines.append(f"{name}: {description}")
        if prefixes:
            lines.append(f"  prefixes: {prefixes}")
    return "\n".join(lines)


if QT_AVAILABLE:  # pragma: no cover
    from PySide6.QtWidgets import QLabel, QMainWindow, QPlainTextEdit, QTabWidget, QVBoxLayout, QWidget

    class MainWindow(QMainWindow):
        def __init__(
            self,
            runtime_status: dict,
            commands: dict[str, dict],
            history_path: Path,
            log_path: Path,
            settings_window: SettingsWindow,
        ):
            super().__init__()
            self.setWindowTitle("ActionFlow")
            self.resize(980, 680)
            self._tabs = QTabWidget()
            self.setCentralWidget(self._tabs)

            status = QWidget()
            status_layout = QVBoxLayout(status)
            self._status_label = QLabel(self._build_status_text(runtime_status))
            self._status_label.setWordWrap(True)
            status_layout.addWidget(self._status_label)
            self._tabs.addTab(status, "Status")

            self.settings_window = settings_window
            self._tabs.addTab(settings_window, "Settings")

            self.history_view = HistoryView(history_path)
            self._tabs.addTab(self.history_view, "History")

            self.logs_view = LogsView(log_path)
            self._tabs.addTab(self.logs_view, "Logs")

            help_view = QPlainTextEdit()
            help_view.setReadOnly(True)
            help_view.setPlainText(build_commands_help(commands))
            self._tabs.addTab(help_view, "Commands / Help")

        def _build_status_text(self, runtime_status: dict) -> str:
            return (
                f"Runtime: {runtime_status.get('runtime_health', 'starting')}\n"
                f"Reason: {runtime_status.get('runtime_health_reason', '')}\n"
                f"LLM state: {runtime_status.get('llm_state', 'unknown')}\n"
                f"Provider: {runtime_status.get('llm_provider', '')}\n"
                f"Model: {runtime_status.get('llm_model', '')}\n"
                f"Commands loaded: {runtime_status.get('command_count', 0)}\n"
                f"Hotkey: {runtime_status.get('hotkeys', {}).get('intercept', '')}\n"
                f"Hotkeys registered: {runtime_status.get('hotkeys_registered', False)}\n"
                f"Hotkey backend: {runtime_status.get('hotkey_backend', '')}\n"
                f"Hotkey callbacks: {runtime_status.get('hotkey_callback_count', 0)}\n"
                f"Last hotkey: {runtime_status.get('last_hotkey_callback_at', '-') or '-'}\n"
                f"Last hotkey error: {runtime_status.get('last_hotkey_error', '-') or '-'}"
            )

        def show_tab(self, name: str) -> None:
            mapping = {
                "status": 0,
                "settings": 1,
                "history": 2,
                "logs": 3,
                "commands": 4,
            }
            self._tabs.setCurrentIndex(mapping.get(name, 0))
            self.show()
            self.raise_()
            self.activateWindow()

        def refresh_runtime(self, runtime_status: dict) -> None:
            self._status_label.setText(self._build_status_text(runtime_status))
            self.history_view.refresh()
            self.logs_view.refresh()

else:
    class MainWindow:
        def __init__(
            self,
            runtime_status: dict,
            commands: dict[str, dict],
            history_path: Path,
            log_path: Path,
            settings_window: SettingsWindow,
        ):
            self.runtime_status = runtime_status
            self.commands = commands
            self.history_view = HistoryView(history_path)
            self.logs_view = LogsView(log_path)
            self.settings_window = settings_window
            self.help_text = build_commands_help(commands)
            self.visible_tab = "status"
            self.shown = False

        def show(self) -> None:
            self.shown = True

        def raise_(self) -> None:
            return None

        def activateWindow(self) -> None:
            return None

        def show_tab(self, name: str) -> None:
            self.visible_tab = name
            self.shown = True

        def refresh_runtime(self, runtime_status: dict) -> None:
            self.runtime_status = runtime_status
            self.history_view.refresh()
            self.logs_view.refresh()
