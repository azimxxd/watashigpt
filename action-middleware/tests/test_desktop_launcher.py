from __future__ import annotations

import runpy
from pathlib import Path
from types import SimpleNamespace

from actionflow.app import main as app_main
from actionflow.app.bootstrap import BootstrapState
from actionflow.app.launcher import DesktopAppLauncher
from actionflow.app.startup_logging import configure_startup_log_path
from actionflow.core.llm_ops import LLMSetupChoice


def _configure_launcher_runtime(monkeypatch, tmp_path, *, llm_needs_setup: bool = False):
    history_path = tmp_path / "history.jsonl"
    history_path.write_text('{"ts":"2026-03-26T10:00:00","command":"count","input":"hi","output":"Words: 1","status":"success"}\n', encoding="utf-8")
    log_path = tmp_path / "actionflow.log"
    log_path.write_text("2026-03-26 INFO launched\n", encoding="utf-8")

    config = {
        "llm": {"provider": "groq", "model": "llama-3.3-70b-versatile"},
        "image_generation": {"provider": "pollinations", "model": "flux"},
        "hotkeys": {"intercept": "ctrl+alt+x", "undo": "ctrl+alt+z", "silent_toggle": "ctrl+alt+s"},
        "ui": {"mode": "silent", "log_path": str(log_path)},
        "app": {"launch_at_startup": False, "debug_console": False},
    }
    snapshot = {
        "llm_state": "needs_setup" if llm_needs_setup else "ready",
        "llm_provider": "groq",
        "llm_model": "llama-3.3-70b-versatile",
        "hotkeys": dict(config["hotkeys"]),
        "hotkeys_registered": True,
        "hotkeys_error": None,
        "hotkey_backend": "FakeHotkeys",
        "hotkey_callback_count": 0,
        "last_hotkey_callback_at": None,
        "last_hotkey_error": None,
        "command_count": 3,
        "history_path": str(history_path),
        "log_path": str(log_path),
        "config_path": str(tmp_path / "config.yaml"),
        "ui_mode": "silent",
        "runtime_initialized": True,
        "runtime_polling_active": False,
        "runtime_health": "ready",
        "runtime_health_reason": "background runtime active",
    }
    commands = {
        "count": {"description": "Count stats", "prefixes": ["COUNT:"]},
        "wiki": {"description": "Wiki lookup", "prefixes": ["WIKI:"]},
        "trans": {"description": "Translate", "prefixes": ["TRANS:"]},
    }

    monkeypatch.setattr("actionflow.app.launcher.launch_at_startup_enabled", lambda: False)
    monkeypatch.setattr("actionflow.app.launcher.runtime.CONFIG", config, raising=False)
    monkeypatch.setattr("actionflow.app.launcher.runtime.initialize_background_runtime", lambda **kwargs: snapshot)
    monkeypatch.setattr("actionflow.app.launcher.runtime.get_log_path", lambda: log_path)
    monkeypatch.setattr("actionflow.app.launcher.runtime.get_history_path", lambda: history_path)
    monkeypatch.setattr("actionflow.app.launcher.runtime.get_available_commands", lambda: commands)
    monkeypatch.setattr("actionflow.app.launcher.runtime.get_runtime_snapshot", lambda: snapshot)
    monkeypatch.setattr("actionflow.app.launcher.runtime.set_setup_handlers", lambda **kwargs: None)
    monkeypatch.setattr("actionflow.app.launcher.runtime.save_secret_values", lambda **kwargs: None)
    monkeypatch.setattr("actionflow.app.launcher.runtime.reload_runtime_config", lambda: snapshot)
    monkeypatch.setattr("actionflow.app.launcher.runtime.restart_hotkeys", lambda: None)
    monkeypatch.setattr("actionflow.app.launcher.runtime.set_runtime_polling_active", lambda active: snapshot.__setitem__("runtime_polling_active", active))
    monkeypatch.setattr("actionflow.app.launcher.runtime.poll_picker_request", lambda: None)
    monkeypatch.setattr("actionflow.app.launcher.runtime.poll_result_request", lambda: None)
    monkeypatch.setattr("actionflow.app.launcher.runtime.notifications", SimpleNamespace(should_show_result_popup=lambda **kwargs: True))
    monkeypatch.setattr(
        "actionflow.app.launcher.build_bootstrap_state",
        lambda *args, **kwargs: BootstrapState(
            config_path=Path(snapshot["config_path"]),
            log_path=log_path,
            history_path=history_path,
            has_user_config=True,
            llm_ready=not llm_needs_setup,
            llm_needs_setup=llm_needs_setup,
            image_key_present=False,
            qt_available=False,
        ),
    )

    return snapshot, history_path, log_path


