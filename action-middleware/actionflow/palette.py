"""Command palette logic shared by the UIs: command metadata (titles, icons),
fuzzy search, and PaletteController which drives mac_ui.CommandPalette."""

from __future__ import annotations

import re
from typing import Callable

from actionflow import llm, preferences
from actionflow.product import CORE_COMMANDS, TOOLS, DEVELOPER_TOOLS, writing_commands

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
    "proofread": ("Fix mistakes", "checkmark.circle", "blue"),
    "clarify": ("Make clearer", "text.alignleft", "purple"),
    "shorten": ("Shorten", "arrow.down.right.and.arrow.up.left", "orange"),
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
    """Shared small writing palette for AppKit and Tk."""

    def __init__(self, text: str, commands: dict, suggestions=None,
                 prompt_for: Callable | None = None) -> None:
        self.text = text
        self.demo = False
        self.commands = writing_commands(commands)
        self.prompt_for = prompt_for
        self.preferences = preferences.load()

    def refresh(self) -> None:
        self.preferences = preferences.load()

    def _item(self, name: str, cmd: dict) -> dict:
        title, icon, tint = COMMAND_META.get(name, (name.replace('_', ' ').title(), 'command', 'gray'))
        if cmd.get('_personal'):
            title = name.replace('personal_', '').replace('_', ' ').title()
        if name == 'trans':
            title = 'Translate to ' + self.preferences['language']
        return {'id':name, 'title':title, 'icon':icon, 'tint':tint,
                'subtitle':command_subtitle(cmd),
                'tag':'Last used' if self.preferences['last_action'] == name else ''}

    @staticmethod
    def _nav(name: str, title: str, subtitle: str = '', icon: str = 'gearshape') -> dict:
        return {'id':name, 'title':title, 'subtitle':subtitle, 'icon':icon, 'tint':'gray'}

    def _custom_item(self, query: str) -> dict:
        return {'id':'custom', 'title':query, 'instruction':query, 'icon':'sparkles',
                'tint':'purple', 'subtitle':'Apply your instruction to the selected text'}

    def _settings(self) -> list[dict]:
        p = self.preferences
        rows = [self._nav('languages', 'Translation language', p['language'], 'globe'),
                self._nav('providers', 'Connect AI' if not llm.ready else 'Change AI provider',
                          llm.provider if llm.ready else 'Bring your API key or use a local model', 'network'),
                self._nav('toggle:show_tools', 'Additional tools', 'On' if p['show_tools'] else 'Off'),
                self._nav('toggle:developer_tools', 'Developer tools', 'On' if p['developer_tools'] else 'Off'),
                self._nav('saved', 'Manage saved actions', f"{len(p['saved_actions'])} saved", 'star'),
                self._nav('toggle:metrics_enabled', 'Local usage counts', 'On · never sent anywhere' if p['metrics_enabled'] else 'Off · no text collected'),
                self._nav('metrics', 'View local counts'),
                self._nav('help', 'How to use ActionFlow', 'Shortcuts, privacy and safe undo', 'questionmark.circle')]
        return rows

    def items(self, query: str, submenu: str | None) -> list[dict]:
        query = query.strip()
        if submenu == 'trans':
            langs = [l for l in TRANS_LANGS if query.lower() in l[0].lower() or query.lower() == l[1].lower()]
            rows = [self._nav('language:' + name, name, flag + '  ' + code, 'globe') for name, code, flag in langs]
            if query and not langs: rows.append(self._nav('language:' + query, query, 'Use this language', 'globe'))
            return rows
        if submenu == 'tone':
            return [self._nav('tone:' + st, st.title(), 'Rewrite in this tone', 'theatermasks')
                    for st in TONE_STYLES if query.lower() in st.lower()]
        if submenu == 'settings': rows = self._settings()
        elif submenu == 'providers':
            rows = [self._nav('provider:' + name, info.label.split(' —')[0],
                             'Runs on your machine' if info.local else 'API key required', 'network')
                    for name, info in llm.PROVIDERS.items()]
        elif submenu == 'saved':
            rows = [self._nav('delete:' + x['id'], x['name'], 'Remove saved action', 'trash')
                    for x in self.preferences['saved_actions']]
        elif submenu in ('tools','developer'):
            names = TOOLS if submenu == 'tools' else DEVELOPER_TOOLS
            rows = [self._item(n,self.commands[n]) for n in names if n in self.commands]
        else:
            rows = [self._item(n, self.commands[n]) for n in CORE_COMMANDS]
            for x in self.preferences['saved_actions']:
                rows.append({'id':'saved:' + x['id'], 'title':x['name'], 'subtitle':x['instruction'],
                             'instruction':x['instruction'], 'icon':'star.fill', 'tint':'yellow'})
            # Existing personal commands stay accessible as favorites.
            rows += [self._item(n,c) for n,c in self.commands.items() if c.get('_personal')]
            if not query:
                if self.preferences['show_tools']: rows.append(self._nav('tools','Additional tools',icon='wrench'))
                if self.preferences['developer_tools']: rows.append(self._nav('developer','Developer tools',icon='chevron.left.forwardslash.chevron.right'))
                rows.append(self._nav('settings','Settings', 'Language, saved actions and AI connection'))
                if not llm.ready:
                    rows.insert(0,self._nav('providers','Connect AI to get started','Choose a provider or a local model','sparkles'))
                return rows
            scored = [(fuzzy_score(query, r['id'], {'description':r.get('subtitle','')}, r['title']), r) for r in rows]
            matches = [r for score,r in sorted(scored,key=lambda x:-x[0]) if score]
            custom = self._custom_item(query)
            return ([custom] + matches) if ' ' in query or not matches else (matches + [custom])
        return [r for r in rows if not query or query.lower() in (r['title'] + ' ' + r.get('subtitle','')).lower()]

    def _stream_action(self, cmd_name: str, cmd_config: dict, payload: str,
                       title: str, icon: str = 'sparkles', tint: str = 'purple') -> dict:
        if not llm.ready:
            return {'kind':'submenu', 'id':'providers', 'title':'Connect AI to get started'}
        try:
            prompt, model = self.prompt_for(cmd_name, cmd_config, payload)
        except ValueError as exc:
            return {'kind':'message','title':title,'text':str(exc)}
        return {'kind':'stream','title':title,'icon':icon,'tint':tint,'cmd_name':cmd_name,
                'cmd_config':cmd_config, 'factory':lambda:llm.stream(prompt,model)}

    def activate(self, item: dict, query: str) -> dict:
        key = item['id']
        menus = {'settings':'Settings', 'languages':'Choose a translation language',
                 'providers':'Connect AI', 'saved':'Remove a saved action',
                 'tools':'Additional tools', 'developer':'Developer tools', 'tone':'Choose a tone'}
        if key in menus:
            return {'kind':'submenu','id':'trans' if key == 'languages' else key,'title':menus[key]}
        if key.startswith('toggle:'):
            name = key.split(':',1)[1]
            if name not in {'show_tools','developer_tools','metrics_enabled'}: raise ValueError('Unknown setting')
            self.preferences = preferences.update(**{name:not self.preferences[name]})
            return {'kind':'reload'}
        if key.startswith('language:'):
            language = key.split(':',1)[1]
            from actionflow.prompts import TRANS_LANG_RE
            if not TRANS_LANG_RE.fullmatch(language + ':'):
                return {'kind':'message','title':'Language','text':'Enter a language name, for example Russian or Brazilian Portuguese.'}
            self.preferences = preferences.update(language=language)
            return {'kind':'home'}
        if key.startswith('provider:'): return {'kind':'connect','provider':key.split(':',1)[1]}
        if key.startswith('delete:'): return {'kind':'delete','id':key.split(':',1)[1],'title':item['title']}
        if key == 'metrics':
            counts = self.preferences['metrics']
            return {'kind':'message','title':'Local usage counts',
                    'text':'Only aggregate counters. No text or app names. Nothing is uploaded.\n\n' +
                           ('\n'.join(f'{k}: {v}' for k,v in sorted(counts.items())) or 'No counts collected. Enable them in Settings if you want to evaluate your usage.')}
        if key == 'help':
            return {'kind':'message','title':'Write, select, improve',
                    'text':'1. Select text in any app and press Ctrl+Alt+X.\n2. Choose an action or type your own instruction.\n3. Review the changes, then replace or copy.\n\nSave a custom instruction with Save action. It never stores the selected text.\n\nTo undo: select the exact inserted result in the source app, then press Ctrl+Alt+Z.\n\nSelected text is sent only when you run an AI action, to the provider you connect. Local models keep processing on your machine. Replacements use plain text; compare formatting before applying.'}
        if key == 'custom' or key.startswith('saved:'):
            return self._stream_action('custom', {'instruction':item['instruction'],'llm_required':True},self.text,item.get('title',item['instruction']))
        if key == 'trans':
            return self._stream_action('trans',self.commands['trans'],self.preferences['language'] + ': ' + self.text,item.get('title','Translate'),'globe','teal')
        if key.startswith('tone:'):
            return self._stream_action('tone',self.commands.get('tone',{}),key[5:] + ': ' + self.text,item.get('title',key))
        cmd = self.commands.get(key,{})
        if key == 'polite' and self.text.strip().lower() in cmd.get('phrases',{}):
            return {'kind':'run'}
        if cmd.get('llm_required') or cmd.get('_personal') or key == 'polite':
            return self._stream_action(key,cmd,self.text,item.get('title',key),item.get('icon','sparkles'),item.get('tint','purple'))
        return {'kind':'run'}

    def refine(self, result_text: str, instruction: str) -> dict:
        action = self._stream_action('custom',{'instruction':instruction,'llm_required':True},result_text,'Refine result')
        # A refinement is contextual; do not save it as if it reproduced the original transform.
        action['refinement'] = True
        return action

    def record(self, event: str, action: str = '') -> None:
        if not self.demo:
            preferences.record(event,action)

    def remember(self, spec: dict) -> None:
        try:
            self.preferences = preferences.update(last_action=spec.get('cmd_name',''))
        except OSError:
            pass  # Saving a preference must not prevent accepting text.

    def save(self, spec: dict, name: str) -> dict:
        if spec.get('refinement') or not spec.get('cmd_config',{}).get('instruction'):
            raise ValueError('Only a standalone custom instruction can be saved')
        result = preferences.save_action(name,spec['cmd_config']['instruction'])
        self.refresh()
        return result
