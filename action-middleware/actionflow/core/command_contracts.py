from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CommandContract:
    name: str
    purpose: str
    expected_input: str
    expected_output_schema: str
    language_behavior: str
    validation_rules: tuple[str, ...]
    error_behavior: str


COMMAND_CONTRACTS: dict[str, CommandContract] = {
    "count": CommandContract(
        name="count",
        purpose="Return realistic text statistics and reading time.",
        expected_input="Any plain text.",
        expected_output_schema="Single-line stats string with words, chars, lines, and reading time.",
        language_behavior="Language-agnostic; it measures structure only.",
        validation_rules=(
            "Reading time is derived from word count.",
            "Short text uses seconds or < 1 min instead of rounding up to one minute.",
        ),
        error_behavior="Empty text still returns a valid zero-ish stats line.",
    ),
    "escape": CommandContract(
        name="escape",
        purpose="Escape text predictably for code, regex, HTML, shell, or SQL contexts.",
        expected_input="Optional mode prefix followed by raw text, for example json: hello \"world\".",
        expected_output_schema="Escaped text only, with no commentary.",
        language_behavior="Preserves content language and only escapes control characters.",
        validation_rules=(
            "Default mode is JSON-style string escaping.",
            "Explicit modes are html, regex, json, python, shell, and sql.",
        ),
        error_behavior="Unsupported modes fail with a clear usage error.",
    ),
    "clip": CommandContract(
        name="clip",
        purpose="Manage internal named text snippets.",
        expected_input="clip:save:name:text, clip:load:name, clip:list, clip:delete:name, or clip:clear.",
        expected_output_schema="Save/delete/list are display-only. Load returns the stored text.",
        language_behavior="Stored text is preserved exactly as written.",
        validation_rules=(
            "Clip names allow only letters, numbers, dash, and underscore.",
            "Load fails clearly when a clip does not exist.",
        ),
        error_behavior="Invalid syntax or names return explicit usage errors.",
    ),
    "stack": CommandContract(
        name="stack",
        purpose="Push text onto an internal LIFO stack.",
        expected_input="STACK:<text> or PUSH:<text>.",
        expected_output_schema="Display-only confirmation with current depth.",
        language_behavior="Stored text is preserved exactly as written.",
        validation_rules=(
            "Push appends a new item.",
            "POP returns the most recently pushed item.",
        ),
        error_behavior="Overflow and empty-pop errors are explicit.",
    ),
    "command": CommandContract(
        name="command",
        purpose="Run an allowlisted system command and show its real output.",
        expected_input="A single allowlisted command such as echo hello, dir, or ls.",
        expected_output_schema="Real stdout, or stderr if stdout is empty.",
        language_behavior="Language-agnostic; passes through system command output.",
        validation_rules=(
            "Only allowlisted binaries are permitted.",
            "Pipes, redirects, and chained shell operators are blocked.",
        ),
        error_behavior="Invalid syntax, blocked commands, and timeouts return explicit errors.",
    ),
    "wiki": CommandContract(
        name="wiki",
        purpose="Return a useful Wikipedia summary or a clean list of likely meanings.",
        expected_input="A topic or term.",
        expected_output_schema="Either a summary block or a short list of disambiguation options.",
        language_behavior="Returns Wikipedia content as provided by the API.",
        validation_rules=(
            "Disambiguation pages must not be shown as raw useless summaries.",
            "When safe, the most probable meaning is preferred.",
        ),
        error_behavior="Not-found and HTTP failures return clean lookup errors.",
    ),
    "define": CommandContract(
        name="define",
        purpose="Return safe, useful dictionary definitions.",
        expected_input="A single word or short term.",
        expected_output_schema="One primary definition plus a short list of alternatives when helpful.",
        language_behavior="English dictionary lookup output.",
        validation_rules=(
            "Offensive, vulgar, or slang-first meanings are filtered by default.",
            "Safe, common meanings are ranked first.",
        ),
        error_behavior="Missing entries return a clean not-found message.",
    ),
    "explain": CommandContract(
        name="explain",
        purpose="Explain the source text more clearly than the original.",
        expected_input="A concept, sentence, paragraph, or code snippet.",
        expected_output_schema="A concise explanation only.",
        language_behavior="Keeps the source language unless a command explicitly requests another one.",
        validation_rules=(
            "Output must explain, not merely echo or paraphrase the source.",
            "Complex input should be simplified into clearer wording.",
        ),
        error_behavior="If the first result is too close to the input, the command retries.",
    ),
    "email": CommandContract(
        name="email",
        purpose="Turn rough notes into a polished single-language email draft.",
        expected_input="Notes, bullets, or a short request.",
        expected_output_schema="Email draft only, optionally with a same-language subject line.",
        language_behavior="The entire draft stays in the input language unless explicitly overridden.",
        validation_rules=(
            "No assistant preamble.",
            "No mixed-language output.",
        ),
        error_behavior="Mixed-language or malformed drafts trigger validation failure and retry.",
    ),
    "haiku": CommandContract(
        name="haiku",
        purpose="Convert an idea into a short haiku-style poem.",
        expected_input="A theme, sentence, or paragraph.",
        expected_output_schema="Short poem only, typically three lines.",
        language_behavior="Keeps the source language when possible.",
        validation_rules=(
            "The result should look like a short poem.",
            "No commentary before or after the poem.",
        ),
        error_behavior="Non-poetic or overlong outputs trigger validation failure and retry.",
    ),
    "fill": CommandContract(
        name="fill",
        purpose="Fill template placeholders with explicit values or obvious context.",
        expected_input="Assignments plus a template, for example name=Aldiyar: Hello {{name}}.",
        expected_output_schema="Completed template text only.",
        language_behavior="Preserves the template language.",
        validation_rules=(
            "Explicit assignments are applied deterministically.",
            "Unresolved placeholders count as an error when the command had enough information.",
        ),
        error_behavior="If placeholders are not actually filled, the command fails instead of pretending success.",
    ),
}