def test_app_starts_in_gui_tray_mode(monkeypatch, tmp_path):
    configure_startup_log_path(tmp_path / "startup.log")
    snapshot, _, _ = _configure_launcher_runtime(monkeypatch, tmp_path)
    launcher = DesktopAppLauncher()

    status = launcher.initialize()

    assert status["runtime_initialized"] is True
    assert launcher.tray is not None
    assert launcher.tray.icon_created is True
    assert launcher.main_window is not None
    assert snapshot["runtime_polling_active"] is True
    assert launcher.tray.status_name == "ready"


def test_settings_window_opens_from_main_window(monkeypatch, tmp_path):
    configure_startup_log_path(tmp_path / "startup.log")
    _configure_launcher_runtime(monkeypatch, tmp_path)
    launcher = DesktopAppLauncher()
    launcher.initialize()

    launcher.open_settings()

    assert launcher.main_window.visible_tab == "settings"


def test_first_run_setup_opens_when_token_missing(monkeypatch, tmp_path):
    configure_startup_log_path(tmp_path / "startup.log")
    _configure_launcher_runtime(monkeypatch, tmp_path, llm_needs_setup=True)
    opened = {"count": 0}

    monkeypatch.setattr(DesktopAppLauncher, "run_first_run_setup", lambda self: opened.__setitem__("count", opened["count"] + 1))
    launcher = DesktopAppLauncher()
    launcher.initialize()

    assert opened["count"] == 1


def test_hotkey_registration_can_be_restarted_from_gui(monkeypatch, tmp_path):
    configure_startup_log_path(tmp_path / "startup.log")
    _configure_launcher_runtime(monkeypatch, tmp_path)
    restarted = {"count": 0}
    monkeypatch.setattr("actionflow.app.launcher.runtime.restart_hotkeys", lambda: restarted.__setitem__("count", restarted["count"] + 1))
    launcher = DesktopAppLauncher()
    launcher.initialize()

    launcher.restart_hotkeys()

    assert restarted["count"] == 1


def test_picker_opens_without_prefix(monkeypatch, tmp_path):
    configure_startup_log_path(tmp_path / "startup.log")
    _configure_launcher_runtime(monkeypatch, tmp_path)
    items = iter(["plain text", None])
    opened = {}
    monkeypatch.setattr("actionflow.app.launcher.runtime.poll_picker_request", lambda: next(items))
    monkeypatch.setattr(DesktopAppLauncher, "open_command_palette", lambda self, text: opened.setdefault("text", text))

    launcher = DesktopAppLauncher()
    launcher.initialize()
    launcher.poll_runtime_events()

    assert opened["text"] == "plain text"


def test_result_window_commands_still_show_windows(monkeypatch, tmp_path):
    configure_startup_log_path(tmp_path / "startup.log")
    _configure_launcher_runtime(monkeypatch, tmp_path)
    items = iter([("Wikipedia: Python", "summary", False, True), None])
    monkeypatch.setattr("actionflow.app.launcher.runtime.poll_result_request", lambda: next(items))
    launcher = DesktopAppLauncher()
    launcher.initialize()

    launcher.poll_runtime_events()

    assert launcher.result_windows.shown_results[0][0] == "Wikipedia: Python"


