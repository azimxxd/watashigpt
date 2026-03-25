from __future__ import annotations

from dataclasses import dataclass

from actionflow.ui.qt_compat import QT_AVAILABLE


@dataclass
class SettingsState:
    llm_provider: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    image_provider: str = "pollinations"
    image_model: str = "flux"
    image_api_key: str = ""
    intercept_hotkey: str = "ctrl+alt+x"
    undo_hotkey: str = "ctrl+alt+z"
    silent_toggle_hotkey: str = "ctrl+alt+s"
    ui_mode: str = "silent"
    log_path: str = ""
    launch_at_startup: bool = False
    debug_console: bool = False


def build_settings_state(config: dict, *, llm_api_key: str = "", image_api_key: str = "") -> SettingsState:
    llm_cfg = config.get("llm", {}) if isinstance(config.get("llm", {}), dict) else {}
    image_cfg = config.get("image_generation", {}) if isinstance(config.get("image_generation", {}), dict) else {}
    hotkeys = config.get("hotkeys", {}) if isinstance(config.get("hotkeys", {}), dict) else {}
    ui_cfg = config.get("ui", {}) if isinstance(config.get("ui", {}), dict) else {}
    app_cfg = config.get("app", {}) if isinstance(config.get("app", {}), dict) else {}
    return SettingsState(
        llm_provider=str(llm_cfg.get("provider", "")).strip(),
        llm_model=str(llm_cfg.get("model", "")).strip(),
        llm_api_key=llm_api_key,
        image_provider=str(image_cfg.get("provider", "pollinations")).strip(),
        image_model=str(image_cfg.get("model", "flux")).strip(),
        image_api_key=image_api_key,
        intercept_hotkey=str(hotkeys.get("intercept", "ctrl+alt+x")).strip(),
        undo_hotkey=str(hotkeys.get("undo", "ctrl+alt+z")).strip(),
        silent_toggle_hotkey=str(hotkeys.get("silent_toggle", "ctrl+alt+s")).strip(),
        ui_mode=str(ui_cfg.get("mode", "silent")).strip(),
        log_path=str(ui_cfg.get("log_path", "")).strip(),
        launch_at_startup=bool(app_cfg.get("launch_at_startup", False)),
        debug_console=bool(app_cfg.get("debug_console", False)),
    )


def apply_settings_state(config: dict, state: SettingsState) -> dict:
    updated = dict(config)
    updated.setdefault("llm", {})
    updated["llm"] = {
        **updated["llm"],
        "provider": state.llm_provider,
        "model": state.llm_model,
        "mode": "auto",
    }
    updated.setdefault("image_generation", {})
    updated["image_generation"] = {
        **updated["image_generation"],
        "provider": state.image_provider,
        "model": state.image_model,
    }
    updated.setdefault("hotkeys", {})
    updated["hotkeys"] = {
        **updated["hotkeys"],
        "intercept": state.intercept_hotkey,
        "undo": state.undo_hotkey,
        "silent_toggle": state.silent_toggle_hotkey,
    }
    updated.setdefault("ui", {})
    updated["ui"] = {
        **updated["ui"],
        "mode": state.ui_mode,
        "log_path": state.log_path,
    }
    updated.setdefault("app", {})
    updated["app"] = {
        **updated["app"],
        "launch_at_startup": state.launch_at_startup,
        "debug_console": state.debug_console,
    }
    return updated


if QT_AVAILABLE:  # pragma: no cover
    from PySide6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QFormLayout,
        QGroupBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QPushButton,
        QVBoxLayout,
        QWidget,
    )

    class SettingsWindow(QWidget):
        def __init__(self, state: SettingsState, on_save):
            super().__init__()
            self._on_save = on_save
            self._provider = QLineEdit(state.llm_provider)
            self._model = QLineEdit(state.llm_model)
            self._llm_key = QLineEdit(state.llm_api_key)
            self._llm_key.setEchoMode(QLineEdit.Password)
            self._image_provider = QLineEdit(state.image_provider)
            self._image_model = QLineEdit(state.image_model)
            self._image_key = QLineEdit(state.image_api_key)
            self._image_key.setEchoMode(QLineEdit.Password)
            self._intercept = QLineEdit(state.intercept_hotkey)
            self._undo = QLineEdit(state.undo_hotkey)
            self._silent = QLineEdit(state.silent_toggle_hotkey)
            self._ui_mode = QComboBox()
            self._ui_mode.addItems(["silent", "minimal", "debug"])
            self._ui_mode.setCurrentText(state.ui_mode or "silent")
            self._log_path = QLineEdit(state.log_path)
            self._launch = QCheckBox("Launch at startup")
            self._launch.setChecked(state.launch_at_startup)
            self._debug = QCheckBox("Enable debug console mode by default")
            self._debug.setChecked(state.debug_console)

            root = QVBoxLayout(self)
            for title, rows in (
                ("LLM", [("Provider", self._provider), ("Model", self._model), ("API key", self._llm_key)]),
                ("Image", [("Provider", self._image_provider), ("Model", self._image_model), ("API key", self._image_key)]),
                ("Hotkeys", [("Intercept", self._intercept), ("Undo", self._undo), ("Silent toggle", self._silent)]),
                ("Desktop", [("UI mode", self._ui_mode), ("Log path", self._log_path)]),
            ):
                box = QGroupBox(title)
                form = QFormLayout(box)
                for label, widget in rows:
                    form.addRow(QLabel(label), widget)
                root.addWidget(box)

            root.addWidget(self._launch)
            root.addWidget(self._debug)
            button_row = QHBoxLayout()
            save_button = QPushButton("Save Settings")
            save_button.clicked.connect(self.save)
            button_row.addWidget(save_button)
            root.addLayout(button_row)

        def current_state(self) -> SettingsState:
            return SettingsState(
                llm_provider=self._provider.text().strip(),
                llm_model=self._model.text().strip(),
                llm_api_key=self._llm_key.text().strip(),
                image_provider=self._image_provider.text().strip(),
                image_model=self._image_model.text().strip(),
                image_api_key=self._image_key.text().strip(),
                intercept_hotkey=self._intercept.text().strip(),
                undo_hotkey=self._undo.text().strip(),
                silent_toggle_hotkey=self._silent.text().strip(),
                ui_mode=self._ui_mode.currentText().strip(),
                log_path=self._log_path.text().strip(),
                launch_at_startup=self._launch.isChecked(),
                debug_console=self._debug.isChecked(),
            )

        def save(self) -> None:
            self._on_save(self.current_state())

else:
    class SettingsWindow:
        def __init__(self, state: SettingsState, on_save):
            self.state = state
            self._on_save = on_save

        def current_state(self) -> SettingsState:
            return self.state

        def save(self) -> None:
            self._on_save(self.state)
