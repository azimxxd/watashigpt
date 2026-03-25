from actionflow.app import runtime


def test_undo_entry_is_committed_only_after_commit_call():
    original_stack = list(runtime._undo_stack)
    original_pending = runtime._pending_undo_entry
    try:
        runtime._undo_stack.clear()
        runtime._pending_undo_entry = None

        runtime._push_undo("before", "after")
        assert runtime._pending_undo_entry == {"original": "before", "replacement": "after"}
        assert runtime._undo_stack == []

        runtime._commit_pending_undo()
        assert runtime._pending_undo_entry is None
        assert runtime._undo_stack[-1] == {"original": "before", "replacement": "after"}
    finally:
        runtime._undo_stack[:] = original_stack
        runtime._pending_undo_entry = original_pending


def test_repeat_targets_last_successful_command():
    original_handlers = dict(runtime._BUILTIN_HANDLERS)
    original_last_command = runtime._last_command
    original_undo_stack = list(runtime._undo_stack)
    original_pending = runtime._pending_undo_entry
    calls: list[tuple[str, str]] = []

    def good_handler(payload: str, full_text: str, cmd_config: dict) -> None:
        calls.append(("good", payload))

    def bad_handler(payload: str, full_text: str, cmd_config: dict) -> None:
        calls.append(("bad", payload))
        raise RuntimeError("boom")

    try:
        runtime._BUILTIN_HANDLERS["good"] = good_handler
        runtime._BUILTIN_HANDLERS["bad"] = bad_handler
        runtime._last_command = None
        runtime._undo_stack.clear()
        runtime._pending_undo_entry = None

        runtime.dispatch("good", "alpha", "alpha", {"prefixes": ["GOOD:"]})
        assert runtime._last_command["name"] == "good"

        try:
            runtime.dispatch("bad", "beta", "beta", {"prefixes": ["BAD:"]})
        except RuntimeError:
            pass

        assert runtime._last_command["name"] == "good"
    finally:
        runtime._BUILTIN_HANDLERS.clear()
        runtime._BUILTIN_HANDLERS.update(original_handlers)
        runtime._last_command = original_last_command
        runtime._undo_stack[:] = original_undo_stack
        runtime._pending_undo_entry = original_pending
