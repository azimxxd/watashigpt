import pytest

from actionflow.core.command_runner import format_command_result, prepare_command_for_execution


def test_prepare_command_allows_plain_echo():
    prepared = prepare_command_for_execution("echo hello", ["echo", "ls"], is_windows=False)
    assert prepared.binary == "echo"
    assert prepared.argv[0] == "/bin/sh"


def test_prepare_command_supports_windows_dir_builtin():
    prepared = prepare_command_for_execution("dir", ["dir", "echo"], is_windows=True)
    assert prepared.binary == "dir"
    assert prepared.argv[:4] == ["cmd.exe", "/d", "/s", "/c"]


def test_prepare_command_rejects_shell_chaining():
    with pytest.raises(ValueError):
        prepare_command_for_execution("echo hello && whoami", ["echo", "whoami"], is_windows=False)


def test_prepare_command_rejects_non_allowlisted_binary():
    with pytest.raises(PermissionError):
        prepare_command_for_execution("powershell Get-ChildItem", ["echo", "dir"], is_windows=True)


def test_format_command_result_prefers_stdout_then_stderr():
    assert format_command_result("hello\n", "warn", 0) == "hello"
    assert format_command_result("", "warn\n", 2) == "warn"
