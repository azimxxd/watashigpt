"""Regression checks without posting real keyboard events or reading messages."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main
from actionflow import platform_mac as mac


def quartz_stub():
    return SimpleNamespace(
        kCGEventKeyDown=10, kCGEventKeyUp=11,
        kCGEventTapDisabledByTimeout=-1, kCGEventTapDisabledByUserInput=-2,
        kCGKeyboardEventKeycode='key', kCGKeyboardEventAutorepeat='repeat',
        CGEventGetIntegerValueField=lambda event, field: event.get(field, 0),
        CGEventGetFlags=lambda event: event['flags'],
        CGEventMaskBit=lambda value: 1 << value,
        kCGSessionEventTap=1, kCGHeadInsertEventTap=0, kCGEventTapOptionDefault=0,
    )


def test_hotkey_swallows_press_repeat_and_release_after_modifiers(monkeypatch):
    q = quartz_stub()
    monkeypatch.setitem(sys.modules, 'Quartz', q)
    calls = []
    monkeypatch.setattr(mac.threading, 'Thread', lambda target, **kw: SimpleNamespace(start=target))
    listener = mac.HotkeyListener()
    listener.add_hotkey('ctrl+alt+x', lambda: calls.append('triggered'))
    down = {'key': mac.KEYCODES['x'], 'flags': mac.FLAG_CTRL | mac.FLAG_ALT}
    assert listener._callback(None, 10, down, None) is None
    repeat = {'key': mac.KEYCODES['x'], 'flags': 0, 'repeat': 1}
    assert listener._callback(None, 10, repeat, None) is None
    assert listener._callback(None, 11, repeat, None) is None
    assert calls == ['triggered']
    # A subsequent ordinary key press must reach the editor.
    plain = {'key': mac.KEYCODES['x'], 'flags': 0}
    assert listener._callback(None, 10, plain, None) is plain


def test_command_x_is_not_actionflow_hotkey(monkeypatch):
    monkeypatch.setitem(sys.modules, 'Quartz', quartz_stub())
    listener = mac.HotkeyListener()
    listener.add_hotkey('ctrl+alt+x', lambda: None)
    cut = {'key': mac.KEYCODES['x'], 'flags': mac.FLAG_CMD}
    assert listener._callback(None, 10, cut, None) is cut


def test_no_listen_only_fallback(monkeypatch):
    q = quartz_stub()
    attempts = []
    q.CGEventTapCreate = lambda *args: attempts.append(args[2])
    monkeypatch.setitem(sys.modules, 'Quartz', q)
    listener = mac.HotkeyListener()
    listener._run()
    assert attempts == [q.kCGEventTapOptionDefault]
    assert listener._tap is None and listener._ready.is_set()


def test_capture_does_not_copy_when_modifiers_still_held(monkeypatch):
    monkeypatch.setattr(mac, 'wait_for_modifiers_released', lambda: False)
    monkeypatch.setattr(mac, 'clipboard_snapshot', lambda: (_ for _ in ()).throw(AssertionError('clipboard touched')))
    monkeypatch.setattr(mac, 'send_copy', lambda: (_ for _ in ()).throw(AssertionError('key posted')))
    assert mac.capture_selection() == ''


def test_permissions_retry_waits_then_starts_only_once(monkeypatch):
    state = {'trusted': False, 'listening': True}
    calls = []
    monkeypatch.setattr(main, 'mac', SimpleNamespace(
        is_accessibility_trusted=lambda: state['trusted'],
        has_input_monitoring=lambda: state['listening']))
    monkeypatch.setattr(main, '_mac_hotkeys', None)
    def start(bindings):
        calls.append(bindings)
        monkeypatch.setattr(main, '_mac_hotkeys', object())
        return True
    monkeypatch.setattr(main, '_start_mac_hotkeys', start)
    assert not main._retry_mac_hotkeys([])
    assert calls == []
    state['trusted'] = True
    assert main._retry_mac_hotkeys([])
    assert main._retry_mac_hotkeys([])
    assert calls == [[]]


def test_open_copied_text_never_captures_or_targets_another_app(monkeypatch):
    import threading
    calls = []
    monkeypatch.setattr(main, '_job_lock', threading.Lock())
    monkeypatch.setattr(main, 'clipboard_paste', lambda: 'Example copied text')
    monkeypatch.setattr(main, '_writing_palette', lambda text, source: calls.append((text, source)))
    main._open_copied_text()
    assert calls == [('Example copied text', None)]
    assert not main._job_lock.locked()
