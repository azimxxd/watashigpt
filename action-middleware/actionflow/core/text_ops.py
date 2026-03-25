from __future__ import annotations

import ast
import base64
import hashlib
import html
import json
import math
import operator
import re
import shlex
from datetime import datetime
from xml.dom import minidom

from .models import TextAnalysis

try:
    from langdetect import DetectorFactory
    from langdetect import detect as _langdetect_detect

    DetectorFactory.seed = 0
    _LANGDETECT_AVAILABLE = True
except ImportError:
    _LANGDETECT_AVAILABLE = False


CODE_INDICATORS = [
    r"^\s*(def |class |import |from \w+ import|function |const |let |var )",
    r"[{};]\s*$",
    r"^\s*(public |private |protected |static |async |await )",
    r"^\s*#include|^\s*package |^\s*using ",
    r"[!=]=",
    r"->\s*\w+",
    r"^\s*<\w+[\s/>]",
]

INFORMAL_MARKERS = frozenset(
    [
        "lol", "omg", "wtf", "bruh", "nah", "gonna", "wanna", "gotta",
        "idk", "imo", "tbh", "lmao", "smh", "fr", "ngl", "asap", "pls",
        "plz", "thx", "ty", "np",
    ]
)

SAFE_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}

SAFE_FUNCTIONS = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "ceil": math.ceil,
    "floor": math.floor,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
}

READING_WORDS_PER_MINUTE = 225
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z0-9_.-]+)\s*\}\}")


def analyze_text(text: str) -> TextAnalysis:
    result = TextAnalysis()
    stripped = text.strip()
    result.length = len(stripped)

    if _LANGDETECT_AVAILABLE and stripped:
        try:
            result.language = _langdetect_detect(stripped[:500])
        except Exception:
            result.language = "en"

    code_line_count = 0
    sample = stripped.split("\n")[:30]
    for line in sample:
        for pattern in CODE_INDICATORS:
            if re.search(pattern, line):
                code_line_count += 1
                break
    result.is_code = (code_line_count / max(len(sample), 1)) > 0.3

    if result.is_code:
        if re.search(r"\bdef\b.*:\s*$|^\s*import\s+\w+|from\s+\w+\s+import", stripped, re.MULTILINE):
            result.code_language = "python"
        elif re.search(r"\bfunction\b|\bconst\b|\blet\b|\bconsole\.", stripped):
            result.code_language = "javascript"

    informal_count = sum(1 for word in stripped.lower().split() if word.strip(".,!?") in INFORMAL_MARKERS)
    result.is_formal = informal_count < 2

    if result.is_code:
        result.looks_like = "code"
    elif stripped.startswith("{") and stripped.endswith("}"):
        result.looks_like = "json"
    elif re.match(r"https?://", stripped):
        result.looks_like = "url"
    elif re.search(r"^(diff --git|@@\s)", stripped, re.MULTILINE):
        result.looks_like = "commit_diff"
    elif re.search(r"^\s*[-*]\s", stripped, re.MULTILINE) and stripped.count("\n") > 2:
        result.looks_like = "list"
    elif re.search(r"(action items|next steps|attendees|agenda)", stripped.lower()):
        result.looks_like = "meeting_notes"
    elif re.search(r"(traceback|error|exception|stack trace)", stripped.lower()):
        result.looks_like = "error"
    elif re.search(r"(dear |hi |hello |subject:|re:)", stripped.lower()[:100]):
        result.looks_like = "email_draft"

    return result


