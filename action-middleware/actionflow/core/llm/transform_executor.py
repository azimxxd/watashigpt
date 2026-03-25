from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .command_specs import COMMAND_SPECS, ParsedCommandInput
from .output_cleaner import clean_transform_output
from .prompt_builder import build_prompt, build_retry_prompt
from .validators import ValidationContext, ValidationError
from ..text_ops import fill_placeholders


class TransformExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class TransformResult:
    output: str
    attempts: int


def execute_transform_command(
    command_name: str,
    raw_payload: str,
    llm_call: Callable[[str], str],
    extra_args: dict[str, str] | None = None,
) -> TransformResult:
    if command_name not in COMMAND_SPECS:
        raise TransformExecutionError(f"Unknown transform command: {command_name}")

    spec = COMMAND_SPECS[command_name]
    try:
        parsed: ParsedCommandInput = spec.parse(raw_payload)
    except Exception as exc:
        raise TransformExecutionError(str(exc)) from exc

    merged_args = dict(spec.default_args)
    merged_args.update(parsed.args)
    if extra_args:
        merged_args.update({k: v for k, v in extra_args.items() if v is not None and v != ""})
    parsed = ParsedCommandInput(payload=parsed.payload, args=merged_args)

    if not parsed.payload.strip():
        raise TransformExecutionError("Command payload is empty")

    validation_context = ValidationContext(command_name=command_name, source_text=parsed.payload, args=parsed.args)

    if command_name == "fill" and parsed.args:
        filled = fill_placeholders(parsed.payload, parsed.args)
        try:
            spec.validator(filled, validation_context)
        except ValidationError as exc:
            raise TransformExecutionError(str(exc)) from exc
        return TransformResult(output=filled, attempts=1)

    prompt = build_prompt(spec, parsed)

    attempts = spec.retries + 1
    last_error: str | None = None
    for attempt in range(attempts):
        output = llm_call(prompt)
        cleaned = clean_transform_output(output, single_line=spec.single_line_output)
        try:
            spec.validator(cleaned, validation_context)
            return TransformResult(output=cleaned, attempts=attempt + 1)
        except ValidationError as exc:
            last_error = str(exc)
            if attempt >= attempts - 1:
                break
            prompt = build_retry_prompt(spec, parsed, cleaned, str(exc))

    raise TransformExecutionError(last_error or "Transform validation failed")
