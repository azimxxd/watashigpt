"""Built-in text transformations — pure functions, no I/O, no UI.

Handlers in main.py call these and take care of undo/paste/notifications.
"""

from __future__ import annotations

import ast
import base64
import binascii
import hashlib
import html
import json
import math
import operator
import re
import secrets
import string
import xml.dom.minidom

import yaml

# ============================================================
# FMT — JSON / YAML / XML
# ============================================================


def format_structured(text: str) -> tuple[str, str]:
    """Pretty-print (or minify / sort) JSON, YAML or XML.

    Optional mode prefix in the text: "min:" / "minify:" / "sort:".
    Returns (label, result); raises ValueError with a user-facing message.
    """
    content = text.strip()
    minify = sort_keys = False
    lower = content.lower()
    if lower.startswith(("min:", "minify:")):
        minify = True
        content = re.sub(r"^(?:min|minify):\s*", "", content, flags=re.IGNORECASE).strip()
    elif lower.startswith("sort:"):
        sort_keys = True
        content = re.sub(r"^sort:\s*", "", content, flags=re.IGNORECASE).strip()

    try:
        parsed = json.loads(content)
        if minify:
            return "Minified JSON", json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
        return ("Formatted JSON" + (" (sorted)" if sort_keys else ""),
                json.dumps(parsed, indent=2, ensure_ascii=False, sort_keys=sort_keys))
    except json.JSONDecodeError as exc:
        json_error = exc

    if content.lstrip().startswith("<"):
        try:
            dom = xml.dom.minidom.parseString(content)
            had_decl = content.lstrip().startswith("<?xml")
            if minify:
                result = dom.toxml()
                if not had_decl:
                    result = re.sub(r"^<\?xml[^?]*\?>\s*", "", result)
                return "Minified XML", result
            result = dom.toprettyxml(indent="  ")
            if not had_decl:
                result = "\n".join(result.split("\n")[1:])
            # minidom adds blank lines around text nodes
            return "Formatted XML", "\n".join(l for l in result.split("\n") if l.strip())
        except Exception:
            pass

    try:
        parsed_yaml = yaml.safe_load(content)
        if isinstance(parsed_yaml, (dict, list)) and ("\n" in content or ":" in content):
            if minify:
                return "YAML → minified JSON", json.dumps(parsed_yaml, separators=(",", ":"),
                                                          ensure_ascii=False)
            return ("Formatted YAML" + (" (sorted)" if sort_keys else ""),
                    yaml.dump(parsed_yaml, default_flow_style=False, allow_unicode=True,
                              sort_keys=sort_keys).strip())
    except Exception:
        pass

    raise ValueError(f"Could not parse as JSON, YAML or XML (JSON error at line "
                     f"{json_error.lineno}, col {json_error.colno}: {json_error.msg})")


# ============================================================
# Small transforms
# ============================================================


def text_stats(text: str) -> str:
    content = text.strip()
    words = len(content.split())
    lines = content.count("\n") + 1
    reading_min = max(1, round(words / 200))
    return (f"Words: {words}\nCharacters: {len(content)}\nLines: {lines}\n"
            f"Reading time: ~{reading_min} min")


def mocking_case(text: str) -> str:
    """sPoNgEbOb case — alternates letters only, so spaces don't break the rhythm."""
    out, upper = [], True
    for ch in text.strip():
        if ch.isalpha():
            out.append(ch.upper() if upper else ch.lower())
            upper = not upper
        else:
            out.append(ch)
    return "".join(out)


def b64_encode(text: str) -> str:
    return base64.b64encode(text.strip().encode()).decode()


def b64_decode(text: str) -> str:
    """Strict Base64 (standard or URL-safe) → UTF-8 text. Raises ValueError."""
    content = re.sub(r"\s+", "", text)
    content += "=" * (-len(content) % 4)
    for decoder in (lambda c: base64.b64decode(c, validate=True), base64.urlsafe_b64decode):
        try:
            return decoder(content).decode("utf-8")
        except (binascii.Error, ValueError, UnicodeDecodeError):
            continue
    raise ValueError("not valid Base64 (or not UTF-8 text)")


def sha256(text: str) -> str:
    return hashlib.sha256(text.strip().encode()).hexdigest()


def generate_password(length: int = 20) -> str:
    """Random password with at least one lower, upper, digit and symbol."""
    length = max(8, min(int(length), 256))
    charset = string.ascii_letters + string.digits + string.punctuation
    while True:
        pw = "".join(secrets.choice(charset) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in string.punctuation for c in pw)):
            return pw


# ============================================================
# REDACT — personal data
# ============================================================

def _phone(match: re.Match) -> str:
    # Order numbers / years / prices also look like digit groups — a phone
    # number has at least 9 digits.
    return "[PHONE]" if sum(ch.isdigit() for ch in match.group(0)) >= 9 else match.group(0)


