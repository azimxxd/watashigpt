from .external import EXTERNAL_COMMANDS
from .system import SYSTEM_COMMANDS
from .text import TEXT_COMMANDS

BUILTIN_COMMANDS = {
    **TEXT_COMMANDS,
    **SYSTEM_COMMANDS,
    **EXTERNAL_COMMANDS,
}
