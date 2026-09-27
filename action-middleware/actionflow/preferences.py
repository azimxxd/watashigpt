"""Atomic local preferences. Never stores selected text, results or API keys."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import uuid


def _home() -> Path:
    user = os.environ.get('SUDO_USER')
    return Path(os.path.expanduser('~' + user)) if user else Path.home()

PATH = _home() / '.actionflow_preferences.json'
_LOCK = threading.RLock()
DEFAULTS = {'language': 'English', 'last_action': '', 'show_tools': False,
            'developer_tools': False, 'metrics_enabled': False,
            'saved_actions': [], 'metrics': {}, 'onboarded': False}


def load() -> dict:
    with _LOCK:
        data = copy.deepcopy(DEFAULTS)
        try:
            raw = json.loads(PATH.read_text(encoding='utf-8'))
            if not isinstance(raw, dict): return data
            for k in ('language', 'last_action'):
                if isinstance(raw.get(k), str): data[k] = raw[k]
            for k in ('show_tools', 'developer_tools', 'metrics_enabled', 'onboarded'):
                if isinstance(raw.get(k), bool): data[k] = raw[k]
            saved = raw.get('saved_actions', [])
            if isinstance(saved, list):
                data['saved_actions'] = [x for x in saved if isinstance(x, dict) and
                    all(isinstance(x.get(k), str) for k in ('id','name','instruction'))][:20]
            metrics = raw.get('metrics', {})
            if isinstance(metrics, dict):
                data['metrics'] = {k:v for k,v in metrics.items() if k in EVENTS and isinstance(v,int) and v >= 0}
        except (OSError, ValueError):
            pass
        return data


def _write(data: dict) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.actionflow-', dir=PATH.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, PATH)
    finally:
        if os.path.exists(name): os.unlink(name)


def update(**values) -> dict:
    if not set(values) <= set(DEFAULTS): raise ValueError('Unknown preference')
    with _LOCK:
        data = load()
        data.update(values)
        _write(data)
        return data


def save_action(name: str, instruction: str, action_id: str | None = None) -> dict:
    name, instruction = name.strip(), instruction.strip()
    if not name or len(name) > 60 or not instruction or len(instruction) > 2000:
        raise ValueError('Use a name up to 60 characters and an instruction up to 2000 characters')
    with _LOCK:
        data = load()
        if action_id is not None:
            existing = next((x for x in data['saved_actions'] if x['id'] == action_id), None)
            if existing is None:
                raise ValueError('This command no longer exists. Create a new command instead.')
        else:
            existing = next((x for x in data['saved_actions'] if x['instruction'] == instruction), None)
        if existing:
            existing['name'] = name
            existing['instruction'] = instruction
            item = existing
        else:
            if len(data['saved_actions']) >= 20: raise ValueError('Remove a saved action first (maximum 20)')
            item = {'id':uuid.uuid4().hex, 'name':name, 'instruction':instruction}
            data['saved_actions'].append(item)
        _write(data)
        return item


def remove_action(action_id: str) -> None:
    with _LOCK:
        data = load()
        data['saved_actions'] = [x for x in data['saved_actions'] if x['id'] != action_id]
        _write(data)


# Fixed aggregate counters only: no text, app names, custom titles, or timestamps.
EVENTS = frozenset({'opened','generated','accepted','copied','discarded','failed',
                   'proofread','clarify','shorten','trans','custom','tool'})


def record(event: str, action: str = '') -> None:
    if event not in EVENTS: return
    try:
        with _LOCK:
            data = load()
            if not data['metrics_enabled']: return
            for key in [event] + ([action if action in {'proofread','clarify','shorten','trans','custom'} else 'tool']
                                  if event == 'generated' and action else []):
                data['metrics'][key] = data['metrics'].get(key, 0) + 1
            _write(data)
    except OSError:
        pass  # Optional metrics must never interrupt editing.
