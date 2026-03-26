from __future__ import annotations

import os
import platform
import sys
from pathlib import Path


APP_NAME = "ActionFlow"
APP_SLUG = "actionflow"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def package_root() -> Path:
    if is_frozen() and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parents[2]


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resource_path(*parts: str) -> Path:
    return package_root().joinpath(*parts)


def _windows_roaming_dir() -> Path:
    return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME


def _windows_local_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME


def config_dir() -> Path:
    if platform.system() == "Windows":
        return _windows_roaming_dir()
    return Path.home() / ".config" / APP_SLUG


def data_dir() -> Path:
    if platform.system() == "Windows":
        return _windows_local_dir()
    state_home = os.environ.get("XDG_STATE_HOME", "")
    if state_home:
        return Path(state_home) / APP_SLUG
    return Path.home() / ".local" / "state" / APP_SLUG


def logs_dir() -> Path:
    return data_dir() / "logs"


def user_config_path() -> Path:
    env_override = os.environ.get("ACTIONFLOW_CONFIG_PATH", "").strip()
    if env_override:
        return Path(env_override).expanduser()
    if is_frozen():
        return config_dir() / "config.yaml"
    return project_root() / "config.yaml"


def example_config_path() -> Path:
    env_override = os.environ.get("ACTIONFLOW_EXAMPLE_CONFIG_PATH", "").strip()
    if env_override:
        return Path(env_override).expanduser()
    return resource_path("config.yaml.example")


def secrets_path() -> Path:
    return config_dir() / "actionflow_secrets.yaml"


def clips_path() -> Path:
    return data_dir() / "actionflow_clips.json"


def history_path() -> Path:
    return data_dir() / "actionflow_history.jsonl"


def runtime_log_path() -> Path:
    return logs_dir() / "actionflow.log"


def startup_log_path() -> Path:
    return logs_dir() / "actionflow_startup.log"


def desktop_entry_name() -> str:
    return "actionflow.desktop"


def windows_startup_shortcut_path() -> Path:
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "ActionFlow.bat"


def linux_autostart_path() -> Path:
    return Path.home() / ".config" / "autostart" / desktop_entry_name()


def linux_applications_path() -> Path:
    return Path.home() / ".local" / "share" / "applications" / desktop_entry_name()


def linux_icons_path() -> Path:
    return Path.home() / ".local" / "share" / "icons" / "hicolor" / "256x256" / "apps" / "actionflow.png"


def packaged_executable_path() -> Path:
    return Path(sys.executable).resolve()


def source_launcher_path() -> Path:
    root = project_root()
    return root / ("ActionFlow.pyw" if platform.system() == "Windows" else "main.py")


def recommended_launcher_path() -> Path:
    return packaged_executable_path() if is_frozen() else source_launcher_path()

