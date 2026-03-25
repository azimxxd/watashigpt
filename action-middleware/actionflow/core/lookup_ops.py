from __future__ import annotations

from dataclasses import dataclass
import html
import re
from typing import Any


_WIKI_DISAMBIGUATION_RE = re.compile(r"\bmay refer to\b|может означать|страница значений", re.IGNORECASE)
_CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
_LATIN_RE = re.compile(r"[A-Za-z]")
_BAD_DEFINITION_MARKERS = (
    "slang",
    "offensive",
    "vulgar",
    "derogatory",
    "obscene",
    "sexual",
    "taboo",
    "profanity",
    "обсц",
    "бран",
    "вульг",
    "неценз",
    "мат",
    "табу",
)
_PREFERRED_PARTS_OF_SPEECH = {"noun", "verb", "adjective", "adverb"}
_RU_MEANING_LABELS: tuple[tuple[str, str], ...] = (
    ("{{обсц", "обсц."),
    ("{{бран", "бран."),
    ("{{вульг", "вульг."),
    ("{{прост", "прост."),
    ("{{разг", "разг."),
    ("{{сленг", "сленг."),
    ("{{зоол", "зоол."),
)
_RU_POS_LABELS: tuple[tuple[str, str], ...] = (
    ("{{сущ", "сущ."),
    ("{{гл", "глаг."),
    ("{{прил", "прил."),
    ("{{нар", "нареч."),
)
_WIKITEXT_LINK_RE = re.compile(r"\[\[([^|\]]+)\|([^\]]+)\]\]")
_WIKITEXT_SIMPLE_LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_WIKITEXT_EXTERNAL_LINK_RE = re.compile(r"\[https?://[^\s\]]+\s+([^\]]+)\]")
_WIKITEXT_REF_RE = re.compile(r"<ref[^>]*>.*?</ref>", re.IGNORECASE | re.DOTALL)
_WIKITEXT_TAG_RE = re.compile(r"<[^>]+>")
_WIKITEXT_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_RU_LANGUAGE_SECTION_RE = re.compile(r"= \{\{-ru-\}\} =(.*?)(?=\n= \{\{-[a-z-]+-\}\} =|\Z)", re.DOTALL)
_RU_MEANING_SECTION_RE = re.compile(r"==== Значение ====(.*?)(?=\n==== |\n=== |\Z)", re.DOTALL)
_RU_DEFINITION_LINE_RE = re.compile(r"^#\s*(.+)$", re.MULTILINE)
_LATIN_PAREN_RE = re.compile(r"\s*\(([A-Za-z0-9 .,_;:-]+)\)")


@dataclass(frozen=True)
class WikiLookupResult:
    kind: str
    title: str
    summary: str = ""
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class DefinitionResult:
    part_of_speech: str
    definition: str
    example: str = ""
    labels: tuple[str, ...] = ()


def detect_query_language(text: str) -> str:
    payload = text.strip()
    if not payload:
        return "en"
    if _CYRILLIC_RE.search(payload):
        return "ru"
    if _LATIN_RE.search(payload):
        return "en"
    return "en"


def is_wiki_disambiguation(payload: dict[str, Any]) -> bool:
    title = str(payload.get("title", ""))
    extract = str(payload.get("extract", ""))
    page_type = str(payload.get("type", ""))
    return (
        page_type == "disambiguation"
        or title.lower().endswith("(disambiguation)")
        or title.lower().endswith("(значения)")
        or bool(_WIKI_DISAMBIGUATION_RE.search(extract))
    )


def extract_wiki_choices(search_payload: dict[str, Any], *, limit: int = 5) -> list[str]:
    query = search_payload.get("query", {}) if isinstance(search_payload, dict) else {}
    search = query.get("search", []) if isinstance(query, dict) else []
    titles: list[str] = []
    for item in search:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        if not title or title.lower().endswith("(disambiguation)"):
            continue
        if title not in titles:
            titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def choose_preferred_wiki_title(query: str, titles: list[str]) -> str | None:
    normalized_query = normalize_lookup_text(query)
    if not normalized_query:
        return None

    scored: list[tuple[int, str]] = []
    for index, title in enumerate(titles):
        normalized_title = normalize_lookup_text(title)
        score = 100 - index * 10
        if normalized_title == normalized_query:
            score += 35
        if normalized_title.startswith(normalized_query):
            score += 20
        if "(" not in title:
            score += 10
        if "programming language" in title.lower():
            score += 8
        if "disambiguation" in title.lower():
            score -= 100
        scored.append((score, title))

    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def build_wiki_result(query: str, summary_payload: dict[str, Any], search_payload: dict[str, Any] | None = None) -> WikiLookupResult:
    title = str(summary_payload.get("title", query)).strip() or query
    extract = str(summary_payload.get("extract", "")).strip()
    if extract and not is_wiki_disambiguation(summary_payload):
        return WikiLookupResult(kind="summary", title=title, summary=extract)

    choices = extract_wiki_choices(search_payload or {})
    if choices:
        return WikiLookupResult(kind="choices", title=query.strip() or title, choices=tuple(choices))
    return WikiLookupResult(kind="not_found", title=query.strip() or title)


