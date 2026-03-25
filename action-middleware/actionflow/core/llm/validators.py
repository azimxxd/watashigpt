from __future__ import annotations

import re
from dataclasses import dataclass

from .output_cleaner import detect_script
from ..text_ops import extract_placeholders


class ValidationError(ValueError):
    pass


LANGUAGE_HINTS = {
    "EN": {"label": "English", "scripts": {"latin"}},
    "US": {"label": "English (US)", "scripts": {"latin"}},
    "UK": {"label": "English (UK)", "scripts": {"latin"}},
    "RU": {"label": "Russian", "scripts": {"cyrillic"}},
    "JA": {"label": "Japanese", "scripts": {"japanese", "cjk"}},
    "JP": {"label": "Japanese", "scripts": {"japanese", "cjk"}},
    "ZH": {"label": "Chinese", "scripts": {"cjk"}},
    "CN": {"label": "Chinese", "scripts": {"cjk"}},
}

ROUGH_LANGUAGE_MARKERS = (
    "хуй", "еблан", "долбаеб", "долбоеб", "идиот", "сука", "fuck", "idiot", "stupid", "moron",
)


@dataclass(frozen=True)
class ValidationContext:
    command_name: str
    source_text: str
    args: dict[str, str]


def _ensure_not_empty(output: str) -> None:
    if not output.strip():
        raise ValidationError("Output is empty")


def _tokenize_words(text: str) -> list[str]:
    return re.findall(r"\w+", text.lower(), flags=re.UNICODE)


def _normalized_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


def _word_overlap_ratio(source: str, output: str) -> float:
    source_words = set(_tokenize_words(source))
    output_words = set(_tokenize_words(output))
    if not source_words or not output_words:
        return 0.0
    return len(source_words & output_words) / max(len(source_words), len(output_words))


def _script_counts(text: str) -> dict[str, int]:
    counts = {"latin": 0, "cyrillic": 0, "japanese": 0, "cjk": 0}
    for ch in text:
        if "a" <= ch.lower() <= "z":
            counts["latin"] += 1
        elif "\u0400" <= ch <= "\u04ff":
            counts["cyrillic"] += 1
        elif "\u3040" <= ch <= "\u30ff":
            counts["japanese"] += 1
        elif "\u4e00" <= ch <= "\u9fff":
            counts["cjk"] += 1
    return counts


def _has_unexpected_script_mix(source_text: str, output: str) -> bool:
    source_script = detect_script(source_text)
    counts = _script_counts(output)
    total = sum(counts.values())
    if total == 0 or source_script not in counts:
        return False
    source_share = counts[source_script] / total
    foreign_share = 1.0 - source_share
    return source_share < 0.7 and foreign_share > 0.25


def _is_underwritten_source(text: str) -> bool:
    stripped = text.strip()
    words = _tokenize_words(stripped)
    punctuation = sum(1 for ch in stripped if ch in ".!?,;:")
    return len(words) <= 6 or (len(words) <= 10 and punctuation == 0) or len(stripped) < 40


def validate_translation(output: str, context: ValidationContext) -> None:
    _ensure_not_empty(output)
    target_code = context.args.get("target_code", "").upper()
    hint = LANGUAGE_HINTS.get(target_code)
    if hint:
        script = detect_script(output)
        if script not in hint["scripts"] and script != "other":
            raise ValidationError(f"Output is not in target language {hint['label']}")
    if output.lower().startswith("translation:"):
        raise ValidationError("Translation output contains label text")


def validate_rewrite(output: str, context: ValidationContext) -> None:
    _ensure_not_empty(output)
    source_script = detect_script(context.source_text)
    output_script = detect_script(output)
    if source_script in {"latin", "cyrillic", "japanese"} and output_script not in {source_script, "other", "mixed"}:
        raise ValidationError("Rewrite unexpectedly changed language/script")

    source_norm = re.sub(r"\s+", " ", context.source_text.strip().lower())
    output_norm = re.sub(r"\s+", " ", output.strip().lower())
    source_words = _tokenize_words(context.source_text)
    output_words = _tokenize_words(output)
    source_core = " ".join(source_words)
    output_core = " ".join(output_words)
    underwritten = _is_underwritten_source(context.source_text)

    is_rewrite = context.command_name == "rewrite"

    if source_norm == output_norm:
        raise ValidationError("Rewrite left the text unchanged")

    if is_rewrite and source_core == output_core and underwritten:
        raise ValidationError("Rewrite was only a superficial punctuation/case fix")

    if is_rewrite and underwritten and len(output_words) <= len(source_words) + 1:
        raise ValidationError("Rewrite improvement is too weak for a short or rough input")

    if any(marker in source_norm for marker in ROUGH_LANGUAGE_MARKERS) and source_core == output_core:
        raise ValidationError("Rewrite left rough phrasing unchanged")