def _ast_eval(node: ast.AST):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in SAFE_OPERATORS:
        return SAFE_OPERATORS[type(node.op)](_ast_eval(node.left), _ast_eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in SAFE_OPERATORS:
        return SAFE_OPERATORS[type(node.op)](_ast_eval(node.operand))
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in SAFE_FUNCTIONS:
        return SAFE_FUNCTIONS[node.func.id](*[_ast_eval(arg) for arg in node.args])
    raise ValueError("Unsafe or unsupported expression")


def safe_eval_math(expr: str) -> str | None:
    expression = expr.strip().lower()
    expression = re.sub(r"(\d+(?:\.\d+)?)\s*%\s+of\s+(\d+(?:\.\d+)?)", r"(\1/100)*\2", expression)
    expression = expression.replace("^", "**")
    expression = expression.replace("pi", str(math.pi))

    try:
        parsed = ast.parse(expression, mode="eval")
        value = _ast_eval(parsed.body)
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(round(value, 10)).rstrip("0").rstrip(".")
    except Exception:
        return None


def sponge_case(text: str) -> str:
    out: list[str] = []
    upper = False
    for char in text:
        if char.isalpha():
            out.append(char.upper() if upper else char.lower())
            upper = not upper
        else:
            out.append(char)
    return "".join(out)


def encode_base64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def decode_base64(text: str) -> str:
    return base64.b64decode(text.encode("ascii"), validate=True).decode("utf-8")


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_PII_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
    (re.compile(r"\b(?:\+?\d[\d\s().-]{7,}\d)\b"), "[PHONE]"),
    (re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "[CARD]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
]


def redact_sensitive(text: str) -> str:
    redacted = text
    for pattern, replacement in _PII_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def escape_text(text: str, mode: str = "html") -> str:
    normalized = (mode or "json").lower()
    if normalized in {"code", "string"}:
        normalized = "json"
    if normalized == "html":
        return html.escape(text, quote=True)
    if normalized == "regex":
        return re.escape(text)
    if normalized == "json":
        return json.dumps(text)[1:-1]
    if normalized == "python":
        escaped = text.encode("unicode_escape").decode("ascii")
        escaped = escaped.replace("'", "\\'")
        return escaped
    if normalized == "shell":
        return shlex.quote(text)
    if normalized == "sql":
        return text.replace("'", "''")
    raise ValueError(f"Unsupported escape mode: {mode}")


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MARKDOWN_RE = re.compile(r"[*_`#>\[\]()~-]+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def sanitize_text(text: str) -> str:
    cleaned = _ANSI_RE.sub("", text)
    cleaned = _HTML_TAG_RE.sub("", cleaned)
    cleaned = _MARKDOWN_RE.sub("", cleaned)
    cleaned = _CONTROL_RE.sub("", cleaned)
    return cleaned.strip()


def format_structured_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return stripped
    if stripped.startswith("{") or stripped.startswith("["):
        return json.dumps(json.loads(stripped), ensure_ascii=False, indent=2, sort_keys=False)
    if stripped.startswith("<"):
        return minidom.parseString(stripped.encode("utf-8")).toprettyxml(indent="  ")
    raise ValueError("Unsupported structured text")


def text_stats(text: str) -> dict[str, int]:
    words = len(re.findall(r"\w+", text, flags=re.UNICODE))
    chars = len(text)
    lines = len(text.splitlines()) or 1
    return {
        "words": words,
        "chars": chars,
        "lines": lines,
        "reading_seconds": max(1, round(words / READING_WORDS_PER_MINUTE * 60)) if words else 0,
    }


def format_reading_time(seconds: int) -> str:
    if seconds <= 0:
        return "0 sec"
    if seconds < 5:
        return "< 5 sec"
    if seconds < 60:
        rounded = int(round(seconds / 5.0) * 5)
        rounded = max(5, min(55, rounded))
        return f"~{rounded} sec"
    if seconds < 90:
        return "< 1 min"
    minutes = int(round(seconds / 60.0))
    return f"~{minutes} min"


def extract_placeholders(text: str) -> list[str]:
    seen: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(text):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def fill_placeholders(text: str, values: dict[str, str]) -> str:
    if not values:
        return text

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return values.get(key, match.group(0))

    return _PLACEHOLDER_RE.sub(_replace, text)


def normalize_date_string(text: str) -> str | None:
    try:
        import dateparser
    except ImportError:
        return None
    parsed = dateparser.parse(text)
    if parsed is None:
        return None
    return parsed.astimezone().date().isoformat() if parsed.tzinfo else parsed.date().isoformat()
