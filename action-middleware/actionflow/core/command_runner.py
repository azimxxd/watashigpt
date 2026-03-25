from __future__ import annotations

from dataclasses import dataclass
import os
import re
import shlex


WINDOWS_SHELL_BUILTINS = {"dir", "echo", "type"}
POSIX_SHELL_BUILTINS = {"echo", "printf", "pwd", "type"}
_DANGEROUS_SHELL_TOKENS = {"&&", "||", "|", ";", ">", ">>", "<", "<<", "&"}
_DANGEROUS_SHELL_CHARS_RE = re.compile(r"[\r\n`]|(?:\$\()|(?:\$\{)")


@dataclass(frozen=True)
class PreparedCommand:
    argv: list[str]
    binary: str
    display_command: str
    uses_shell_wrapper: bool = False


def _split_command(command: str, *, is_windows: bool) -> list[str]:
    return shlex.split(command, posix=not is_windows)


def prepare_command_for_execution(command: str, allowlist: list[str], *, is_windows: bool) -> PreparedCommand:
    stripped = command.strip()
    if not stripped:
        raise ValueError("CMD expects a command after the prefix")
    if _DANGEROUS_SHELL_CHARS_RE.search(stripped):
        raise ValueError("Command contains unsupported shell control characters")

    parts = _split_command(stripped, is_windows=is_windows)
    if not parts:
        raise ValueError("CMD expects a command after the prefix")
    if any(token in _DANGEROUS_SHELL_TOKENS for token in parts):
        raise ValueError("Pipes, redirects, and chained shell operators are not allowed")

    allowset = {item.lower() for item in allowlist}
    binary = os.path.basename(parts[0]).lower()
    if binary not in allowset:
        raise PermissionError(f"'{binary}' is not in the allowed command list")

    if is_windows and binary in WINDOWS_SHELL_BUILTINS:
        return PreparedCommand(
            argv=["cmd.exe", "/d", "/s", "/c", stripped],
            binary=binary,
            display_command=stripped,
            uses_shell_wrapper=True,
        )

    if not is_windows and binary in POSIX_SHELL_BUILTINS:
        return PreparedCommand(
            argv=["/bin/sh", "-lc", stripped],
            binary=binary,
            display_command=stripped,
            uses_shell_wrapper=True,
        )

    return PreparedCommand(argv=parts, binary=binary, display_command=stripped)


def format_command_result(stdout: str, stderr: str, exit_code: int) -> str:
    clean_stdout = (stdout or "").strip()
    clean_stderr = (stderr or "").strip()
    if clean_stdout:
        return clean_stdout
    if clean_stderr:
        return clean_stderr
    if exit_code == 0:
        return "(no output)"
    return f"(exit {exit_code} with no output)"
