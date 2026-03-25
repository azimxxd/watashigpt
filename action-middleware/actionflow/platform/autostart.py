from __future__ import annotations

import os
import platform
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAIN_SCRIPT = PROJECT_ROOT / "main.py"


def _windows_startup_path() -> Path:
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "ActionFlow.bat"


def _linux_autostart_path() -> Path:
    return Path.home() / ".config" / "autostart" / "actionflow.desktop"


def configure_launch_at_startup(enabled: bool) -> Path:
    system = platform.system()
    if system == "Windows":
        target = _windows_startup_path()
        if enabled:
            target.parent.mkdir(parents=True, exist_ok=True)
            pythonw = Path(sys.executable).with_name("pythonw.exe")
            python_bin = pythonw if pythonw.exists() else Path(sys.executable)
            content = f'@echo off\r\nstart "" "{python_bin}" "{MAIN_SCRIPT}"\r\n'
            target.write_text(content, encoding="utf-8")
        elif target.exists():
            target.unlink()
        return target

    target = _linux_autostart_path()
    if enabled:
        target.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=ActionFlow\n"
            f"Exec={sys.executable} {MAIN_SCRIPT}\n"
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
