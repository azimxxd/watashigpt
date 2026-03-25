import pytest
from types import SimpleNamespace
import queue
from pathlib import Path

from actionflow.app import runtime
from actionflow.app.notifications import NotificationSettings


def test_history_writes_unicode_in_utf8(tmp_path):
    original_path = runtime._HISTORY_PATH
    try:
        runtime._HISTORY_PATH = tmp_path / "history.jsonl"
        runtime._log_history("trans", "Доброе утро", "おはようございます。", 12, text_language="ja")
        raw = runtime._HISTORY_PATH.read_bytes()
        decoded = raw.decode("utf-8")
        assert "Доброе утро" in decoded
        assert "おはようございます" in decoded
    finally:
        runtime._HISTORY_PATH = original_path


def test_count_logs_actual_output_not_stale(monkeypatch):
    original_last_output = runtime._last_command_output
    original_result = runtime._current_dispatch_result
    captured = {}

    try:
        runtime._last_command_output = "hello"
        runtime._current_dispatch_result = None

        def fake_activity(cmd_name, input_text, output_text, duration, **kwargs):
            captured["output"] = output_text

        monkeypatch.setattr(runtime.TUI, "activity_entry", fake_activity)
        runtime.dispatch("count", "hello world", "hello world", {"prefixes": ["COUNT:"]})
        assert captured["output"].startswith("Words: 2 | Chars:")
        assert captured["output"] != "hello"
    finally:
        runtime._last_command_output = original_last_output
        runtime._current_dispatch_result = original_result


def test_invalid_decode_does_not_log_success(monkeypatch):
    captured = {}
    original_last_output = runtime._last_command_output
    original_result = runtime._current_dispatch_result
    original_pending = runtime._pending_undo_entry

    try:
        runtime._last_command_output = "hello"
        runtime._current_dispatch_result = None
        runtime._pending_undo_entry = None

        def fake_activity(cmd_name, input_text, output_text, duration, **kwargs):
            captured["output"] = output_text
            captured["is_error"] = kwargs.get("is_error", False)

        monkeypatch.setattr(runtime.TUI, "activity_entry", fake_activity)

        with pytest.raises(RuntimeError):
            runtime.dispatch("decode", "%%%not-base64%%%", "%%%not-base64%%%", {"prefixes": ["DECODE:"]})

        assert captured["is_error"] is True
        assert "hello" not in captured["output"]
        assert runtime._pending_undo_entry is None
    finally:
        runtime._last_command_output = original_last_output
        runtime._current_dispatch_result = original_result
        runtime._pending_undo_entry = original_pending


def test_image_failure_does_not_replace_text_or_reuse_previous_output(monkeypatch, tmp_path):
    original_last_output = runtime._last_command_output
    original_result = runtime._current_dispatch_result
    original_image_dir = runtime._IMAGE_DIR
    original_undo_stack = list(runtime._undo_stack)
    calls = {"replace": 0}
    captured = {}

    try:
        runtime._last_command_output = "Richard"
        runtime._current_dispatch_result = None
        runtime._IMAGE_DIR = tmp_path
        runtime._undo_stack[:] = [{"original": "x", "replacement": "Richard"}]

        monkeypatch.setattr(
            runtime,
            "_generate_image_bytes",
            lambda prompt, settings: (_ for _ in ()).throw(RuntimeError("image provider requires authorization")),
        )
        monkeypatch.setattr(runtime, "_prompt_image_setup", lambda provider: "")

        def fake_replace(text):
            calls["replace"] += 1

        def fake_activity(cmd_name, input_text, output_text, duration, **kwargs):
            captured["output"] = output_text
            captured["is_error"] = kwargs.get("is_error", False)

        monkeypatch.setattr(runtime, "_replace_selection", fake_replace)
        monkeypatch.setattr(runtime.TUI, "activity_entry", fake_activity)

        with pytest.raises(RuntimeError):
            runtime.dispatch("image", "future", "IMAGE:future", {"prefixes": ["IMAGE:"]})

        assert calls["replace"] == 0
        assert captured["is_error"] is True
        assert "Richard" not in captured["output"]
        assert runtime._undo_stack[-1]["replacement"] == "Richard"
    finally:
        runtime._last_command_output = original_last_output
        runtime._current_dispatch_result = original_result
        runtime._IMAGE_DIR = original_image_dir
        runtime._undo_stack[:] = original_undo_stack


