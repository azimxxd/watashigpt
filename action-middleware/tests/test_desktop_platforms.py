from __future__ import annotations

from pathlib import Path

from actionflow.platform import autostart


def test_windows_autostart_file_can_be_created(monkeypatch, tmp_path):
    monkeypatch.setattr("actionflow.platform.autostart.platform.system", lambda: "Windows")
    monkeypatch.setattr("actionflow.platform.autostart._windows_startup_path", lambda: tmp_path / "ActionFlow.bat")

    target = autostart.configure_launch_at_startup(True)

    assert target.exists()
    assert "main.py" in target.read_text(encoding="utf-8")


def test_linux_autostart_file_can_be_created(monkeypatch, tmp_path):
    monkeypatch.setattr("actionflow.platform.autostart.platform.system", lambda: "Linux")
    monkeypatch.setattr("actionflow.platform.autostart._linux_autostart_path", lambda: tmp_path / "actionflow.desktop")

    target = autostart.configure_launch_at_startup(True)

    assert target.exists()
    assert "Desktop Entry" in target.read_text(encoding="utf-8")