def normalize_lookup_text(text: str) -> str:
    lowered = text.lower().strip()
    lowered = re.sub(r"\s*\([^)]*\)\s*", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _definition_score(result: DefinitionResult) -> int:
    text = f"{result.part_of_speech} {' '.join(result.labels)} {result.definition} {result.example}".lower()
    score = 0
    if result.part_of_speech.lower() in _PREFERRED_PARTS_OF_SPEECH:
        score += 15
    if any(marker in text for marker in _BAD_DEFINITION_MARKERS):
        score -= 100
    if 20 <= len(result.definition) <= 180:
        score += 10
    if result.example:
        score += 3
    return score


def select_best_definition_results(results: list[DefinitionResult], *, limit: int = 3) -> list[DefinitionResult]:
    ranked: list[tuple[int, DefinitionResult]] = []
    for result in results:
        score = _definition_score(result)
        ranked.append((score, result))

    ranked.sort(key=lambda item: item[0], reverse=True)
    preferred = [item for item in ranked if item[0] >= 0] or ranked

    selected: list[DefinitionResult] = []
    seen: set[tuple[str, str]] = set()
    for _, result in preferred:
        key = (
            result.part_of_speech.lower(),
            result.definition.lower(),
        )
        if key in seen:
            continue
        seen.add(key)
        selected.append(result)
        if len(selected) >= limit:
            break
    return selected


def select_safe_definitions(entries: list[dict[str, Any]], *, limit: int = 3) -> list[DefinitionResult]:
    candidates: list[DefinitionResult] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for meaning in entry.get("meanings", []):
            if not isinstance(meaning, dict):
                continue
            part_of_speech = str(meaning.get("partOfSpeech", "")).strip()
            for definition in meaning.get("definitions", []):
                if not isinstance(definition, dict):
                    continue
                text = str(definition.get("definition", "")).strip()
                example = str(definition.get("example", "")).strip()
                if not text:
                    continue
                candidates.append(DefinitionResult(part_of_speech=part_of_speech, definition=text, example=example))

    return select_best_definition_results(candidates, limit=limit)


def parse_russian_wiktionary_definitions(wikitext: str, *, limit: int = 3) -> list[DefinitionResult]:
    if not wikitext.strip():
        return []

    language_match = _RU_LANGUAGE_SECTION_RE.search(wikitext)
    language_section = language_match.group(1) if language_match else wikitext
    meaning_match = _RU_MEANING_SECTION_RE.search(language_section)
    meaning_section = meaning_match.group(1) if meaning_match else language_section
    part_of_speech = _detect_russian_part_of_speech(language_section)

    candidates: list[DefinitionResult] = []
    for match in _RU_DEFINITION_LINE_RE.finditer(meaning_section):
        raw_line = match.group(1).strip()
        if not raw_line or raw_line == "#":
            continue
        labels = _extract_russian_labels(raw_line)
        cleaned = _cleanup_wiktionary_markup(raw_line, output_language="ru")
        cleaned = cleaned.lstrip("—-:;,. ").strip()
        if not cleaned:
            continue
        candidates.append(
            DefinitionResult(
                part_of_speech=part_of_speech,
                definition=cleaned,
                labels=tuple(labels),
            )
        )

    return select_best_definition_results(candidates, limit=limit)


def _detect_russian_part_of_speech(section: str) -> str:
    lowered = section.lower()
    for marker, label in _RU_POS_LABELS:
        if marker in lowered:
            return label
    return ""


def _extract_russian_labels(raw_line: str) -> list[str]:
    lowered = raw_line.lower()
    labels: list[str] = []
    for marker, label in _RU_MEANING_LABELS:
        if marker in lowered and label not in labels:
            labels.append(label)
    return labels


def _cleanup_wiktionary_markup(text: str, *, output_language: str) -> str:
    cleaned = _WIKITEXT_COMMENT_RE.sub("", text)
    cleaned = _WIKITEXT_REF_RE.sub("", cleaned)
    cleaned = _remove_wikitext_templates(cleaned)
    cleaned = _WIKITEXT_LINK_RE.sub(r"\2", cleaned)
    cleaned = _WIKITEXT_SIMPLE_LINK_RE.sub(r"\1", cleaned)
    cleaned = _WIKITEXT_EXTERNAL_LINK_RE.sub(r"\1", cleaned)
    cleaned = _WIKITEXT_TAG_RE.sub("", cleaned)
    cleaned = cleaned.replace("'''", "").replace("''", "")
    cleaned = cleaned.replace("{{-}}", "-")
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if output_language == "ru":
        cleaned = _LATIN_PAREN_RE.sub("", cleaned)
    return cleaned.strip(" ,;")


def _remove_wikitext_templates(text: str) -> str:
    output: list[str] = []
    depth = 0
    index = 0
    while index < len(text):
        if text.startswith("{{", index):
            depth += 1
            index += 2
            continue
        if depth and text.startswith("}}", index):
            depth -= 1
            index += 2
            continue
        if depth == 0:
            output.append(text[index])
        index += 1
    return "".join(output)