def test_explicit_prefixed_command_executes_after_capture(monkeypatch):
    routed = {}

    class FakeHotkeys:
        def release_modifiers(self):
            return None

    class FakeWindowManager:
        def capture_focus_target(self):
            return 123

        def restore_focus(self, target):
            return None

    monkeypatch.setattr(runtime, "_rate_limit_check", lambda: True)
    monkeypatch.setattr(runtime.time, "sleep", lambda _: None)
    monkeypatch.setattr(runtime, "_hotkey_manager", FakeHotkeys())
    monkeypatch.setattr(runtime, "_window_manager", FakeWindowManager())
    monkeypatch.setattr(runtime, "_capture_selection_via_copy", lambda timeout=1.2: ("IMAGE:apple", ""))
    monkeypatch.setattr(runtime, "detect_active_window", lambda: SimpleNamespace(context_type="docs"))
    monkeypatch.setattr(runtime, "analyze_text", lambda text: SimpleNamespace(looks_like="text", language="en"))
    monkeypatch.setattr(runtime, "notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "route", lambda text: routed.setdefault("text", text))

    runtime._do_intercept()

    assert routed["text"] == "IMAGE:apple"


def test_display_only_llm_command_logs_actual_output_not_done(monkeypatch):
    original_last_output = runtime._last_command_output
    original_result = runtime._current_dispatch_result
    captured = {}

    class FakeResult:
        output = "A real explanation"

    try:
        runtime._last_command_output = "stale"
        runtime._current_dispatch_result = None

        monkeypatch.setattr(runtime, "_ensure_llm_backend", lambda name="": True)
        monkeypatch.setattr(runtime, "execute_transform_command", lambda *args, **kwargs: FakeResult())
        monkeypatch.setattr(runtime, "notify", lambda *args, **kwargs: None)

        def fake_activity(cmd_name, input_text, output_text, duration, **kwargs):
            captured["output"] = output_text

        monkeypatch.setattr(runtime.TUI, "activity_entry", fake_activity)
        runtime.dispatch(
            "explain",
            "cache",
            "EXP: cache",
            {"llm_required": True, "description": "Explain", "prefixes": ["EXP:"]},
        )

        assert captured["output"] == "A real explanation"
    finally:
        runtime._last_command_output = original_last_output
        runtime._current_dispatch_result = original_result


def test_count_result_window_still_opens_in_silent_mode(monkeypatch):
    shown = {"count": 0}
    original_mode = runtime.notifications.mode

    try:
        while True:
            try:
                runtime._result_queue.get_nowait()
            except queue.Empty:
                break
        runtime.notifications.set_runtime_mode("silent")
        runtime._queue_result_popup("Text Stats", "Words: 2", critical=False)
        monkeypatch.setattr(runtime, "_show_result_popup", lambda title, text: shown.__setitem__("count", shown["count"] + 1))
        runtime._drain_result_queue_windows()
        assert shown["count"] == 1
    finally:
        runtime.notifications.set_runtime_mode(original_mode)


def test_critical_result_popup_can_surface_in_debug_mode(monkeypatch):
    shown = {"count": 0}
    original_mode = runtime.notifications.mode

    try:
        while True:
            try:
                runtime._result_queue.get_nowait()
            except queue.Empty:
                break
        runtime.notifications.set_runtime_mode("debug")
        runtime._queue_result_popup("Critical", "Need setup", critical=True)
        monkeypatch.setattr(runtime, "_show_result_popup", lambda title, text: shown.__setitem__("count", shown["count"] + 1))
        runtime._drain_result_queue_windows()
        assert shown["count"] == 1
    finally:
        runtime.notifications.set_runtime_mode(original_mode)


def test_wiki_special_ui_still_opens_in_silent_mode(monkeypatch):
    shown = {"count": 0}
    original_mode = runtime.notifications.mode

    try:
        while True:
            try:
                runtime._result_queue.get_nowait()
            except queue.Empty:
                break
        runtime.notifications.set_runtime_mode("silent")
        runtime._queue_result_popup("Wikipedia: Python", "Python summary", special_ui=True)
        monkeypatch.setattr(runtime, "_show_result_popup", lambda title, text: shown.__setitem__("count", shown["count"] + 1))
        runtime._drain_result_queue_windows()
        assert shown["count"] == 1
    finally:
        runtime.notifications.set_runtime_mode(original_mode)


def test_define_russian_query_returns_russian_result_window(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        runtime,
        "_fetch_wiktionary_wikitext",
        lambda language, word: """
= {{-ru-}} =
{{сущ-ru}}

==== Значение ====
# {{зоол.|ru}} крупная неядовитая [[змея]]
""",
    )
    monkeypatch.setattr(runtime, "_record_command_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime.TUI, "action", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runtime,
        "_queue_result_popup",
        lambda title, text, **kwargs: captured.update({"title": title, "text": text}),
    )

    runtime.handle_define("питон", "DEFINE: питон", {})

    assert captured["title"] == "Определение: питон"
    assert "крупная неядовитая змея" in captured["text"]
    assert "Example:" not in captured["text"]


def test_define_english_query_returns_english_result_window(monkeypatch):
    captured = {}

    monkeypatch.setattr(
        runtime,
        "_fetch_json",
        lambda url: [
            {
                "meanings": [
                    {
                        "partOfSpeech": "noun",
                        "definitions": [{"definition": "A large nonvenomous snake."}],
                    }
                ]
            }
        ],
    )
    monkeypatch.setattr(runtime, "_record_command_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime.TUI, "action", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runtime,
        "_queue_result_popup",
        lambda title, text, **kwargs: captured.update({"title": title, "text": text}),
    )

    runtime.handle_define("python", "DEFINE: python", {})

    assert captured["title"] == "Define: python"
    assert "A large nonvenomous snake." in captured["text"]


def test_wiki_russian_query_uses_russian_wikipedia_and_russian_ui(monkeypatch):
    captured = {}
    seen_urls = []

    def fake_fetch_json(url):
        seen_urls.append(url)
        if "page/summary" in url:
            return {"title": "Питоны", "extract": "Питоны — семейство неядовитых змей."}
        return {"query": {"search": [{"title": "Питоны"}]}}

    monkeypatch.setattr(runtime, "_fetch_json", fake_fetch_json)
    monkeypatch.setattr(runtime, "_record_command_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime.TUI, "action", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runtime,
        "_queue_result_popup",
        lambda title, text, **kwargs: captured.update({"title": title, "text": text}),
    )

    runtime.handle_wiki("питон", "WIKI: питон", {})

    assert any("ru.wikipedia.org" in url for url in seen_urls)
    assert captured["title"] == "Википедия: Питоны"
    assert "семейство неядовитых змей" in captured["text"]


def test_wiki_english_query_uses_english_wikipedia_and_english_ui(monkeypatch):
    captured = {}
    seen_urls = []

    def fake_fetch_json(url):
        seen_urls.append(url)
        if "page/summary" in url:
            return {"title": "Python", "extract": "Python is a family of nonvenomous snakes."}
        return {"query": {"search": [{"title": "Python"}]}}

    monkeypatch.setattr(runtime, "_fetch_json", fake_fetch_json)
    monkeypatch.setattr(runtime, "_record_command_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime.TUI, "action", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runtime,
        "_queue_result_popup",
        lambda title, text, **kwargs: captured.update({"title": title, "text": text}),
    )

    runtime.handle_wiki("python", "WIKI: python", {})

    assert any("en.wikipedia.org" in url for url in seen_urls)
    assert captured["title"] == "Wikipedia: Python"
    assert "family of nonvenomous snakes" in captured["text"]


def test_wiki_ambiguous_query_keeps_same_language_in_choices_ui(monkeypatch):
    captured = {}

    def fake_fetch_json(url):
        if "page/summary" in url:
            return {
                "title": "Питон",
                "type": "disambiguation",
                "extract": "Питон может означать несколько статей.",
            }
        return {
            "query": {
                "search": [
                    {"title": "Питоны"},
                    {"title": "Python"},
                ]
            }
        }

    monkeypatch.setattr(runtime, "_fetch_json", fake_fetch_json)
    monkeypatch.setattr(runtime, "_record_command_result", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime.TUI, "action", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        runtime,
        "_queue_result_popup",
        lambda title, text, **kwargs: captured.update({"title": title, "text": text}),
    )

    runtime.handle_wiki("питон", "WIKI: питон", {})

    assert "Термин неоднозначен" in captured["text"]
    assert "This term is ambiguous" not in captured["text"]


def test_no_prefix_still_opens_picker_queue(monkeypatch):
    queued = {"text": None}

    class FakeQueue:
        def put(self, value):
            queued["text"] = value

    class FakeHotkeys:
        def release_modifiers(self):
            return None

    class FakeWindowManager:
        def capture_focus_target(self):
            return 123

        def restore_focus(self, target):
            return None

    monkeypatch.setattr(runtime, "_rate_limit_check", lambda: True)
    monkeypatch.setattr(runtime.time, "sleep", lambda _: None)
    monkeypatch.setattr(runtime, "_hotkey_manager", FakeHotkeys())
    monkeypatch.setattr(runtime, "_window_manager", FakeWindowManager())
    monkeypatch.setattr(runtime, "_capture_selection_via_copy", lambda timeout=1.2: ("plain text", ""))
    monkeypatch.setattr(runtime, "detect_active_window", lambda: SimpleNamespace(context_type="docs"))
    monkeypatch.setattr(runtime, "analyze_text", lambda text: SimpleNamespace(looks_like="text", language="en"))
    monkeypatch.setattr(runtime, "_popup_queue", FakeQueue())
    monkeypatch.setattr(runtime, "notify", lambda *args, **kwargs: None)

    runtime._do_intercept()

    assert queued["text"] == "plain text"


def test_windowless_hotkey_path_does_not_crash_without_stdout(monkeypatch, tmp_path):
    queued = {"text": None}
    original_stdout = runtime.sys.stdout
    original_log_path = runtime.notifications.settings.log_path
    original_hotkey_count = runtime._hotkey_callback_count
    original_last_hotkey = runtime._last_hotkey_callback_at
    original_last_error = runtime._last_hotkey_error
    original_last_error_at = runtime._last_hotkey_error_at

    class FakeQueue:
        def put(self, value):
            queued["text"] = value

    class FakeHotkeys:
        def release_modifiers(self):
            return None

    class FakeWindowManager:
        def capture_focus_target(self):
            return 123

        def restore_focus(self, target):
            return None

    try:
        runtime.notifications.configure(
            NotificationSettings(
                ui_mode="silent",
                log_path=tmp_path / "actionflow.log",
            )
        )
        runtime.sys.stdout = None
        monkeypatch.setattr(runtime, "_rate_limit_check", lambda: True)
        monkeypatch.setattr(runtime.time, "sleep", lambda _: None)
        monkeypatch.setattr(runtime, "_hotkey_manager", FakeHotkeys())
        monkeypatch.setattr(runtime, "_window_manager", FakeWindowManager())
        monkeypatch.setattr(runtime, "_capture_selection_via_copy", lambda timeout=1.2: ("plain text", ""))
        monkeypatch.setattr(runtime, "detect_active_window", lambda: SimpleNamespace(context_type="docs"))
        monkeypatch.setattr(runtime, "analyze_text", lambda text: SimpleNamespace(looks_like="text", language="en"))
        monkeypatch.setattr(runtime, "_popup_queue", FakeQueue())
        monkeypatch.setattr(runtime, "notify", lambda *args, **kwargs: None)

        runtime._do_intercept()

        assert queued["text"] == "plain text"
        assert runtime._hotkey_callback_count == original_hotkey_count + 1
        assert runtime._last_hotkey_error is None
        log_text = Path(tmp_path / "actionflow.log").read_text(encoding="utf-8")
        assert "Hotkey callback fired" in log_text
        assert "Selection capture success" in log_text
    finally:
        runtime.sys.stdout = original_stdout
        runtime.notifications.configure(
            NotificationSettings(
                ui_mode="silent",
                log_path=original_log_path,
            )
        )
        runtime._hotkey_callback_count = original_hotkey_count
        runtime._last_hotkey_callback_at = original_last_hotkey
        runtime._last_hotkey_error = original_last_error
        runtime._last_hotkey_error_at = original_last_error_at


def test_runtime_snapshot_reports_partial_when_hotkeys_failed():
    original_runtime_initialized = runtime._runtime_initialized
    original_hotkeys_registered = runtime._hotkeys_registered
    original_hotkey_registration_error = runtime._hotkey_registration_error
    original_runtime_polling_active = runtime._runtime_polling_active

    try:
        runtime._runtime_initialized = True
        runtime._hotkeys_registered = False
        runtime._hotkey_registration_error = "hook failed"
        runtime._runtime_polling_active = True

        snapshot = runtime.get_runtime_snapshot()

        assert snapshot["runtime_health"] == "partial"
        assert snapshot["hotkeys_registered"] is False
        assert snapshot["hotkeys_error"] == "hook failed"
    finally:
        runtime._runtime_initialized = original_runtime_initialized
        runtime._hotkeys_registered = original_hotkeys_registered
        runtime._hotkey_registration_error = original_hotkey_registration_error
        runtime._runtime_polling_active = original_runtime_polling_active


def test_restart_hotkeys_logs_registration_success(monkeypatch, tmp_path):
    original_log_path = runtime.notifications.settings.log_path
    original_hotkey_manager = runtime._hotkey_manager
    original_hotkeys_registered = runtime._hotkeys_registered
    original_hotkey_registration_error = runtime._hotkey_registration_error
    registrations = []

    class FakeHotkeys:
        def clear_hotkeys(self):
            registrations.append("cleared")

        def add_hotkey(self, hotkey, callback):
            registrations.append(hotkey)

    try:
        runtime.notifications.configure(
            NotificationSettings(
                ui_mode="silent",
                log_path=tmp_path / "actionflow.log",
            )
        )
        runtime._hotkey_manager = FakeHotkeys()
        runtime.restart_hotkeys()

        assert runtime._hotkeys_registered is True
        assert "ctrl+alt+x" in registrations
        log_text = Path(tmp_path / "actionflow.log").read_text(encoding="utf-8")
        assert "Registering hotkeys" in log_text
        assert "Hotkeys registered successfully" in log_text
    finally:
        runtime.notifications.configure(
            NotificationSettings(
                ui_mode="silent",
                log_path=original_log_path,
            )
        )
        runtime._hotkey_manager = original_hotkey_manager
        runtime._hotkeys_registered = original_hotkeys_registered
        runtime._hotkey_registration_error = original_hotkey_registration_error
