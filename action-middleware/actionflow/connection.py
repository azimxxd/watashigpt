"""GUI setup services: validate in a worker, persist only after UI acceptance."""
from __future__ import annotations

from actionflow import llm
from actionflow.config import CONFIG, save_values


def validate(provider: str, key: str, model: str = '') -> tuple[str, str, str]:
    info = llm.PROVIDERS[provider]
    if not info.local and not key.strip(): raise ValueError('Enter an API key')
    client, model = llm.make_client(provider, key.strip(), model.strip(), settings={'timeout':10})
    try:
        llm.ping(client, provider, model)
    finally:
        client.close()
    return provider, key.strip(), model


def finish(validated: tuple[str, str, str]) -> None:
    provider, key, model = validated
    if key:
        llm.secret_set(f'llm:{provider}', key)
    values = {'provider':provider, 'model':model, 'base_url':'', 'request_options':{}, 'api_key':''}
    save_values('llm', values)
    CONFIG['llm'].update(values)
    # The verified key is used for this session; the persistent copy is in keyring only.
    CONFIG['llm']['api_key'] = key
    llm.init(primary_key=key)
