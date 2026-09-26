"""Prompt construction for every LLM-backed command (one place to edit)."""

from __future__ import annotations

import re

TONE_STYLE_RE = re.compile(r"^([a-zA-Z]+):\s*")
# "JP: text", "Kazakh: text", "Brazilian Portuguese: text", "казахский: text"
TRANS_LANG_RE = re.compile(r"^([^\W\d_](?:[^\W\d_]|[ -]){1,29}?):\s*")

CUSTOM_INSTRUCTION_PROMPT = (
    "{instruction}\n\n"
    "Apply the instruction above to the text below. Reply with ONLY the resulting "
    "text — no preamble, no quotes, no explanations. Keep the original language "
    "unless the instruction asks for another one.\n\nText:\n{text}"
)


def context_vars(text: str, ta=None, app_ctx=None) -> dict:
    """Variables available to config.yaml prompt templates."""
    fmt_vars = {"text": text.strip()}
    if ta:
        fmt_vars["code_language"] = ta.code_language or "unknown"
        fmt_vars["looks_like"] = ta.looks_like
        fmt_vars["language"] = ta.language
        fmt_vars["is_code"] = str(ta.is_code)
        ctx_parts = []
        if ta.is_code:
            ctx_parts.append(f"code ({ta.code_language or 'unknown language'})")
        if not ta.is_formal:
            ctx_parts.append("informal tone")
        ctx_parts.append(f"looks like: {ta.looks_like}")
        fmt_vars["context"] = ", ".join(ctx_parts)
    else:
        fmt_vars.update(context="general text", code_language="unknown",
                        looks_like="prose", language="en", is_code="False")
    fmt_vars["app_context"] = app_ctx.context_type if app_ctx else "unknown"
    return fmt_vars


# Prompts are English, and models answer in the prompt's language unless told
# otherwise — so a Russian selection came back summarized in English.
LANGUAGE_RULE = ("Write your answer in the same language as the text you are given "
                 "(Russian text → Russian answer, Kazakh → Kazakh, English → English).\n\n")

# Commands whose output language is decided by the command itself
_OWN_LANGUAGE = frozenset({"trans", "custom", "gitcommit"})


def prompt_for(cmd_name: str, cmd_config: dict, text: str,
               variables: dict | None = None) -> tuple[str, str]:
    """(prompt, model) for any LLM-backed command, answering in the text's language."""
    prompt, model = _build(cmd_name, cmd_config, text, variables)
    if cmd_name not in _OWN_LANGUAGE:
        prompt = LANGUAGE_RULE + prompt
    return prompt, model


def _build(cmd_name: str, cmd_config: dict, text: str,
           variables: dict | None = None) -> tuple[str, str]:
    """(prompt, model) for any LLM-backed command.

    `text` is the command payload (for TONE "style: text", for TRANS
    "LANG: text"). Raises ValueError with a user-facing message on bad input.
    """
    model = cmd_config.get("model", "")
    body = text.strip()

    if cmd_name == "custom":
        instruction = (cmd_config.get("instruction") or "").strip()
        if not instruction:
            raise ValueError("Type an instruction first")
        return (CUSTOM_INSTRUCTION_PROMPT.replace("{instruction}", instruction)
                .replace("{text}", body), model)

    if cmd_config.get("_personal"):
        parts = [f"Task: {cmd_config['description']}", ""] if cmd_config.get("description") else []
        for ex in cmd_config.get("examples", []):
            parts += [f"Input: {ex['input']}", f"Output: {ex['output']}", ""]
        parts += [f"Input: {body}", "Output:"]
        return "\n".join(parts), model

    if cmd_name == "tone":
        m = TONE_STYLE_RE.match(text)
        if not m or not text[m.end():].strip():
            raise ValueError("TONE needs a style and text, e.g. TONE:casual: hello")
        return (f"Rewrite the following text in a {m.group(1).lower()} tone. "
                f"Return ONLY the rewritten text, nothing else:\n\n{text[m.end():].strip()}", model)

    if cmd_name == "trans":
        m = TRANS_LANG_RE.match(text)
        if not m or not text[m.end():].strip():
            raise ValueError("TRANS needs a language and text, e.g. TRANS:JP: hello")
        template = cmd_config.get("llm_prompt", "Translate to {lang}: {text}")
        return format_prompt(template, {"lang": m.group(1).strip(),
                                         "text": text[m.end():].strip()}), model

    if cmd_name == "polite":
        return ("Rewrite the following text to be polite and professional. "
                "Keep the same meaning but make it appropriate for a workplace. "
                f"Return ONLY the rewritten text, nothing else:\n\n{body}", model)

    template = cmd_config.get("llm_prompt", "Process this text: {text}")
    return format_prompt(template, variables or context_vars(text)), model


class _KeepMissing(dict):
    """format_map helper: unknown {placeholders} are left as-is."""
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def format_prompt(template: str, variables: dict) -> str:
    """Fill a user-supplied prompt template without ever raising.

    Unknown placeholders stay literal; malformed templates (stray braces,
    positional fields) fall back to plain {text} substitution.
    """
    try:
        return template.format_map(_KeepMissing(variables))
    except (ValueError, IndexError, AttributeError):
        result = template
        for key, value in variables.items():
            result = result.replace("{" + key + "}", str(value))
        return result
