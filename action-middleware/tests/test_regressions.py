"""Regression checks run without live clipboard, GUI, credentials, or network."""
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import main
from actionflow import command_security, history, llm


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(main, '_undo_stack', [])
    monkeypatch.setattr(main, '_current_source_window', 'mac:123')
    monkeypatch.setattr(main, '_chain_suppress_paste', False)
    monkeypatch.setattr(main, 'notify', Mock())
    monkeypatch.setattr(history, 'HISTORY_PATH', tmp_path / 'history.jsonl')
    monkeypatch.setitem(main.CONFIG, 'history', {'log_text': False})


@pytest.mark.parametrize('parts', [
    ['git', '-calias.probe=!echo unsafe', 'probe'], ['git', '-c', 'x=y', 'status'],
    ['git', 'init'], ['git', 'reset', '--hard'], ['git', 'clean', '-fd'],
    ['git', '--exec-path=/tmp', 'status'], ['git', 'log', '--output=/tmp/out'],
    ['git', 'log', '--ext-diff'], ['git', 'log', '-p'],
    ['sort', '-o/tmp/out'], ['sort', '--output=/tmp/out'],
    ['uniq', 'input', 'output'], ['date', '092612002026'], ['hostname', 'new-host'],
    ['python', '-c', 'print(1)'], ['find', '.', '-delete'], ['/bin/ls'],
])
def test_command_policy_rejects_writes_and_exec(parts, monkeypatch):
    runner = Mock()
    monkeypatch.setattr(main, '_run_as_user', runner)
    monkeypatch.setattr(main, '_CMD_ALLOWED_COMMANDS', [*command_security.DEFAULT_ALLOWED, 'python', 'find'])
    import shlex
    main.handle_command(shlex.join(parts), '', {})
    runner.assert_not_called()


@pytest.mark.parametrize('parts', [['ls', '-la'], ['cat', 'notes.txt'], ['git', 'status', '--short'],
                                  ['git', 'log', '--oneline', '--max-count=10'], ['date', '+%F']])
def test_command_policy_uses_trusted_path(parts, monkeypatch):
    monkeypatch.setattr(command_security.shutil, 'which', lambda name, path: '/usr/bin/' + name)
    argv = command_security.validated_argv(parts, command_security.DEFAULT_ALLOWED)
    assert argv[0] == '/usr/bin/' + parts[0]
    if parts[0] == 'git':
        assert '--no-pager' in argv and 'core.fsmonitor=false' in argv


def test_password_and_repeat_never_logged(monkeypatch, capsys):
    monkeypatch.setattr(main, '_replace_selection', Mock())
    monkeypatch.setattr(main.textops, 'generate_password', lambda n: 'SYNTHETIC_SECRET_123!')
    monkeypatch.setitem(main.CONFIG, 'history', {'log_text': True})
    main.dispatch('password', '', 'PW:', {})
    main.dispatch('repeat', '', 'REPEAT:', {})
    assert 'SYNTHETIC_SECRET' not in capsys.readouterr().out
    assert 'SYNTHETIC_SECRET' not in history.HISTORY_PATH.read_text()


def test_private_transform_logs_only_lengths(monkeypatch, capsys):
    monkeypatch.setattr(main, '_replace_selection', Mock())
    main.dispatch('mock', 'private source', 'MOCK: private source', {})
    output = capsys.readouterr().out
    assert 'private source' not in output and 'PrIvAtE' not in output
    assert '[14 chars]' in output
    assert 'private source' not in history.HISTORY_PATH.read_text()


def test_full_undo_stack_does_not_break_chain(monkeypatch):
    monkeypatch.setattr(main, '_undo_stack', [{'original':'old', 'replacement':'new'} for _ in range(20)])
    paste = Mock()
    monkeypatch.setattr(main, '_replace_selection', paste)
    main.route('MOCK:|B64: hello')
    paste.assert_called_once_with('SGVMbE8=', expected_text='MOCK:|B64: hello')
    assert len(main._undo_stack) == 20
    assert main._undo_stack[-1]['original'] == 'MOCK:|B64: hello'


