"""Writing-product flows, persistence, preview safety and onboarding isolation."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import main
from actionflow import preferences, product, palette, prompts, connection, llm, config


@pytest.fixture(autouse=True)
def local_settings(monkeypatch,tmp_path):
    monkeypatch.setattr(preferences,'PATH',tmp_path/'preferences.json')
    monkeypatch.setattr(llm,'ready',True)
    monkeypatch.setattr(llm,'MODE','live')
    monkeypatch.setattr(main,'notify',Mock())
    monkeypatch.setattr(main,'_log_history',Mock())


def controller(text='Private text selected only for this request'):
    commands=main.load_config(config.CONFIG_EXAMPLE_PATH)['commands']
    return palette.PaletteController(text,commands,prompt_for=prompts.prompt_for)


def test_old_config_gets_four_core_commands_without_rewriting(tmp_path):
    path=tmp_path/'config.yaml'
    original='commands:\n  mine:\n    prefixes: [ME:]\n'
    path.write_text(original)
    cfg=config.load_config(path)
    assert set(product.CORE_COMMANDS) <= set(cfg['commands'])
    assert 'mine' in cfg['commands']
    assert path.read_text() == original


def test_home_stays_small_and_extra_packs_are_opt_in():
    ctl=controller()
    assert [r['id'] for r in ctl.items('',None)] == [*product.CORE_COMMANDS,'settings']
    for query in ['image','wiki','command','haiku','roast','base64','review']:
        assert ctl.items(query,None)[0]['id'] == 'custom'
    ctl.activate({'id':'toggle:show_tools'},'')
    ctl.activate({'id':'toggle:developer_tools'},'')
    assert [r['id'] for r in controller().items('',None)][-3:] == ['tools','developer','settings']
    assert any(r['id']=='b64' for r in ctl.items('','tools'))
    assert [r['id'] for r in ctl.items('','developer')] == list(product.DEVELOPER_TOOLS)
    assert not set(product.DISABLED_COMMANDS) & {r['id'] for r in ctl.items('','tools')}


def test_translation_language_survives_restart_and_runs_directly(monkeypatch):
    ctl=controller('Привет')
    ctl.activate({'id':'language:Kazakh'},'')
    ctl=controller('Привет')
    row=next(r for r in ctl.items('',None) if r['id']=='trans')
    assert row['title']=='Translate to Kazakh'
    seen=[]
    monkeypatch.setattr(llm,'stream',lambda prompt, model:seen.append(prompt) or iter(['Сәлем']))
    result=ctl.activate(row,'')
    assert list(result['factory']()) == ['Сәлем']
    assert 'Kazakh' in seen[0] and 'Привет' in seen[0]


def test_invalid_language_does_not_overwrite_preference():
    result=controller().activate({'id':'language:x: malicious'},'')
    assert result['kind']=='message'
    assert preferences.load()['language']=='English'


def test_saved_action_contains_instruction_not_selected_text(monkeypatch):
    ctl=controller('SECRET_SOURCE')
    spec=ctl.activate({'id':'custom','instruction':'Make this friendly','title':'Friendly'},'')
    ctl.save(spec,'Friendly message')
    raw=preferences.PATH.read_text()
    assert 'SECRET_SOURCE' not in raw
    assert 'Make this friendly' in raw
    assert preferences.PATH.stat().st_mode & 0o777 == 0o600
    restarted=controller('DIFFERENT_SOURCE')
    item=next(r for r in restarted.items('',None) if r['id'].startswith('saved:'))
    seen=[]
    monkeypatch.setattr(llm,'stream',lambda prompt,model:seen.append(prompt) or iter(['done']))
    list(restarted.activate(item,'')['factory']())
    assert 'DIFFERENT_SOURCE' in seen[0] and 'SECRET_SOURCE' not in seen[0]
    action_id=preferences.load()['saved_actions'][0]['id']
    preferences.remove_action(action_id)
    assert not preferences.load()['saved_actions']


def test_duplicate_instruction_renames_instead_of_duplicating():
    a=preferences.save_action('Short','Make it short')
    b=preferences.save_action('Shorter','Make it short')
    assert a['id']==b['id']
    assert len(preferences.load()['saved_actions'])==1
    assert preferences.load()['saved_actions'][0]['name']=='Shorter'


def test_refinement_cannot_be_saved_as_standalone_action():
    ctl=controller()
    spec=ctl.refine('A result','Shorter')
    with pytest.raises(ValueError): ctl.save(spec,'Shorter')
    assert not preferences.PATH.exists()


def test_local_metrics_are_opt_in_fixed_counters_only():
    preferences.record('opened')
    assert not preferences.PATH.exists()
    preferences.update(metrics_enabled=True)
    preferences.record('generated','customer@example.com')
    preferences.record('accepted')
    preferences.record('SECRET_UNKNOWN_EVENT')
    raw=preferences.PATH.read_text()
    assert 'customer@example.com' not in raw and 'SECRET_UNKNOWN_EVENT' not in raw
    assert preferences.load()['metrics']=={'generated':1,'tool':1,'accepted':1}
    preferences.update(metrics_enabled=False)
    preferences.record('opened')
    assert 'opened' not in preferences.load()['metrics']


def test_corrupt_preferences_fall_back_without_breaking_palette():
    preferences.PATH.write_text('not json')
    assert controller().preferences['language']=='English'
    preferences.PATH.write_text(json.dumps({'saved_actions':[None,{'id':3}],'metrics':['bad']}))
    assert controller().preferences['saved_actions']==[]


@pytest.mark.parametrize('original,result',[
    ('I has a plan.','I have a plan.'),('Привет, мир!','Привет, друг!'),
    ('Line 1\n\n- A\n- B','Line 1\n\n- B'),('same','same'),('','new'),('old',''),
    ('x'*21000,'y'*21000),
])
def test_diff_preserves_both_texts(original,result):
    segments=product.diff_segments(original,result)
    if len(original)>20000:
        assert ('delete',original) in segments and ('insert',result) in segments
    else:
        assert ''.join(t for k,t in segments if k!='insert')==original
        assert ''.join(t for k,t in segments if k!='delete')==result


@pytest.mark.parametrize('command',list(product.CORE_COMMANDS))
def test_core_prompts_preserve_facts_and_formatting(command):
    text='RU: Иван: 125 тенге\nhttps://example.com' if command=='trans' else 'Иван: 125 тенге\nhttps://example.com'
    prompt,_=prompts.prompt_for(command,product.CORE_COMMANDS[command],text)
    for term in ['names','numbers','URLs','paragraph','125','https://example.com']:
        assert term in prompt


@pytest.mark.parametrize('name',sorted(product.DISABLED_COMMANDS))
def test_retired_commands_do_not_execute_even_with_old_config(monkeypatch,name):
    handler=Mock()
    monkeypatch.setitem(main._BUILTIN_HANDLERS,name,handler)
    assert main.dispatch(name,'payload','PREFIX: payload',{}) is None
    handler.assert_not_called()


def test_connection_validation_does_not_save_anything(monkeypatch):
    client=Mock()
    monkeypatch.setattr(llm,'make_client',Mock(return_value=(client,'model')))
    monkeypatch.setattr(llm,'ping',Mock())
    save=Mock()
    monkeypatch.setattr(connection,'save_values',save)
    secret=Mock()
    monkeypatch.setattr(llm,'secret_set',secret)
    assert connection.validate('groq','test-key') == ('groq','test-key','model')
    client.close.assert_called_once()
    save.assert_not_called()
    secret.assert_not_called()


def test_connection_persists_no_plaintext_key(monkeypatch):
    secret=Mock()
    save=Mock()
    init=Mock()
    monkeypatch.setattr(llm,'secret_set',secret)
    monkeypatch.setattr(connection,'save_values',save)
    monkeypatch.setattr(llm,'init',init)
    monkeypatch.setitem(config.CONFIG,'llm',{})
    connection.finish(('groq','test-key','model'))
    assert 'test-key' not in str(save.call_args)
    secret.assert_called_once_with('llm:groq','test-key')
    init.assert_called_once_with(primary_key='test-key')


def test_complex_provider_options_can_be_reset_safely(monkeypatch,tmp_path):
    path=tmp_path/'config.yaml'
    path.write_text('llm:\n  request_options:\n    old: true\n  provider: groq\n')
    monkeypatch.setattr(config,'CONFIG_PATH',path)
    config.save_values('llm',{'request_options':{},'provider':'gemini'})
    import yaml
    data=yaml.safe_load(path.read_text())
    assert data['llm']['request_options']=={} and data['llm']['provider']=='gemini'


def test_demo_never_injects_paste_or_runs_tools(monkeypatch):
    monkeypatch.setattr(main,'_NATIVE_UI',True)
    fake=SimpleNamespace(run=lambda:{'kind':'replace','item':{'id':'proofread'},'text':'demo','seconds':1},
                         preview_buttons=[Mock() for _ in range(6)],_stream_spec={'cmd_name':'proofread'})
    monkeypatch.setattr(main,'mac_ui',SimpleNamespace(CommandPalette=lambda *a,**k:fake),raising=False)
    commit=Mock()
    dispatch=Mock()
    monkeypatch.setattr(main,'_commit_generated',commit)
    monkeypatch.setattr(main,'dispatch',dispatch)
    main._writing_palette('sample',None,demo=True)
    commit.assert_not_called()
    dispatch.assert_not_called()


def test_failed_commit_keeps_generated_text_available(monkeypatch):
    monkeypatch.setattr(main,'_NATIVE_UI',True)
    fake=SimpleNamespace(run=lambda:{'kind':'replace','item':{'id':'proofread'},'text':'retained result','seconds':1},
                         _stream_spec={'cmd_name':'proofread'})
    monkeypatch.setattr(main,'mac_ui',SimpleNamespace(CommandPalette=lambda *a,**k:fake),raising=False)
    monkeypatch.setattr(main,'_commit_generated',Mock(side_effect=RuntimeError('focus changed')))
    import queue
    results=queue.Queue()
    monkeypatch.setattr(main,'_result_queue',results)
    main._writing_palette('sample','mac:123')
    assert results.get_nowait()[1]=='retained result'


def test_practice_does_not_inflate_usage_metrics():
    preferences.update(metrics_enabled=True)
    ctl=controller()
    ctl.demo=True
    ctl.record('generated','proofread')
    assert preferences.load()['metrics']=={}


@pytest.fixture
def native_ui(monkeypatch):
    if getattr(main,'mac_ui',None) is None: pytest.skip('AppKit is unavailable')
    ui=main.mac_ui.CommandPalette(controller('I has a plan.'),context='Test',status='Test')
    yield ui
    ui.panel.close()


def _complete(ui,result='I have a plan.'):
    ui._stream_spec={'cmd_name':'proofread','cmd_config':{}}
    ui._stream_item={'id':'proofread'}
    ui._enter_preview({'title':'Fix mistakes'})
    ui._ui_queue.put(('chunk',ui._stream_token,result))
    ui._ui_queue.put(('done',ui._stream_token,None))
    ui._drain_ui_queue()


def test_native_preview_keeps_views_separate_from_replacement(native_ui):
    ui=native_ui
    _complete(ui)
    ui.preview_action(2)
    assert str(ui.text_view.string())=='I has a plan.'
    ui.preview_action(5)
    assert ui.outcome['text']=='I have a plan.'


def test_native_copy_uses_result_even_when_original_is_shown(native_ui,monkeypatch):
    ui=native_ui
    _complete(ui)
    ui.preview_action(2)
    board=Mock()
    monkeypatch.setattr(main.mac_ui,'NSPasteboard',SimpleNamespace(generalPasteboard=lambda:board))
    ui.preview_action(4)
    assert board.setString_forType_.call_args.args[0]=='I have a plan.'
    assert ui.outcome['kind']=='copied'


def test_native_partial_failure_cannot_be_accepted(native_ui):
    ui=native_ui
    ui._enter_preview({'title':'Fix mistakes'})
    ui._stream_spec={'cmd_name':'proofread'}
    ui._ui_queue.put(('chunk',ui._stream_token,'Incomplete'))
    ui._ui_queue.put(('error',ui._stream_token,'synthetic failure'))
    ui._drain_ui_queue()
    ui.preview_action(5)
    assert not ui._stream_text and ui.outcome is None
    assert 'Incomplete' not in str(ui.text_view.string())
    assert not ui.preview_buttons[5].isEnabled()


def test_native_cancel_ignores_late_connection_result(native_ui,monkeypatch):
    ui=native_ui
    finish=Mock()
    monkeypatch.setattr(connection,'finish',finish)
    old_token=ui._stream_token
    ui._back_to_list()
    ui._ui_queue.put(('connected',old_token,('groq','synthetic-key','model')))
    ui._drain_ui_queue()
    finish.assert_not_called()


def test_native_empty_response_has_no_replace_action(native_ui):
    _complete(native_ui,'')
    native_ui.preview_action(5)
    assert native_ui.outcome is None and not native_ui.preview_buttons[5].isEnabled()
