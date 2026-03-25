from __future__ import annotations

import re


_PREAMBLE_PATTERNS = [
    r"^\s*(?:sure|certainly|of course|absolutely)\b[:!,\s-]*",
    r"^\s*(?:here(?:'s| is)\s+)?(?:the\s+)?(?:translated|rewritten|improved|summarized|summary|title|headline|email|regex|docstring|review|commit message|meeting notes|todo list|bullet list|translation|output|result)\b[:!,\s-]*",
    r"^\s*(?:i(?:'m| am)\s+happy\s+to\s+help)\b[:!,\s-]*",
    r"^\s*(?:the\s+translated\s+text\s+is)\b[:!,\s-]*",
]


def strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return stripped


def strip_assistant_boilerplate(text: str) -> str:
    cleaned = strip_code_fences(text).strip().strip('"').strip("'").strip()
    while True:
        updated = cleaned
        for pattern in _PREAMBLE_PATTERNS:
            updated = re.sub(pattern, "", updated, flags=re.IGNORECASE).strip()
        if updated == cleaned:
            break
        cleaned = updated
    return cleaned


def normalize_whitespace(text: str, keep_lines: bool = True) -> str:
    if keep_lines:
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
        return "\n".join(lines).strip()
    return re.sub(r"\s+", " ", text).strip()


def detect_script(text: str) -> str:
    has_cyrillic = any("\u0400" <= ch <= "\u04ff" for ch in text)
    has_hiragana = any("\u3040" <= ch <= "\u309f" for ch in text)
    has_katakana = any("\u30a0" <= ch <= "\u30ff" for ch in text)
    has_han = any("\u4e00" <= ch <= "\u9fff" for ch in text)
    has_latin = any("a" <= ch.lower() <= "z" for ch in text)

    if has_hiragana or has_katakana:
        return "japanese"
    if has_han and not has_latin:
        return "cjk"
    if has_cyrillic and not has_latin:
        return "cyrillic"
    if has_latin and not (has_cyrillic or has_han or has_hiragana or has_katakana):
        return "latin"
    if has_latin and has_cyrillic:
        return "mixed"
    return "other"


def clean_transform_output(text: str, single_line: bool = False) -> str:
    cleaned = strip_assistant_boilerplate(text)
    cleaned = normalize_whitespace(cleaned, keep_lines=not single_line)
    if single_line:
        cleaned = cleaned.splitlines()[0].strip()
    return cleaned