@pytest.fixture
def clipboard(monkeypatch):
    state = {'text':'previous', 'version':1, 'selection':'original', 'window':'mac:123'}
    def copy(value):
        state['text'] = value
        state['version'] += 1
        return True
    monkeypatch.setattr(main, '_IS_MAC', True)
    monkeypatch.setattr(main.time, 'sleep', lambda _: None)
    monkeypatch.setattr(main, '_focus_window', lambda _: True)
    monkeypatch.setattr(main, '_get_active_window_id', lambda: state['window'])
    monkeypatch.setattr(main.mac, 'clipboard_set', copy)
    monkeypatch.setattr(main.mac, 'clipboard_get', lambda: state['text'])
    monkeypatch.setattr(main.mac, 'clipboard_snapshot', lambda: [{'rich':b'original-rich-data'}])
    monkeypatch.setattr(main.mac, 'clipboard_change_count', lambda: state['version'])
    monkeypatch.setattr(main.mac, 'clipboard_restore', Mock())
    monkeypatch.setattr(main.mac, 'capture_selection', lambda: state['selection'])
    monkeypatch.setattr(main.mac, 'wait_for_modifiers_released', lambda **kw: True)
    monkeypatch.setattr(main.mac, 'send_paste', Mock(return_value=True))
    tasks=[]
    class Timer:
        def __init__(self, target, **kw): tasks.append(target)
        def start(self): pass
    monkeypatch.setattr(main.threading, 'Thread', Timer)
    return state, tasks


@pytest.mark.parametrize('failure', ['copy', 'focus', 'selection', 'sync', 'send'])
def test_failed_paste_keeps_text_and_undo(monkeypatch, clipboard, failure):
    state, tasks = clipboard
    if failure == 'copy': monkeypatch.setattr(main.mac, 'clipboard_set', lambda _: False)
    if failure == 'focus': monkeypatch.setattr(main, '_focus_window', lambda _: False)
    if failure == 'selection': state['selection'] = 'different selection'
    if failure == 'sync': monkeypatch.setattr(main, '_wait_for_clipboard_sync', lambda _: False)
    if failure == 'send': main.mac.send_paste.return_value = False
    assert main.dispatch('mock', 'original', 'original', {}) is None
    assert main._undo_stack == []
    if failure != 'send': main.mac.send_paste.assert_not_called()


def test_paste_restores_all_formats_only_if_unchanged(clipboard):
    state, tasks = clipboard
    main._replace_selection('new', expected_text='original')
    main.mac.send_paste.assert_called_once()
    tasks.pop()()
    main.mac.clipboard_restore.assert_called_once_with([{'rich': b'original-rich-data'}])


def test_new_user_copy_survives_restore_timer(clipboard):
    state, tasks = clipboard
    main._replace_selection('new', expected_text='original')
    state['text'] = 'user copy'
    state['version'] += 1
    tasks.pop()()
    main.mac.clipboard_restore.assert_not_called()
    assert state['text'] == 'user copy'


def test_new_operation_invalidates_old_restore_timer(clipboard):
    _, tasks = clipboard
    main._replace_selection('new', expected_text='original')
    main._cancel_pending_clipboard_restore()
    tasks.pop()()
    main.mac.clipboard_restore.assert_not_called()


@pytest.mark.parametrize('selected,window,success', [
    ('after','mac:123',True), ('','mac:123',False), ('wrong','mac:123',False),
    ('after','mac:456',False),
])
def test_undo_requires_explicit_matching_selection(monkeypatch, clipboard, selected, window, success):
    state, _ = clipboard
    state.update(selection=selected, window=window)
    main._undo_stack.append({'original':'before', 'replacement':'after', 'source_window':'mac:123'})
    monkeypatch.setattr(main, '_reset_keyboard_state', lambda: None)
    main._do_undo()
    assert (not main._undo_stack) == success
    assert main.mac.send_paste.call_count == int(success)
    if success: assert state['text'] == 'before'


def test_undo_does_not_race_an_active_job(clipboard):
    main._job_lock.acquire()
    try: main._do_undo()
    finally: main._job_lock.release()
    main.mac.send_paste.assert_not_called()


def test_undo_retained_on_paste_failure(monkeypatch, clipboard):
    state, _ = clipboard
    state['selection'] = 'after'
    main.mac.send_paste.return_value = False
    monkeypatch.setattr(main, '_reset_keyboard_state', lambda: None)
    main._undo_stack.append({'original':'before','replacement':'after','source_window':'mac:123'})
    main._do_undo()
    assert len(main._undo_stack) == 1


def test_fallback_uses_own_endpoint_and_options(monkeypatch):
    cfg = {'provider':'ollama', 'base_url':'http://localhost:1234/v1', 'model':'primary',
           'request_options':{'primary_only':True},
           'fallback':{'provider':'gemini','api_key':'synthetic','model':'backup',
                       'request_options':{'backup_only':True}}}
    monkeypatch.setitem(main.CONFIG, 'llm', cfg)
    monkeypatch.setattr(llm, 'resolve_api_key', lambda *a: 'synthetic')
    import openai
    constructors=[]
    def fake_client(**kw):
        constructors.append(kw)
        return SimpleNamespace(**kw)
    monkeypatch.setattr(openai, 'OpenAI', fake_client)
    for name in ('client','model','provider','ready','MODE','fallback_client','fallback_model',
                 'fallback_provider','fallback_ready'):
        monkeypatch.setattr(llm, name, getattr(llm, name))
    llm.init()
    assert constructors[0]['base_url'] == 'http://localhost:1234/v1'
    assert constructors[1]['base_url'] == llm.PROVIDERS['gemini'].base_url
    assert 'primary_only' not in llm.request_options('gemini', 'backup')
    assert llm.request_options('gemini','backup')['backup_only'] is True
    assert llm._candidates('primary-override')[1][2] == 'backup'
    cfg['fallback']['base_url'] = 'https://backup.invalid/v1'
    llm.init()
    assert constructors[-1]['base_url'] == 'https://backup.invalid/v1'


