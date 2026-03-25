from actionflow.core.clipboard_stack import ClipboardStack, NamedClipsStore


def test_clipboard_stack_push_pop():
    stack = ClipboardStack(max_items=2)
    assert stack.push("a") == 1
    assert stack.push("b") == 2
    item, depth = stack.pop()
    assert item == "b"
    assert depth == 1


def test_clipboard_stack_is_lifo_and_can_clear():
    stack = ClipboardStack(max_items=3)
    stack.push("first")
    stack.push("second")
    assert stack.peek() == "second"
    assert stack.pop() == ("second", 1)
    stack.clear()
    assert len(stack) == 0


def test_named_clip_store_round_trip_and_list(tmp_path):
    store = NamedClipsStore(tmp_path / "clips.json", max_clips=3)
    store.save_clip("draft", "hello")
    store.save_clip("ideas", "ship it")
    assert store.get_clip("draft") == "hello"
    assert [name for name, _ in store.list_clips()] == ["draft", "ideas"]


def test_named_clip_store_delete_and_clear(tmp_path):
    store = NamedClipsStore(tmp_path / "clips.json", max_clips=3)
    store.save_clip("draft", "hello")
    assert store.delete_clip("draft") is True
    assert store.load() == {}
    store.save_clip("a", "1")
    store.save_clip("b", "2")
    assert store.clear() == 2
    assert store.load() == {}
