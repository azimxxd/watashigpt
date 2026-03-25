from __future__ import annotations

from dataclasses import dataclass

from actionflow.core.llm_ops import LLMSetupChoice, PROVIDER_DEFAULT_MODELS
from actionflow.ui.qt_compat import QT_AVAILABLE


@dataclass
class SetupResult:
    llm_choice: LLMSetupChoice
    image_api_key: str = ""


if QT_AVAILABLE:  # pragma: no cover
    from PySide6.QtWidgets import (
        QCheckBox,
        QComboBox,
        QDialog,
        QDialogButtonBox,
        QFormLayout,
        QLabel,
        QLineEdit,
        QVBoxLayout,
    )

    class FirstRunSetupDialog(QDialog):
        def __init__(self, title: str = "ActionFlow Setup", message: str = ""):
            super().__init__()
            self.setWindowTitle(title)
            self._provider = QComboBox()
            self._provider.addItems(list(PROVIDER_DEFAULT_MODELS.keys()))
            self._api_key = QLineEdit()
            self._api_key.setEchoMode(QLineEdit.Password)
            self._model = QLineEdit(PROVIDER_DEFAULT_MODELS[self._provider.currentText()])
            self._image_key = QLineEdit()
            self._image_key.setEchoMode(QLineEdit.Password)
            self._mock = QCheckBox("Use mock mode for now")
            self._provider.currentTextChanged.connect(self._on_provider_changed)

            layout = QVBoxLayout(self)
            if message:
                layout.addWidget(QLabel(message))
            form = QFormLayout()
            form.addRow("Provider", self._provider)
            form.addRow("API key", self._api_key)
            form.addRow("Model", self._model)
            form.addRow("Image API key", self._image_key)
            layout.addLayout(form)
            layout.addWidget(self._mock)
            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

        def _on_provider_changed(self, provider: str) -> None:
            self._model.setText(PROVIDER_DEFAULT_MODELS.get(provider, ""))

        def to_result(self) -> SetupResult:
            if self._mock.isChecked():
                choice = LLMSetupChoice(action="mock", model=self._model.text().strip())
            elif self._api_key.text().strip():
                choice = LLMSetupChoice(
                    action="configure",
                    provider=self._provider.currentText().strip(),
                    api_key=self._api_key.text().strip(),
                    model=self._model.text().strip() or PROVIDER_DEFAULT_MODELS.get(self._provider.currentText(), ""),
                )
            else:
                choice = LLMSetupChoice(action="cancel")
            return SetupResult(llm_choice=choice, image_api_key=self._image_key.text().strip())

else:
    class FirstRunSetupDialog:
        def __init__(self, title: str = "ActionFlow Setup", message: str = ""):
            self.title = title
            self.message = message
            self.result = SetupResult(llm_choice=LLMSetupChoice(action="cancel"))

        def exec(self) -> int:
            return 0

        def to_result(self) -> SetupResult:
            return self.result
