"""Command palette logic shared by the UIs: command metadata (titles, icons),
fuzzy search, and PaletteController which drives mac_ui.CommandPalette."""

from __future__ import annotations

import re
from typing import Callable

from actionflow import llm

TONE_STYLES = ["casual", "formal", "friendly", "confident", "empathetic", "diplomatic",
               "concise", "academic", "professional email", "encouraging", "sarcastic", "gen-z"]

TRANS_LANGS = [
    ("English", "EN", "\U0001f1ec\U0001f1e7"), ("Russian", "RU", "\U0001f1f7\U0001f1fa"),
    ("Kazakh", "KK", "\U0001f1f0\U0001f1ff"), ("Ukrainian", "UK", "\U0001f1fa\U0001f1e6"),
    ("Spanish", "ES", "\U0001f1ea\U0001f1f8"), ("French", "FR", "\U0001f1eb\U0001f1f7"),
    ("German", "DE", "\U0001f1e9\U0001f1ea"), ("Chinese", "ZH", "\U0001f1e8\U0001f1f3"),
    ("Japanese", "JA", "\U0001f1ef\U0001f1f5"), ("Korean", "KO", "\U0001f1f0\U0001f1f7"),
    ("Turkish", "TR", "\U0001f1f9\U0001f1f7"), ("Arabic", "AR", "\U0001f1f8\U0001f1e6"),
    ("Portuguese", "PT", "\U0001f1e7\U0001f1f7"), ("Italian", "IT", "\U0001f1ee\U0001f1f9"),
    ("Hindi", "HI", "\U0001f1ee\U0001f1f3"), ("Polish", "PL", "\U0001f1f5\U0001f1f1"),
    ("Uzbek", "UZ", "\U0001f1fa\U0001f1ff"), ("Dutch", "NL", "\U0001f1f3\U0001f1f1"),
]


# name → (title, SF Symbol, tint). Commands not listed get a generic look.
COMMAND_META: dict[str, tuple[str, str, str]] = {
    "summarize": ("Summarize", "text.append", "purple"),
    "rewrite": ("Rewrite", "pencil.line", "purple"),
    "explain": ("Explain", "lightbulb", "yellow"),
    "tone": ("Change Tone", "theatermasks", "pink"),
    "trans": ("Translate", "globe", "teal"),
    "polite": ("Make Polite", "hand.wave", "pink"),
    "bullets": ("Bullet Points", "list.bullet", "purple"),
    "title": ("Headline", "textformat.size", "purple"),
    "tweet": ("Shorten to Tweet", "bubble.left", "blue"),
    "email": ("Draft Email", "envelope", "blue"),
    "meeting": ("Meeting Notes", "person.3", "indigo"),
    "todo": ("Action Items", "checklist", "indigo"),
    "eli5": ("Explain Simply", "face.smiling", "yellow"),
    "haiku": ("Haiku", "leaf", "green"),
    "roast": ("Roast", "flame", "red"),
    "fill": ("Fill Placeholders", "square.and.pencil", "purple"),
    "regex": ("Generate Regex", "asterisk", "orange"),
    "docstring": ("Docstring", "doc.text", "orange"),
    "review": ("Code Review", "checkmark.seal", "orange"),
    "gitcommit": ("Commit Message", "arrow.triangle.branch", "orange"),
    "fmt": ("Format JSON / YAML / XML", "curlybraces", "orange"),
    "b64": ("Base64 Encode", "lock", "orange"),
    "decode": ("Base64 Decode", "lock.open", "orange"),
    "hash": ("SHA-256 Hash", "number.square", "orange"),
    "escape": ("Escape Characters", "chevron.left.forwardslash.chevron.right", "orange"),
    "calc": ("Calculate", "plus.forwardslash.minus", "blue"),
    "date": ("Parse Date", "calendar", "blue"),
    "count": ("Word Count", "number", "blue"),
    "redact": ("Redact Personal Data", "eye.slash", "blue"),
    "sanitize": ("Strip Formatting", "eraser", "blue"),
    "mock": ("Mocking Case", "textformat.abc", "gray"),
    "wiki": ("Wikipedia", "book", "green"),
    "define": ("Define Word", "character.book.closed", "green"),
    "image": ("Generate Image", "photo", "green"),
    "command": ("Run Shell Command", "terminal", "gray"),
    "password": ("Generate Password", "key", "gray"),
    "repeat": ("Repeat Last Command", "arrow.clockwise", "gray"),
    "clip": ("Clipboard Slots", "paperclip", "gray"),
    "stack": ("Push to Clipboard Stack", "tray.and.arrow.down", "gray"),
    "pop": ("Pop Clipboard Stack", "tray.and.arrow.up", "gray"),
    "test": ("Test Pipeline", "stethoscope", "gray"),
}

