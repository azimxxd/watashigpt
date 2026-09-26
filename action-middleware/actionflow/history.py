"""Command history: ~/.actionflow_history.jsonl (append-only, 0600).

Selected text is private by default — only lengths are stored unless
config.yaml sets history.log_text: true.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from actionflow.privacy import log_texts
from actionflow.tui import TUI

HISTORY_PATH = Path.home() / ".actionflow_history.jsonl"

_MAX_LINES, _KEEP_LINES = 5000, 3000


def log(command: str, input_text: str, output_text: str, duration_ms: int, *,
        provider: str = "builtin", app_context: str = "", text_length: int = 0,
        text_language: str = "", trigger: str = "") -> None:
    try:
        input_text, output_text = log_texts(command, input_text, output_text)
        entry = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "command": command,
            "input": input_text[:500],
            "output": output_text[:500],
            "duration_ms": duration_ms,
            "provider": provider,
            "app_context": app_context,
            "text_length": text_length,
            "text_language": text_language,
            "trigger": trigger,
        }
        fd = os.open(HISTORY_PATH, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        TUI.warn(f"History log write failed: {exc}")


def entries(limit: int | None = None, command_filter: str | None = None) -> list[dict]:
    """Parsed history entries (oldest first), optionally the last `limit`."""
    out: list[dict] = []
    if not HISTORY_PATH.exists():
        return out
    with open(HISTORY_PATH, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if command_filter and command_filter.lower() not in entry.get("command", "").lower():
                continue
            out.append(entry)
    return out[-limit:] if limit else out


def rotate() -> None:
    """Keep the file bounded: past _MAX_LINES, keep the newest _KEEP_LINES."""
    try:
        if not HISTORY_PATH.exists():
            return
        lines = HISTORY_PATH.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        if len(lines) > _MAX_LINES:
            tmp = HISTORY_PATH.with_suffix(".tmp")
            tmp.write_text("".join(lines[-_KEEP_LINES:]), encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(HISTORY_PATH)
    except Exception as exc:
        TUI.warn(f"History rotation failed: {exc}")


def recent_text(limit: int = 20) -> str:
    rows = []
    for e in entries(limit):
        inp = str(e.get("input", ""))[:40].replace("\n", " ")
        out = str(e.get("output", ""))[:40].replace("\n", " ")
        rows.append(f"{e.get('ts', '?')[:19]}  {e.get('command', '?'):<12}  {inp}  →  {out}")
    return "\n".join(rows) or "No history yet."


def _md_cell(value) -> str:
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")[:60]


def export_session(since: datetime, mode_line: str) -> Path:
    """Write this session's entries to ~/actionflow_session_<ts>.md."""
    since_iso = since.isoformat(timespec="seconds")
    rows = [e for e in entries() if e.get("ts", "") >= since_iso]
    lines = [
        "# ActionFlow Session Export", "",
        f"- **Date**: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- **Mode**: {mode_line}", "", "## Activity Log", "",
    ]
    if rows:
        lines += ["| Time | Command | Input | Output | Duration |",
                  "|------|---------|-------|--------|----------|"]
        lines += [f"| {_md_cell(e.get('ts', ''))} | `{_md_cell(e.get('command', ''))}` | "
                  f"{_md_cell(e.get('input', ''))} | {_md_cell(e.get('output', ''))} | "
                  f"{e.get('duration_ms', 0)}ms |" for e in rows]
    else:
        lines.append("_No activity recorded this session._")
    path = Path.home() / f"actionflow_session_{datetime.now():%Y%m%d_%H%M%S}.md"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def show_cli(command_filter: str | None = None) -> None:
    """`main.py --history [--grep CMD]`: last 50 entries as a table."""
    rows = entries(50, command_filter)
    if not rows:
        label = f" matching \"{command_filter}\"" if command_filter else ""
        print(f"{TUI.YELLOW}No history entries found{label} ({HISTORY_PATH}).{TUI.RESET}")
        return
    print(f"\n  {TUI.BOLD}{TUI.CYAN}{'Timestamp':<20} {'Command':<12} {'Input':<30} "
          f"{'Output':<30} {'ms':>6}  {'Provider':<15}{TUI.RESET}")
    print(f"  {TUI.DIM}{'─' * 118}{TUI.RESET}")
    for e in rows:
        out = str(e.get("output", ""))
        color = TUI.RED if out.startswith("ERROR:") else TUI.CYAN
        print(f"  {TUI.DIM}{e.get('ts', '?')[:19]:<20}{TUI.RESET} "
              f"{color}{TUI.BOLD}{str(e.get('command', '?'))[:11]:<12}{TUI.RESET} "
              f"{TUI.DIM}{str(e.get('input', ''))[:28].replace(chr(10), ' '):<30} "
              f"{out[:28].replace(chr(10), ' '):<30}{TUI.RESET} "
              f"{TUI.DIM}{e.get('duration_ms', 0):>6}  {str(e.get('provider', '?'))[:15]:<15}{TUI.RESET}")
    label = f" (filtered: {command_filter})" if command_filter else ""
    print(f"\n  {TUI.DIM}{len(rows)} entries{label}{TUI.RESET}\n")
