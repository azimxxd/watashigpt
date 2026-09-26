"""Small, fail-closed command grammar. Config may narrow it, never bypass it."""
from __future__ import annotations

import re
import shutil

# Only flags with no filesystem mutation or subprocess execution are accepted.
_FLAGS = {
    'ls': {'-a', '-l', '-h', '-la', '-al', '-lh', '-lah', '-A', '-R', '-1'},
    'cat': {'-n', '-b', '-s'}, 'grep': {'-i', '-n', '-v', '-w', '-F', '-E', '-l', '-c'},
    'echo': set(), 'wc': {'-l', '-w', '-c', '-m'},
    'head': set(), 'tail': set(), 'sort': {'-n', '-r', '-u', '-f'},
    'uniq': {'-c', '-d', '-u'}, 'diff': {'-u', '-q', '-r', '-w'},
    'file': {'-b', '-i'}, 'stat': set(), 'whoami': set(),
    'hostname': set(), 'uname': {'-a', '-s', '-r', '-m', '-n'},
    'which': {'-a'}, 'pwd': {'-L', '-P'}, 'df': {'-h', '-k'},
    'du': {'-h', '-s', '-k'}, 'uptime': set(), 'cal': set(), 'date': {'-u'},
}
DEFAULT_ALLOWED = [*_FLAGS, 'git']


def validated_argv(parts: list[str], allowed: list[str]) -> list[str]:
    """Validate before resolving an executable from trusted system directories."""
    if not parts or parts[0] not in allowed or '/' in parts[0]:
        raise ValueError('Command is not allowed')
    name, args = parts[0], parts[1:]
    if name == 'git':
        if not args or args[0] not in {'status', 'log'}:
            raise ValueError('Git supports only status and log')
        permitted = ({'--short', '-s', '--branch', '-b', '--porcelain'} if args[0] == 'status'
                     else {'--oneline', '--all', '--decorate', '--no-decorate'})
        if any(a not in permitted and not (args[0] == 'log' and
                   re.fullmatch(r'--max-count=\d{1,4}', a)) for a in args[1:]):
            raise ValueError('Git argument is not allowed')
        if args[0] == 'log':
            args = [args[0], '--no-patch', '--no-ext-diff', '--no-textconv', *args[1:]]
        args = ['--no-pager', '--no-optional-locks', '-c', 'core.fsmonitor=false',
                '-c', 'log.showSignature=false', *args]
    else:
        if name not in _FLAGS:
            raise ValueError('No safe argument policy for this command')
        if name in {'hostname', 'whoami', 'pwd', 'uptime', 'uname'} and any(
                a not in _FLAGS[name] for a in args):
            raise ValueError('Arguments are not allowed for this command')
        if name == 'date' and any(a != '-u' and not a.startswith('+') for a in args):
            raise ValueError('Date only supports display formatting and -u')
        if name == 'uniq' and len([a for a in args if not a.startswith('-')]) > 1:
            raise ValueError('uniq output files are not allowed')
        for a in args:
            if a.startswith('-') and a not in _FLAGS[name]:
                # Even after -- reject option-looking operands; no alternate parsers.
                raise ValueError('Command argument is not allowed')
    binary = shutil.which(name, path='/usr/bin:/bin:/usr/sbin:/sbin')
    if not binary:
        raise ValueError('System command is unavailable')
    return [binary, *args]