def test_history_and_logs_are_accessible_from_gui(monkeypatch, tmp_path):
    configure_startup_log_path(tmp_path / "startup.log")
    _configure_launcher_runtime(monkeypatch, tmp_path)
    launcher = DesktopAppLauncher()
    launcher.initialize()

    launcher.open_history()
    assert launcher.main_window.visible_tab == "history"
    assert launcher.main_window.history_view.entries[0]["command"] == "count"

    launcher.open_logs()
    assert launcher.main_window.visible_tab == "logs"
    assert "launched" in launcher.main_window.logs_view.text


def test_gui_still_starts_with_corrupted_history_file(monkeypatch, tmp_path):
    configure_startup_log_path(tmp_path / "startup.log")
    snapshot, history_path, _ = _configure_launcher_runtime(monkeypatch, tmp_path)
    history_path.write_bytes(b'{"command":"count","input":"ok","output":"ok"}\n\xff\xfe')
    monkeypatch.setitem(snapshot, "history_path", str(history_path))

    launcher = DesktopAppLauncher()
    launcher.initialize()

    assert launcher.main_window is not None
    assert launcher.main_window.history_view.entries[0]["command"] == "count"


def test_status_is_not_green_when_hotkeys_failed(monkeypatch, tmp_path):
    configure_startup_log_path(tmp_path / "startup.log")
    snapshot, _, _ = _configure_launcher_runtime(monkeypatch, tmp_path)
    snapshot["hotkeys_registered"] = False
    snapshot["hotkeys_error"] = "hook failed"
    snapshot["runtime_health"] = "partial"
    snapshot["runtime_health_reason"] = "hotkeys unavailable: hook failed"
    launcher = DesktopAppLauncher()

    launcher.initialize()

    assert launcher.tray.status_name == "partial"
    assert "hotkeys unavailable" in launcher.tray.status_message


def test_config_persists_from_settings_ui(monkeypatch, tmp_path):
    configure_startup_log_path(tmp_path / "startup.log")
    _configure_launcher_runtime(monkeypatch, tmp_path)
    saved = {}
    monkeypatch.setattr("actionflow.app.launcher.save_config", lambda config: saved.setdefault("config", config))
    launcher = DesktopAppLauncher()
    launcher.initialize()

    state = launcher._current_settings_state()
    state.llm_provider = "openai"
    state.llm_model = "gpt-4o-mini"
    state.llm_api_key = "secret-key"
    state.launch_at_startup = True
    launcher.save_settings(state)

    assert saved["config"]["llm"]["provider"] == "openai"
    assert saved["config"]["app"]["launch_at_startup"] is True


def test_debug_console_mode_still_uses_console_runtime(monkeypatch):
    calls = {"console": 0, "gui": 0}
    monkeypatch.setattr("actionflow.app.main.main", lambda **kwargs: calls.__setitem__("console", calls["console"] + 1))
    monkeypatch.setattr("actionflow.app.main.DesktopAppLauncher", lambda **kwargs: SimpleNamespace(run=lambda: calls.__setitem__("gui", calls["gui"] + 1)))
    monkeypatch.setattr("sys.argv", ["actionflow", "--debug-console"])

    app_main.run()

    assert calls["console"] == 1
    assert calls["gui"] == 0


def test_normal_launch_uses_gui_mode_not_console(monkeypatch):
    calls = {"console": 0, "gui": 0}
    monkeypatch.setattr("actionflow.app.main.main", lambda **kwargs: calls.__setitem__("console", calls["console"] + 1))
    monkeypatch.setattr("actionflow.app.main.DesktopAppLauncher", lambda **kwargs: SimpleNamespace(run=lambda: calls.__setitem__("gui", calls["gui"] + 1)))
    monkeypatch.setattr("sys.argv", ["actionflow"])

    app_main.run()

    assert calls["gui"] == 1
    assert calls["console"] == 0


