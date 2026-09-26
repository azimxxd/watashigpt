"""The small writing product; legacy commands remain outside the main palette."""
from __future__ import annotations

import copy
import difflib
import re

PRESERVE_RULE = (
    'Preserve meaning, facts, names, numbers, URLs, paragraph breaks and list structure. '
    'Do not invent details or add a preamble. Return only the edited text. '
)
CORE_COMMANDS = {
    'proofread': {'description': 'Fix spelling and grammar; keep your voice',
                  'prefixes': ['FIX:'], 'llm_required': True,
                  'llm_prompt': PRESERVE_RULE + 'Make only necessary spelling, grammar and punctuation corrections.\n\n{text}'},
    'clarify': {'description': 'Make the wording clear and natural',
                'prefixes': ['CLEAR:'], 'llm_required': True,
                'llm_prompt': PRESERVE_RULE + 'Improve clarity without making the text more formal.\n\n{text}'},
    'shorten': {'description': 'Keep the point, remove unnecessary words',
                'prefixes': ['SHORT:'], 'llm_required': True,
                'llm_prompt': PRESERVE_RULE + 'Make the text concise while retaining all essential information.\n\n{text}'},
    'trans': {'description': 'Translate naturally; preserve the meaning',
              'prefixes': ['TRANS:'], 'llm_required': True,
              'llm_prompt': PRESERVE_RULE + 'Translate into {lang}.\n\n{text}'},
}
TOOLS = ('summarize', 'tone', 'polite', 'count', 'redact', 'fmt', 'calc', 'date',
         'b64', 'decode', 'hash', 'escape', 'sanitize', 'password', 'clip', 'stack', 'pop')
DEVELOPER_TOOLS = ('review', 'docstring', 'gitcommit', 'regex')
# Retired external/command-execution features must not run through legacy prefixes either.
DISABLED_COMMANDS = frozenset({'command', 'image', 'wiki', 'define'})


def writing_commands(commands: dict) -> dict:
    """Add new built-ins without rewriting existing user configuration."""
    out = copy.deepcopy(CORE_COMMANDS)
    out.update(commands)
    return out


def change_summary(original: str, result: str) -> str:
    if original == result:
        return 'No changes needed'
    before, after = len(original.split()), len(result.split())
    delta = after - before
    return f'{before} → {after} words' + (f' · {delta:+d}' if delta else '')


def diff_segments(original: str, result: str) -> list[tuple[str, str]]:
    """Word-level diff preserving whitespace; bounded for long selections."""
    if max(len(original), len(result)) > 20000:
        return [('equal', 'Original\n'), ('delete', original), ('equal', '\n\nResult\n'), ('insert', result)]
    a, b = re.findall(r'\s+|[^\s]+', original), re.findall(r'\s+|[^\s]+', result)
    out = []
    for kind, i, j, k, l in difflib.SequenceMatcher(None, a, b).get_opcodes():
        if kind == 'equal': out.append(('equal', ''.join(a[i:j])))
        if kind in ('delete', 'replace'): out.append(('delete', ''.join(a[i:j])))
        if kind in ('insert', 'replace'): out.append(('insert', ''.join(b[k:l])))
    return out
