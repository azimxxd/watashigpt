from __future__ import annotations

from dataclasses import dataclass

from actionflow.ui.qt_compat import QT_AVAILABLE


@dataclass
class CommandPaletteResult:
    command_name: str
    payload: str


def build_command_choices(commands: dict[str, dict]) -> list[tuple[str, str]]:
    choices: list[tuple[str, str]] = []
    for name, config in commands.items():
        description = str(config.get("description", "")).strip()
        choices.append((name, description))
    return sorted(choices, key=lambda item: item[0])


if QT_AVAILABLE:  # pragma: no cover
    from PySide6.QtWidgets import (
        QDialog,
        QDialogButtonBox,
        QHBoxLayout,
        QLabel,
        QLineEdit,
        QListWidget,
        QListWidgetItem,
        QVBoxLayout,
    )

    class CommandPaletteDialog(QDialog):
        def __init__(self, selected_text: str, commands: dict[str, dict]):
            super().__init__()
            self.setWindowTitle("Quick Command")
            self._payload = QLineEdit(selected_text)
            self._argument = QLineEdit()
            self._list = QListWidget()
            self._choices = build_command_choices(commands)
            for name, description in self._choices:
                QListWidgetItem(f"{name}  {description}".strip(), self._list)
            if self._choices:
                self._list.setCurrentRow(0)

            layout = QVBoxLayout(self)
            layout.addWidget(QLabel("Selected text"))
            layout.addWidget(self._payload)
            layout.addWidget(QLabel("Optional extra arg for tone/trans"))
            layout.addWidget(self._argument)
            layout.addWidget(self._list)
            buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
            buttons.accepted.connect(self.accept)
            buttons.rejected.connect(self.reject)
            layout.addWidget(buttons)

        def selected_result(self) -> CommandPaletteResult | None:
            row = self._list.currentRow()
            if row < 0 or row >= len(self._choices):
                return None
            command_name = self._choices[row][0]
            payload = self._payload.text()
            extra = self._argument.text().strip()
            if command_name == "trans" and extra:
                payload = f"{extra.upper()}: {payload}"
            elif command_name == "tone" and extra:
                payload = f"{extra}: {payload}"
            return CommandPaletteResult(command_name=command_name, payload=payload)

else:
    class CommandPaletteDialog:
        def __init__(self, selected_text: str, commands: dict[str, dict]):
            self.selected_text = selected_text
            self.commands = commands
            self.result: CommandPaletteResult | None = None

        def exec(self) -> int:
            return 0

        def selected_result(self) -> CommandPaletteResult | None:
            return self.result