def test_setup_dialog_result_can_be_applied(monkeypatch, tmp_path):
    configure_startup_log_path(tmp_path / "startup.log")
    _configure_launcher_runtime(monkeypatch, tmp_path)
    captured = {}
    monkeypatch.setattr("actionflow.app.launcher.runtime.save_secret_values", lambda **kwargs: captured.update(kwargs))
    monkeypatch.setattr("actionflow.app.launcher.save_config", lambda config: None)
    launcher = DesktopAppLauncher()
    launcher.initialize()

    launcher.apply_setup_result(
        LLMSetupChoice(action="configure", provider="groq", api_key="abc", model="llama"),
        image_api_key="img",
    )

    assert captured["llm_api_key"] == "abc"
    assert captured["image_api_key"] == "img"


def test_startup_log_is_written_in_normal_mode(monkeypatch, tmp_path):
    startup_log = tmp_path / "startup.log"
    configure_startup_log_path(startup_log)
    _configure_launcher_runtime(monkeypatch, tmp_path)

    launcher = DesktopAppLauncher()
    launcher.run(start_event_loop=False)

    text = startup_log.read_text(encoding="utf-8")
    assert "Launcher initialization completed" in text


def test_gui_startup_exception_is_logged(monkeypatch, tmp_path):
    startup_log = tmp_path / "startup.log"
    configure_startup_log_path(startup_log)
    monkeypatch.setattr(DesktopAppLauncher, "initialize", lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr("actionflow.app.launcher._show_startup_error_dialog", lambda message: None)

    launcher = DesktopAppLauncher()
    exit_code = launcher.run(start_event_loop=False)

    assert exit_code == 1
    assert "GUI startup failed" in startup_log.read_text(encoding="utf-8")


def test_launcher_enters_background_loop_in_normal_mode(monkeypatch, tmp_path):
    startup_log = tmp_path / "startup.log"
    configure_startup_log_path(startup_log)
    _configure_launcher_runtime(monkeypatch, tmp_path)
    entered = {"loop": 0}
    monkeypatch.setattr(DesktopAppLauncher, "_run_tk_loop", lambda self: entered.__setitem__("loop", entered["loop"] + 1) or 0)

    launcher = DesktopAppLauncher()
    launcher.headless_test_mode = False
    launcher.use_qt = False
    launcher.app = None
    exit_code = launcher.run()

    assert exit_code == 0
    assert entered["loop"] == 1
    assert "Launcher initialization completed" in startup_log.read_text(encoding="utf-8")


def test_actionflow_pyw_logs_entry_and_invokes_run(monkeypatch, tmp_path):
    startup_log = tmp_path / "startup.log"
    configure_startup_log_path(startup_log)
    calls = {"run": 0}
    monkeypatch.setattr("actionflow.app.main.run", lambda: calls.__setitem__("run", calls["run"] + 1))

    runpy.run_path(str(Path(__file__).resolve().parents[1] / "ActionFlow.pyw"), run_name="__main__")

    assert calls["run"] == 1
    assert "Entered ActionFlow.pyw windowless entrypoint" in startup_log.read_text(encoding="utf-8")


def test_actionflow_pyw_logs_fatal_startup_failure(monkeypatch, tmp_path):
    startup_log = tmp_path / "startup.log"
    configure_startup_log_path(startup_log)
    monkeypatch.setattr("actionflow.app.main.run", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    import tkinter
    from tkinter import messagebox

    monkeypatch.setattr(tkinter, "Tk", lambda: SimpleNamespace(withdraw=lambda: None, destroy=lambda: None))
    monkeypatch.setattr(messagebox, "showerror", lambda *args, **kwargs: None)

    runpy.run_path(str(Path(__file__).resolve().parents[1] / "ActionFlow.pyw"), run_name="__main__")

    assert "Windowed launcher failed" in startup_log.read_text(encoding="utf-8")
