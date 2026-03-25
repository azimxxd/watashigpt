from pathlib import Path

from actionflow.app import notifications as notifications_module
from actionflow.app.notifications import NotificationManager, NotificationSettings


def _build_manager(tmp_path, mode="silent", **overrides):
    manager = NotificationManager()
    sent: list[tuple[str, str]] = []
    settings = NotificationSettings(
        ui_mode=mode,
        log_path=tmp_path / "actionflow.log",
        **overrides,
    )
    manager.configure(settings)
    manager.set_sender(lambda title, message: sent.append((title, message)))
    return manager, sent


def test_successful_command_in_silent_mode_produces_no_notification(tmp_path):
    manager, sent = _build_manager(tmp_path, mode="silent")
    shown = manager.notify_info("Translate", "Done", success=True)
    assert shown is False
    assert sent == []


def test_repeated_errors_are_debounced(tmp_path):
    manager, sent = _build_manager(tmp_path, mode="minimal")
    assert manager.notify_error("Command Error", "Command failed") is True
    assert manager.notify_error("Command Error", "Command failed") is False
    assert len(sent) == 1


def test_debug_mode_still_shows_diagnostic_output_and_notifications(tmp_path):
    manager, sent = _build_manager(tmp_path, mode="debug")
    assert manager.should_print_terminal("status") is True
    assert manager.notify_info("Translate", "Done", success=True) is True
    assert sent == [("Translate", "Done")]


def test_silent_mode_keeps_terminal_logging_visible(tmp_path):
    manager, _ = _build_manager(tmp_path, mode="silent")
    assert manager.should_print_terminal("status") is True
    assert manager.should_print_terminal("box") is True
    assert manager.should_show_result_popup() is True


def test_terminal_output_is_disabled_when_no_console(tmp_path, monkeypatch):
    manager, _ = _build_manager(tmp_path, mode="silent")
    monkeypatch.setattr(notifications_module.sys, "stdout", None)
    assert manager.should_print_terminal("status") is False


def test_critical_setup_error_surfaces_even_in_silent_mode(tmp_path):
    manager, sent = _build_manager(tmp_path, mode="silent")
    assert manager.notify_error("LLM Setup Required", "Need API key", critical=True) is True
    assert sent == [("LLM Setup Required", "Need API key")]


def test_image_saved_path_only_shows_when_enabled(tmp_path):
    manager, sent = _build_manager(tmp_path, mode="silent", notify_on_image_save=True)
    assert manager.notify_info("Image Saved", "Saved at path", image_saved=True) is True
    assert sent == [("Image Saved", "Saved at path")]

    manager_disabled, sent_disabled = _build_manager(tmp_path, mode="silent", notify_on_image_save=False)
    assert manager_disabled.notify_info("Image Saved", "Saved at path", image_saved=True) is False
    assert sent_disabled == []


def test_config_driven_behavior_applies_log_path_and_mode(tmp_path):
    manager = NotificationManager()
    config = {
        "ui": {
            "mode": "minimal",
            "show_success_notifications": True,
            "log_level": "debug",
            "log_path": str(tmp_path / "custom.log"),
        }
    }
    manager.configure_from_config(config)
    assert manager.mode == "minimal"
    assert manager.settings.show_success_notifications is True
    assert manager.settings.log_path == Path(tmp_path / "custom.log")
