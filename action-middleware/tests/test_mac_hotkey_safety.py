"""Regression checks without posting real keyboard events or reading messages."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_keys_down_reads_session_key_state(monkeypatch):
    q = SimpleNamespace(
        kCGEventSourceStateCombinedSessionState='session',
        CGEventSourceKeyState=lambda state, code: code == 36,
    )
    monkeypatch.setitem(sys.modules, 'Quartz', q)
    assert mac.keys_down([36])
    assert not mac.keys_down([76])


def test_wait_for_keys_released_pumps_until_released(monkeypatch):
    held = [True, True, False]
    monkeypatch.setattr(mac, 'keys_down', lambda keycodes: held.pop(0))
    pumps = []
    assert mac.wait_for_keys_released([36], pump=lambda t: pumps.append(t))
    assert len(pumps) == 2


def test_wait_for_keys_released_times_out(monkeypatch):
    monkeypatch.setattr(mac, 'keys_down', lambda keycodes: True)
    assert not mac.wait_for_keys_released([36], timeout=0.05)


def test_palette_holds_key_window_until_accept_key_released(monkeypatch):
    mac_ui = pytest.importorskip('actionflow.mac_ui')
    waits = []
    monkeypatch.setattr(mac_ui.platform_mac, 'ACCEPT_KEYS', (36, 76))
    monkeypatch.setattr(mac_ui.platform_mac, 'wait_for_keys_released',
                        lambda keys, timeout, pump: waits.append(keys) or True)
    pumps = []
    monkeypatch.setattr(mac_ui, 'pump', lambda t=0.0: pumps.append(t))
    mac_ui._hold_key_window_until_accept_released()
    assert waits == [(36, 76)]
    assert len(pumps) == 1  # trailing pump consumes the keyUp itself

