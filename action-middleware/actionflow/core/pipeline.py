from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .command_router import extract_chain_payload, parse_chain


@dataclass(frozen=True)
class PipelineStep:
    name: str
    config: dict[str, Any]


def run_command_chain(text: str, commands: dict) -> tuple[list[PipelineStep], str] | None:
    chain = parse_chain(text, commands)
    if not chain or len(chain) < 2:
        return None
    return [PipelineStep(name=name, config=config) for name, config in chain], extract_chain_payload(text, commands)


def execute_pipeline(
    text: str,
    steps: list[PipelineStep],
    executor: Callable[[str, str, dict[str, Any]], str],
) -> str:
    result = text
    for step in steps:
        result = executor(step.name, result, step.config)
    return result
