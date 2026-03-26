from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

from actionflow.app.paths import (
    APP_NAME,
    linux_autostart_path,
    packaged_executable_path,
    project_root,
    recommended_launcher_path,
    resource_path,
    windows_startup_shortcut_path,
)

PROJECT_ROOT = project_root()
MAIN_SCRIPT = PROJECT_ROOT / "main.py"


def _windows_startup_path() -> Path:
    return windows_startup_shortcut_path()


def _linux_autostart_path() -> Path:
    return linux_autostart_path()


def _launcher_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{packaged_executable_path()}"'
    if platform.system() == "Windows":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        python_bin = pythonw if pythonw.exists() else Path(sys.executable)
        launcher = PROJECT_ROOT / "ActionFlow.pyw"
        return f'"{python_bin}" "{launcher}"'
    return f'"{sys.executable}" "{MAIN_SCRIPT}"'


def configure_launch_at_startup(enabled: bool) -> Path:
    system = platform.system()
    if system == "Windows":
        target = _windows_startup_path()
        if enabled:
            target.parent.mkdir(parents=True, exist_ok=True)
            content = f'@echo off\r\nstart "" {_launcher_command()}\r\n'
            target.write_text(content, encoding="utf-8")
        elif target.exists():
            target.unlink()
        return target

    target = _linux_autostart_path()
    if enabled:
        target.parent.mkdir(parents=True, exist_ok=True)
        icon_path = resource_path("assets", "actionflow.png")
        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            f"Exec={_launcher_command()}\n"
            f"Icon={icon_path}\n"
            "X-GNOME-Autostart-enabled=true\n"
            "Terminal=false\n"
        )
        target.write_text(content, encoding="utf-8")
    elif target.exists():
        target.unlink()
    return target


def launch_at_startup_enabled() -> bool:
    target = _windows_startup_path() if platform.system() == "Windows" else _linux_autostart_path()
    return target.exists()