PII_PATTERNS: list[tuple[re.Pattern, object]] = [
    (re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+"), "[EMAIL]"),
    (re.compile(r"(?:Bearer\s+|api[_-]?key[=:]\s*)[A-Za-z0-9_\-./+=]{20,}", re.IGNORECASE), "[API_KEY]"),
    (re.compile(r"\b(?:sk|pk|gsk|ghp|xox[bap])[-_][A-Za-z0-9_\-]{16,}\b"), "[API_KEY]"),
    (re.compile(r"\b[a-fA-F0-9]{40,}\b"), "[TOKEN]"),
    (re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"), "[CARD]"),
    (re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}\b"), "[IBAN]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "[IP]"),
    (re.compile(r"(?<![\w.])\+?\d[\d\s().-]{7,}\d\b"), _phone),
    (re.compile(r"\b[A-Z]{2}\d{7}\b"), "[PASSPORT]"),
    (re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b"), "[DATE]"),
]


def redact(text: str) -> tuple[str, int]:
    """Mask personal data. Returns (result, number of items masked)."""
    result, count = text.strip(), 0
    for pattern, replacement in PII_PATTERNS:
        def _sub(m: re.Match, r=replacement) -> str:
            nonlocal count
            out = r(m) if callable(r) else r
            count += out != m.group(0)
            return out
        result = pattern.sub(_sub, result)
    return result, count


# ============================================================
# ESCAPE / SANITIZE
# ============================================================


def escape(text: str) -> tuple[str, str]:
    """Escape for HTML / SQL string literal / regex. Mode prefix ("html:",
    "sql:", "regex:") or auto-detect. Returns (mode, result)."""
    content = text.strip()
    mode = None
    for prefix in ("html:", "sql:", "regex:"):
        if content.lower().startswith(prefix):
            mode, content = prefix[:-1], content[len(prefix):].strip()
            break
    if mode is None:
        if "<" in content and ">" in content:
            mode = "html"
        elif "'" in content:
            mode = "sql"
        else:
            mode = "regex"
    if mode == "html":
        return mode, html.escape(content)
    if mode == "sql":
        # A string literal only needs its quotes doubled. (Use parameterised
        # queries for real code — this is for pasting into a console.)
        return mode, content.replace("'", "''")
    return mode, re.escape(content)


def sanitize(text: str) -> str:
    """Strip ANSI codes, HTML tags and Markdown syntax → plain text."""
    result = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text.strip())
    if re.search(r"<[a-zA-Z/][^>]*>", result):
        result = html.unescape(re.sub(r"<[^>]+>", "", result))
    result = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", result)            # images
    result = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", result)             # links
    result = re.sub(r"^#{1,6}\s+", "", result, flags=re.MULTILINE)       # headings
    result = re.sub(r"(\*\*|__)(.+?)\1", r"\2", result)                  # bold
    result = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", result)  # italic
    result = re.sub(r"```[a-zA-Z0-9]*\n?", "", result)                   # code fences
    result = re.sub(r"`([^`]+)`", r"\1", result)                         # inline code
    result = re.sub(r"^\s*[-*+]\s+", "", result, flags=re.MULTILINE)     # list markers
    result = re.sub(r"^\s*\d+\.\s+", "", result, flags=re.MULTILINE)     # numbered lists
    result = re.sub(r"^>\s?", "", result, flags=re.MULTILINE)            # blockquotes
    return result.strip()


# ============================================================
# CALC — safe math (ast whitelist, never eval())
# ============================================================

# Natural-language fragments rewritten in place ("15% of 340 + 1" → "(51.0) + 1")
_CALC_NATURAL = [
    (re.compile(r"(\d+(?:\.\d+)?)\s*%\s*of\s*(\d+(?:\.\d+)?)", re.IGNORECASE),
     lambda m: repr(float(m.group(1)) / 100 * float(m.group(2)))),
]

_MATH_FUNCS = {
    "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan,
    "log": math.log10, "ln": math.log, "log2": math.log2,
    "abs": abs, "ceil": math.ceil, "floor": math.floor,
    "sqrt": math.sqrt, "exp": math.exp,
    "radians": math.radians, "degrees": math.degrees,
}

_AST_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub,
    ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}


def _ast_eval(node: ast.AST):
    if isinstance(node, ast.Constant):
        if not isinstance(node.value, (int, float)) or isinstance(node.value, bool):
            raise ValueError("only numbers allowed")
        if isinstance(node.value, int) and abs(node.value) > 10**15:
            raise ValueError("number too large")
        return node.value
    if isinstance(node, ast.BinOp):
        left, right = _ast_eval(node.left), _ast_eval(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 100:
            raise ValueError("exponent too large (max 100)")
        op_fn = _AST_OPS.get(type(node.op))
        if op_fn is None:
            raise ValueError(f"unsupported operator {type(node.op).__name__}")
        return op_fn(left, right)
    if isinstance(node, ast.UnaryOp):
        operand = _ast_eval(node.operand)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
    raise ValueError(f"unsupported expression ({type(node).__name__})")


def calc(expr: str) -> str | None:
    """Evaluate a math expression; returns the formatted result or None."""
    for pattern, fn in _CALC_NATURAL:
        expr = pattern.sub(lambda m: f"({fn(m)})", expr)

    for func_name, func_fn in _MATH_FUNCS.items():
        pattern = re.compile(rf"\b{func_name}\(([^()]+)\)", re.IGNORECASE)
        while True:
            m = pattern.search(expr)
            if not m:
                break
            inner = calc(m.group(1))
            try:
                if inner is None:
                    return None
                expr = expr[:m.start()] + f"({func_fn(float(inner))!r})" + expr[m.end():]
            except (ValueError, OverflowError):
                return None

    cleaned = re.sub(r"[^0-9+\-*/().%^ e]", "", expr)
    cleaned = re.sub(r"(?<![\d.])e|e(?![\d+\-])", "", cleaned)  # keep "e" only in 1.5e-16
    cleaned = cleaned.replace("^", "**").strip()                  # leading space = IndentationError
    if not cleaned:
        return None
    try:
        value = float(_ast_eval(ast.parse(cleaned, mode="eval").body))
    except Exception:
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return str(int(value)) if value == int(value) else str(round(value, 10))