def validate_summary(output: str, context: ValidationContext) -> None:
    _ensure_not_empty(output)
    if len(output) >= len(context.source_text.strip()) and len(context.source_text.strip()) > 80:
        raise ValidationError("Summary is not shorter than the source")


def validate_title(output: str, context: ValidationContext) -> None:
    _ensure_not_empty(output)
    if "\n" in output:
        raise ValidationError("Title must be a single line")
    if len(output) > 120:
        raise ValidationError("Title is too long")


def validate_email(output: str, context: ValidationContext) -> None:
    _ensure_not_empty(output)
    normalized = _normalized_text(output)
    if normalized.startswith("sure") or normalized.startswith("here"):
        raise ValidationError("Email output contains assistant preamble")
    if _has_unexpected_script_mix(context.source_text, output):
        raise ValidationError("Email output mixed languages/scripts unexpectedly")
    if len(output.splitlines()) < 2:
        raise ValidationError("Email output is too short to be a usable draft")


def validate_tweet(output: str, context: ValidationContext) -> None:
    _ensure_not_empty(output)
    if len(output) > 280:
        raise ValidationError("Tweet exceeds 280 characters")


def validate_regex(output: str, context: ValidationContext) -> None:
    _ensure_not_empty(output)
    if "\n" in output:
        raise ValidationError("Regex output must be a single line")


def validate_bullets(output: str, context: ValidationContext) -> None:
    _ensure_not_empty(output)
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines or not all(line.lstrip().startswith(("-", "*", "•")) for line in lines):
        raise ValidationError("Bullets output is not a bullet list")


def validate_commit(output: str, context: ValidationContext) -> None:
    _ensure_not_empty(output)
    if "\n" in output:
        raise ValidationError("Commit message must be a single line")
    pattern = re.compile(
        r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)"
        r"(?:\([a-z0-9._/-]+\))?!?:\s+\S.+$",
        re.IGNORECASE,
    )
    if not pattern.match(output.strip()):
        raise ValidationError("Commit message must follow conventional commit format")


def validate_review(output: str, context: ValidationContext) -> None:
    _ensure_not_empty(output)
    lines = [line for line in output.splitlines() if line.strip()]
    if not lines or not all(line.lstrip().startswith(("-", "*", "•")) for line in lines):
        raise ValidationError("Review output must be concise bullet points")


def validate_passthrough(output: str, context: ValidationContext) -> None:
    _ensure_not_empty(output)


def validate_explanation(output: str, context: ValidationContext) -> None:
    _ensure_not_empty(output)
    source_norm = _normalized_text(context.source_text)
    output_norm = _normalized_text(output)
    if source_norm == output_norm:
        raise ValidationError("Explanation repeated the source text")
    overlap = _word_overlap_ratio(context.source_text, output)
    if overlap > 0.88 and abs(len(output_norm) - len(source_norm)) < max(12, len(source_norm) // 5):
        raise ValidationError("Explanation stayed too close to the source wording")
    if len(_tokenize_words(output)) < max(6, min(10, len(_tokenize_words(context.source_text)) + 1)):
        raise ValidationError("Explanation is too thin to be useful")


def validate_haiku(output: str, context: ValidationContext) -> None:
    _ensure_not_empty(output)
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) not in {3, 4}:
        raise ValidationError("Haiku output should be a short multi-line poem")
    if any(len(line) > 60 for line in lines):
        raise ValidationError("Haiku lines are too long")
    if len(" ".join(lines).split()) > 30:
        raise ValidationError("Haiku output is too long")


def validate_fill(output: str, context: ValidationContext) -> None:
    _ensure_not_empty(output)
    placeholders_before = extract_placeholders(context.source_text)
    placeholders_after = extract_placeholders(output)
    if placeholders_before and placeholders_after:
        raise ValidationError("Fill left placeholders unresolved")
    if context.args and output.strip() == context.source_text.strip():
        raise ValidationError("Fill did not apply any substitutions")