def test_primary_model_override_not_sent_to_backup(monkeypatch):
    monkeypatch.setattr(llm, 'client', object())
    monkeypatch.setattr(llm, 'provider', 'groq')
    monkeypatch.setattr(llm, 'model', 'primary')
    monkeypatch.setattr(llm, 'fallback_ready', True)
    monkeypatch.setattr(llm, 'fallback_client', object())
    monkeypatch.setattr(llm, 'fallback_provider', 'gemini')
    monkeypatch.setattr(llm, 'fallback_model', 'backup')
    candidates = llm._candidates('command-model')
    assert candidates[0][2] == 'command-model'
    assert candidates[1][2] == 'backup'


def test_focus_change_after_clipboard_copy_stops_paste(monkeypatch, clipboard):
    state, _ = clipboard
    def sync(_):
        state['window'] = 'mac:456'
        return True
    monkeypatch.setattr(main, '_wait_for_clipboard_sync', sync)
    assert main.dispatch('mock', 'original', 'original', {}) is None
    main.mac.send_paste.assert_not_called()
    assert main._undo_stack == []


def test_linux_restore_skips_new_clipboard_text(monkeypatch):
    monkeypatch.setattr(main, '_IS_MAC', False)
    monkeypatch.setattr(main.time, 'sleep', lambda _: None)
    monkeypatch.setattr(main, 'clipboard_paste', lambda **kw: 'new user copy')
    copy = Mock()
    monkeypatch.setattr(main, 'clipboard_copy', copy)
    workers=[]
    class Timer:
        def __init__(self, target, **kw): workers.append(target)
        def start(self): pass
    monkeypatch.setattr(main.threading, 'Thread', Timer)
    main._schedule_clipboard_restore('old', 'generated')
    workers[0]()
    copy.assert_not_called()


def test_stream_provider_errors_do_not_log_payload(monkeypatch, capsys):
    def fail(*args, **kw):
        raise RuntimeError('synthetic-private-payload')
    monkeypatch.setattr(llm, '_create', fail)
    monkeypatch.setattr(llm, 'ready', True)
    monkeypatch.setattr(llm, 'client', object())
    monkeypatch.setattr(llm, 'fallback_ready', False)
    monkeypatch.setattr(llm, 'on_warning', main.TUI.warn)
    with pytest.raises(llm.LLMError): list(llm.stream('synthetic-private-payload'))
    assert 'synthetic-private-payload' not in capsys.readouterr().out


def test_backup_setup_does_not_inherit_primary_endpoint(monkeypatch):
    from actionflow import setup_wizard
    cfg = {'provider':'ollama','base_url':'http://localhost:9999/v1','fallback':{}}
    monkeypatch.setitem(main.CONFIG, 'llm', cfg)
    monkeypatch.setattr(setup_wizard, '_ask', lambda _: 'y')
    monkeypatch.setattr(setup_wizard, '_choose', lambda _: 0)
    configure = Mock(return_value=None)
    monkeypatch.setattr(setup_wizard, '_configure_provider', configure)
    setup_wizard._offer_fallback('ollama')
    assert configure.call_args.kwargs['settings'] == {}


def test_guarded_git_read_only_smoke(tmp_path):
    """Verify fixed safety options against the installed git, in a temporary repo."""
    import subprocess
    import shutil
    git = shutil.which('git', path='/usr/bin:/bin')
    if not git: pytest.skip('git is not installed')
    subprocess.run([git, 'init', '--quiet', str(tmp_path)], check=True, capture_output=True)
    argv = command_security.validated_argv(['git','status','--porcelain'], ['git'])
    assert subprocess.run(argv, cwd=tmp_path, capture_output=True, check=True).stdout == b''
    argv = command_security.validated_argv(['git','log','--oneline','--max-count=1'], ['git'])
    result = subprocess.run(argv, cwd=tmp_path, capture_output=True)
    # An empty repo has no commits; argument parsing must nevertheless succeed.
    assert b'unknown option' not in result.stderr and b'unrecognized argument' not in result.stderr
