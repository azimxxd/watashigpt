from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .validators import (
    LANGUAGE_HINTS,
    ValidationContext,
    validate_bullets,
    validate_commit,
    validate_explanation,
    validate_email,
    validate_fill,
    validate_passthrough,
    validate_regex,
    validate_rewrite,
    validate_review,
    validate_haiku,
    validate_summary,
    validate_title,
    validate_translation,
    validate_tweet,
)


@dataclass(frozen=True)
class ParsedCommandInput:
    payload: str
    args: dict[str, str]


@dataclass(frozen=True)
class TransformCommandSpec:
    name: str
    parse: Callable[[str], ParsedCommandInput]
    task_description: str
    preserve_language: bool = True
    output_schema: str = "Return only the transformed text."
    single_line_output: bool = False
    retries: int = 1
    default_args: dict[str, str] = field(default_factory=dict)
    validator: Callable[[str, ValidationContext], None] = validate_passthrough


def _parse_plain(text: str) -> ParsedCommandInput:
    return ParsedCommandInput(payload=text.strip(), args={})


_STYLE_RE = re.compile(r"^([^:]+):\s*(.+)$", re.DOTALL)


def _parse_style(text: str) -> ParsedCommandInput:
    match = _STYLE_RE.match(text.strip())
    if not match:
        raise ValueError("Expected <style>: <text>")
    return ParsedCommandInput(payload=match.group(2).strip(), args={"style": match.group(1).strip()})


def _parse_language(text: str) -> ParsedCommandInput:
    match = _STYLE_RE.match(text.strip())
    if not match:
        raise ValueError("Expected <LANG>: <text>")
    code = match.group(1).strip().upper()
    payload = match.group(2).strip()
    label = LANGUAGE_HINTS.get(code, {"label": code}).get("label", code)
    return ParsedCommandInput(payload=payload, args={"target_code": code, "target_label": label})


def _parse_fill(text: str) -> ParsedCommandInput:
    stripped = text.strip()
    if ":" in stripped:
        head, tail = stripped.split(":", 1)
        if "=" in head:
            assignments: dict[str, str] = {}
            for chunk in head.split("|"):
                if "=" not in chunk:
                    continue
                key, value = chunk.split("=", 1)
                assignments[key.strip()] = value.strip()
            return ParsedCommandInput(payload=tail.strip(), args=assignments)
    return ParsedCommandInput(payload=stripped, args={})


COMMAND_SPECS: dict[str, TransformCommandSpec] = {
    "translate": TransformCommandSpec(
        name="translate",
        parse=_parse_language,
        task_description="Translate the source text into the requested target language while preserving meaning and tone.",
        preserve_language=False,
        validator=validate_translation,
    ),
    "trans": TransformCommandSpec(
        name="trans",
        parse=_parse_language,
        task_description="Translate the source text into the requested target language while preserving meaning and tone.",
        preserve_language=False,
        validator=validate_translation,
    ),
    "rewrite": TransformCommandSpec(
        name="rewrite",
        parse=_parse_plain,
        task_description="Rewrite the source text so it is noticeably clearer, smoother, more natural, and better structured while preserving meaning.",
        preserve_language=True,
        output_schema="Return only the rewritten text in the same language as the source. A minimal punctuation-only fix is not enough for short or rough input.",
        retries=2,
        default_args={"strength": "strong"},
        validator=validate_rewrite,
    ),
    "summarize": TransformCommandSpec(
        name="summarize",
        parse=_parse_plain,
        task_description="Summarize the source text concisely.",
        preserve_language=True,
        validator=validate_summary,
    ),
    "explain": TransformCommandSpec(
        name="explain",
        parse=_parse_plain,
        task_description="Explain the source text clearly, simply, and usefully instead of paraphrasing it.",
        preserve_language=True,
        validator=validate_explanation,
    ),
    "tone": TransformCommandSpec(
        name="tone",
        parse=_parse_style,
        task_description="Rewrite the source text in the requested tone without changing the core meaning.",
        preserve_language=True,
        validator=validate_rewrite,
    ),
    "bullets": TransformCommandSpec(
        name="bullets",
        parse=_parse_plain,
        task_description="Convert the source text into a concise bullet list.",
        preserve_language=True,
        validator=validate_bullets,
    ),
    "title": TransformCommandSpec(
        name="title",
        parse=_parse_plain,
        task_description="Generate a short, punchy title for the source text.",
        preserve_language=True,
        single_line_output=True,
        validator=validate_title,
    ),
    "tweet": TransformCommandSpec(
        name="tweet",
        parse=_parse_plain,
        task_description="Compress the source text into a tweet-length version.",
        preserve_language=True,
        validator=validate_tweet,
    ),
    "email": TransformCommandSpec(
        name="email",
        parse=_parse_plain,
        task_description="Turn the source notes into a polished email draft.",
        preserve_language=True,
        output_schema="Return only the email draft in one language. If you add a subject line, keep it in the same language as the body.",
        validator=validate_email,
    ),
    "regex": TransformCommandSpec(
        name="regex",
        parse=_parse_plain,
        task_description="Generate only the regular expression that matches the request.",
        preserve_language=False,
        single_line_output=True,
        validator=validate_regex,
    ),
    "docstring": TransformCommandSpec(
        name="docstring",
        parse=_parse_plain,
        task_description="Generate only the docstring/comment block for the provided code.",
        preserve_language=False,
        validator=validate_passthrough,
    ),
    "review": TransformCommandSpec(
        name="review",
        parse=_parse_plain,
        task_description="Return only a concise code review as bullet points.",
        preserve_language=True,
        validator=validate_review,
    ),
    "gitcommit": TransformCommandSpec(
        name="gitcommit",
        parse=_parse_plain,
        task_description="Generate only the commit message.",
        preserve_language=False,
        single_line_output=True,
        validator=validate_commit,
    ),
    "meeting": TransformCommandSpec(
        name="meeting",
        parse=_parse_plain,
        task_description="Structure the source notes into concise meeting notes.",
        preserve_language=True,
        validator=validate_passthrough,
    ),
    "todo": TransformCommandSpec(
        name="todo",
        parse=_parse_plain,
        task_description="Extract only the actionable items as a checklist.",
        preserve_language=True,
        validator=validate_bullets,
    ),
    "eli5": TransformCommandSpec(
        name="eli5",
        parse=_parse_plain,
        task_description="Explain the source text in very simple language.",
        preserve_language=True,
        validator=validate_passthrough,
    ),
    "haiku": TransformCommandSpec(
        name="haiku",
        parse=_parse_plain,
        task_description="Rewrite the idea as a haiku and return only the haiku.",
        preserve_language=True,
        validator=validate_haiku,
    ),
    "roast": TransformCommandSpec(
        name="roast",
        parse=_parse_plain,
        task_description="Write only a short, light roast of the source text.",
        preserve_language=True,
        validator=validate_passthrough,
    ),
    "fill": TransformCommandSpec(
        name="fill",
        parse=_parse_fill,
        task_description="Fill placeholders in the source text using the provided values or obvious context.",
        preserve_language=True,
        output_schema="Return only the completed text with placeholders resolved. Do not leave the template unchanged if values were provided.",
        validator=validate_fill,
    ),
}