# LLM-ish commands that still run immediately (their output isn't a text replacement)
PREVIEW_EXCLUDED = frozenset({"count", "define", "wiki", "image"})


def command_subtitle(cmd: dict) -> str:
    desc = cmd.get("description", "")
    return re.sub(r"\s*\((?:LLM|notification only|phrase lookup \+ LLM|Pollinations\.ai)\)\s*$",
                   "", desc).strip()


def fuzzy_score(query: str, name: str, cmd: dict, title: str) -> float:
    """Rank a command for a search query (0 = no match)."""
    q = query.lower().strip()
    t = title.lower()
    if t.startswith(q) or name.startswith(q):
        return 100
    if any(w.startswith(q) for w in t.split()):
        return 80
    if any(p.lower().rstrip(":").startswith(q) for p in cmd.get("prefixes", [])):
        return 70
    if q in t or q in name:
        return 60
    if any(q in k.lower() for k in cmd.get("keywords", [])):
        return 40
    if q in cmd.get("description", "").lower():
        return 20
    it = iter(t)
    if len(q) >= 2 and all(ch in it for ch in q):  # subsequence: "sm" → "Summarize"
        return 10
    return 0


class PaletteController:
    """Supplies items and actions to mac_ui.CommandPalette."""

    def __init__(self, text: str, commands: dict, suggestions: list | None,
                 prompt_for: Callable[[str, dict, str], tuple[str, str]]) -> None:
        self.prompt_for = prompt_for  # (cmd_name, cmd_config, payload) → (prompt, model)
        self.text = text
        self.commands = commands
        self.suggestions = suggestions or [(n, c, False) for n, c in sorted(commands.items())]

    # ── items ──
    def _item(self, name: str, cmd: dict) -> dict:
        title, icon, tint = COMMAND_META.get(name, (name.replace("_", " ").title(), "command", "gray"))
        if cmd.get("_personal"):
            title = name.replace("personal_", "").replace("_", " ").title()
            icon, tint = "person.crop.circle", "pink"
        item = {"id": name, "title": title, "icon": icon, "tint": tint,
                "subtitle": command_subtitle(cmd)}
        if cmd.get("llm_required") or name == "polite":
            live = llm.MODE == "live"
            item["tag"], item["tag_tint"] = ("AI", "purple") if live else ("No LLM", "gray")
        return item

    def _custom_item(self, query: str) -> dict:
        return {"id": "custom", "title": f"Ask AI: {query}", "icon": "sparkles", "tint": "purple",
                "subtitle": "Run as a custom instruction", "tag": "AI" if llm.MODE == "live" else "No LLM",
                "tag_tint": "purple" if llm.MODE == "live" else "gray", "instruction": query}

    def items(self, query: str, submenu: str | None) -> list[dict]:
        query = query.strip()
        if submenu == "tone":
            styles = [st for st in TONE_STYLES if query.lower() in st.lower()]
            rows = [{"id": f"tone:{st}", "title": st.title(), "icon": "theatermasks", "tint": "pink",
                     "subtitle": f"Rewrite in a {st} tone"} for st in styles]
            if query and not any(st.lower() == query.lower() for st in styles):
                rows.append({"id": f"tone:{query}", "title": query.title(), "icon": "plus",
                             "tint": "pink", "subtitle": "Custom tone"})
            return rows
        if submenu == "trans":
            langs = [l for l in TRANS_LANGS if query.lower() in l[0].lower() or query.lower() == l[1].lower()]
            rows = [{"id": f"trans:{name}", "title": name, "icon": "globe", "tint": "teal",
                     "subtitle": f"{flag}  {code}"} for name, code, flag in langs]
            if query and not langs:
                rows.append({"id": f"trans:{query}", "title": f"Translate to {query}", "icon": "globe",
                             "tint": "teal", "subtitle": "Custom language"})
            return rows

        if not query:
            starred = [self._item(n, c) for n, c, star in self.suggestions if star]
            rest = [self._item(n, c) for n, c, star in self.suggestions if not star]
            if not starred:
                return rest
            return ([{"header": True, "title": "Suggested"}] + starred +
                    [{"header": True, "title": "All Commands"}] + rest)

        scored = []
        for name, cmd in self.commands.items():
            item = self._item(name, cmd)
            score = fuzzy_score(query, name, cmd, item["title"])
            if score:
                scored.append((score, item["title"], item))
        matches = [item for _s, _t, item in sorted(scored, key=lambda x: (-x[0], x[1]))]
        custom = self._custom_item(query)
        # A sentence is an instruction; a word is probably a search.
        if " " in query or not matches:
            return [custom] + matches
        return matches + [custom]

    # ── actions ──
    def _stream_action(self, cmd_name: str, cmd_config: dict, payload: str, title: str,
                       icon: str, tint: str) -> dict:
        if llm.MODE != "live":
            return {"kind": "message", "title": title,
                    "text": "This command needs an LLM.\n\nSet llm.provider in config.yaml "
                            "(or run setup on start) and save the API key with\n"
                            "  python main.py --set-key <provider>"}
        try:
            prompt, model = self.prompt_for(cmd_name, cmd_config, payload)
        except ValueError as exc:
            return {"kind": "message", "title": title, "text": str(exc)}
        return {"kind": "stream", "title": title, "icon": icon, "tint": tint,
                "cmd_name": cmd_name, "cmd_config": cmd_config,
                "factory": lambda: llm.stream(prompt, model)}

    def activate(self, item: dict, query: str) -> dict:
        item_id = item["id"]
        if item_id == "custom":
            return self._stream_action("custom", {"instruction": item["instruction"], "llm_required": True},
                                       self.text, f"Ask AI · {item['instruction']}", "sparkles", "purple")
        if item_id.startswith("tone:"):
            return self._stream_action("tone", self.commands.get("tone", {}),
                                       f"{item_id[5:]}: {self.text}", f"Tone · {item['title']}",
                                       "theatermasks", "pink")
        if item_id.startswith("trans:"):
            code = item_id[6:]
            return self._stream_action("trans", self.commands.get("trans", {}), f"{code}: {self.text}",
                                       f"Translate · {item['title']}", "globe", "teal")
        if item_id == "tone":
            return {"kind": "submenu", "id": "tone", "title": "Choose a tone…"}
        if item_id == "trans":
            return {"kind": "submenu", "id": "trans", "title": "Translate to… (type any language)"}

        cmd = self.commands.get(item_id, {})
        if item_id == "polite" and self.text.strip().lower() in cmd.get("phrases", {}):
            return {"kind": "run"}  # instant phrase lookup
        if (cmd.get("llm_required") or item_id == "polite") and item_id not in PREVIEW_EXCLUDED:
            return self._stream_action(item_id, cmd, self.text, item["title"], item["icon"], item["tint"])
        return {"kind": "run"}

    def refine(self, result_text: str, instruction: str) -> dict:
        cfg = {"instruction": instruction, "llm_required": True}
        action = self._stream_action("custom", cfg, result_text, f"Refined · {instruction}",
                                     "sparkles", "purple")
        return action
