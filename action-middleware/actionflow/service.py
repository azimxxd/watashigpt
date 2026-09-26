"""Start at login: macOS LaunchAgent (Linux systemd lives in platform_linux)."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from actionflow.tui import TUI

LAUNCH_AGENT_LABEL = "com.watashigpt.actionflow"
LAUNCH_AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
MAC_LOG_PATH = Path.home() / "Library" / "Logs" / "ActionFlow.log"


def launch_agent_plist(script_path: Path) -> dict:
    return {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": [sys.executable, str(script_path)],
        "WorkingDirectory": str(script_path.parent),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},  # restart after crashes, not after Quit
        "ThrottleInterval": 10,
        "ProcessType": "Interactive",
        "StandardOutPath": str(MAC_LOG_PATH),
        "StandardErrorPath": str(MAC_LOG_PATH),
        "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "PYTHONUNBUFFERED": "1"},
    }


def install_launch_agent(script_path: Path) -> None:
    """ActionFlow starts at login and lives in the menu bar (no terminal)."""
    LAUNCH_AGENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    MAC_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LAUNCH_AGENT_PATH, "wb") as f:
        plistlib.dump(launch_agent_plist(script_path), f)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(LAUNCH_AGENT_PATH)], capture_output=True)
    proc = subprocess.run(["launchctl", "bootstrap", domain, str(LAUNCH_AGENT_PATH)],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"{TUI.RED}launchctl failed: {proc.stderr.strip()}{TUI.RESET}")
        sys.exit(1)
    print(f"{TUI.GREEN}✓{TUI.RESET} Installed {LAUNCH_AGENT_PATH}")
    print("  ActionFlow now starts at login and lives in the menu bar.")
    print(f"  Logs: {MAC_LOG_PATH}\n")
    print(f"  {TUI.YELLOW}Grant Accessibility + Input Monitoring to this Python binary{TUI.RESET}")
    print("  (System Settings → Privacy & Security → '+', ⌘⇧G to paste the path):")
    print(f"    {os.path.realpath(sys.executable)}")
    print("  API keys must be in the Keychain:  python main.py --set-key <provider>")
    print("  Remove with:  python main.py --uninstall")


def uninstall_launch_agent() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(LAUNCH_AGENT_PATH)],
                   capture_output=True)
    if LAUNCH_AGENT_PATH.exists():
        LAUNCH_AGENT_PATH.unlink()
        print(f"{TUI.GREEN}✓{TUI.RESET} Removed {LAUNCH_AGENT_PATH}")
    else:
        print("Login agent was not installed.")
