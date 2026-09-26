"""Shared privacy rules for history and diagnostic output."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from actionflow.config import CONFIG

SENSITIVE_COMMANDS = frozenset({'password', 'redact', 'command', 'repeat'})
private_output = ContextVar('private_output', default=False)


def text_allowed(command: str) -> bool:
    return command not in SENSITIVE_COMMANDS and bool(
        (CONFIG.get('history') or {}).get('log_text', False))


def log_texts(command: str, source: str, result: str) -> tuple[str, str]:
    if command in SENSITIVE_COMMANDS:
        return f'[{len(source)} chars]', '[REDACTED]'
    if not text_allowed(command):
        return f'[{len(source)} chars]', f'[{len(result)} chars]'
    return source, result


@contextmanager
def command_scope(command: str):
    token = private_output.set(private_output.get() or not text_allowed(command))
    try:
        yield
    finally:
        private_output.reset(token)
