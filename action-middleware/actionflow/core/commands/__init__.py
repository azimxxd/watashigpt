from .builtin import BUILTIN_COMMANDS
from .external import EXTERNAL_COMMANDS
from .llm import LLM_COMMANDS
from .system import SYSTEM_COMMANDS
from .text import TEXT_COMMANDS

ALL_COMMANDS = {
    **TEXT_COMMANDS,
    **SYSTEM_COMMANDS,
    **EXTERNAL_COMMANDS,
    **LLM_COMMANDS,
}

__all__ = [
    "ALL_COMMANDS",
    "BUILTIN_COMMANDS",
    "EXTERNAL_COMMANDS",
    "LLM_COMMANDS",
    "SYSTEM_COMMANDS",
    "TEXT_COMMANDS",
]
