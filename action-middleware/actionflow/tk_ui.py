"""Tk command picker + result popup — the Linux UI (macOS uses mac_ui.py)."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from actionflow import llm
from actionflow.palette import TONE_STYLES as _TONE_STYLES, TRANS_LANGS as _TRANS_LANGS

if TYPE_CHECKING:
    from actionflow.analysis import AppContext, TextAnalysis

try:
    import tkinter as tk
    from tkinter import font as tkfont
    TK_AVAILABLE = True
except ImportError:  # e.g. python3-tk not installed
    TK_AVAILABLE = False

# Tk allows one Tk() per process — every popup is a Toplevel of this hidden root.
_tk_root = None


def get_root():
    """The persistent hidden Tk root, created on first use (main thread only)."""
    global _tk_root
    if _tk_root is None or not _tk_root.winfo_exists():
        _tk_root = tk.Tk()
        _tk_root.withdraw()
    return _tk_root


def root_if_created():
    return _tk_root


def _get_tk_root():
    return get_root()


def _popup_fonts() -> tuple:
    """(regular, bold, small) monospace fonts that exist on this system."""
    family, size = "DejaVu Sans Mono", 10
    if family not in tkfont.families():
        family = "Courier"
    return (tkfont.Font(family=family, size=size),
            tkfont.Font(family=family, size=size, weight="bold"),
            tkfont.Font(family=family, size=size - 1))


def _present_popup(win) -> None:
    """Show a popup and give it keyboard focus."""
    win.deiconify()
    win.focus_force()



class CommandPicker:
    """Frameless tkinter popup for picking a command to apply to selected text."""

    BG = "#1a0a2e"
    BG_ROW = "#1a0a2e"
    BG_HOVER = "#2a1a4e"
    BG_SELECTED = "#3a2a6e"
    FG = "#e0e0e0"
    FG_DIM = "#888888"
    BORDER_COLOR = "#00d4aa"
    BADGE_FAST = "#00d4aa"
    BADGE_LLM = "#d45cff"
    BADGE_MOCK = "#d4aa00"
    SEARCH_BG = "#0e0620"
    PREVIEW_FG = "#777777"
    MIN_WIDTH = 360
    MAX_HEIGHT = 400
    ROW_HEIGHT = 28

    BADGE_STAR = "#ffd700"
    BADGE_PERSONAL = "#ff8c00"

    def __init__(self, selected_text: str, commands: dict,
                 suggestions: list[tuple[str, dict, bool]] | None = None,
                 text_analysis: "TextAnalysis | None" = None,
                 app_context: "AppContext | None" = None):
        self._text = selected_text
        self._commands = commands
        self._suggestions = suggestions
        self._text_analysis = text_analysis
        self._app_context = app_context
        self._cmd_list: list[tuple[str, dict]] = list(commands.items())
        self._filtered: list[tuple[str, dict]] = list(self._cmd_list)
        self._selected_idx = 0
        self._result: tuple | None = None  # (cmd_name, cmd_config) or None
        self._submenu: str | None = None  # "tone" or "trans" or None
        self._sub_items: list = []
        self._sub_selected = 0
        self._custom_entry = None
        self._row_widgets: list = []
        self._command_rows: dict[int, "tk.Frame"] = {}
        self._is_searching = False

        _get_tk_root()
        self._root = tk.Toplevel()
        self._root.withdraw()
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        self._root.configure(bg=self.BG, highlightbackground=self.BORDER_COLOR,
                             highlightthickness=1)

        self._font, self._font_bold, self._font_small = _popup_fonts()

        self._build_ui()
        self._position_window()
        _present_popup(self._root)
        self._search_var.set("")
        self._search_entry.focus_set()

    def _position_window(self) -> None:
        """Position popup at mouse cursor, clamped to screen edges."""
        self._root.update_idletasks()
        mx = self._root.winfo_pointerx()
        my = self._root.winfo_pointery()
        w = max(self.MIN_WIDTH, self._root.winfo_reqwidth())
        h = min(self.MAX_HEIGHT, self._root.winfo_reqheight())
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()

        x = mx + 10
        y = my + 10
        if x + w > sw:
            x = mx - w - 10
        if y + h > sh:
            y = my - h - 10
        x = max(0, x)
        y = max(0, y)
        self._root.geometry(f"{w}x{h}+{x}+{y}")

    def _build_ui(self) -> None:
        """Build the main popup layout."""
        # Header with close button
        header_frame = tk.Frame(self._root, bg=self.BG)
        header_frame.pack(fill="x")

        # Preview
        preview = self._text[:60] + ("..." if len(self._text) > 60 else "")
        tk.Label(header_frame, text=f'"{preview}"', bg=self.BG, fg=self.PREVIEW_FG,
                 font=self._font_small, anchor="w", padx=8, pady=4
                 ).pack(side="left", fill="x", expand=True)

        # Close button (✕)
        close_btn = tk.Label(header_frame, text="✕", bg=self.BG, fg=self.FG_DIM,
                            font=self._font_bold, cursor="hand2", padx=8, pady=4)
        close_btn.pack(side="right")
        close_btn.bind("<Button-1>", lambda e: self._on_escape())
        close_btn.bind("<Enter>", lambda e: close_btn.configure(fg="#ff4444"))
        close_btn.bind("<Leave>", lambda e: close_btn.configure(fg=self.FG_DIM))

        # Analysis summary line
        if self._text_analysis:
            ta = self._text_analysis
            parts = []
            if self._app_context and self._app_context.context_type != "unknown":
                parts.append(self._app_context.context_type)
            parts.append(ta.language)
            if ta.is_code:
                parts.append(f"code" + (f"({ta.code_language})" if ta.code_language else ""))
            elif not ta.is_formal:
                parts.append("informal")
            parts.append(f"{ta.length} chars")
            analysis_line = " · ".join(parts)
            tk.Label(self._root, text=analysis_line, bg=self.BG, fg="#555555",
                     font=self._font_small, anchor="w", padx=8, pady=1
                     ).pack(fill="x")

        # Separator
        tk.Frame(self._root, bg=self.BORDER_COLOR, height=1).pack(fill="x")

        # Search
        search_frame = tk.Frame(self._root, bg=self.SEARCH_BG)
        search_frame.pack(fill="x")
        tk.Label(search_frame, text="\U0001f50d", bg=self.SEARCH_BG, fg=self.FG_DIM,
                 font=self._font_small).pack(side="left", padx=(8, 2))
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._on_search())
        self._search_entry = tk.Entry(
            search_frame, textvariable=self._search_var,
            bg=self.SEARCH_BG, fg=self.FG, insertbackground=self.FG,
            font=self._font, relief="flat", bd=0,
        )
        self._search_entry.pack(fill="x", padx=(0, 8), pady=4, expand=True, side="left")

        # Separator
        tk.Frame(self._root, bg=self.BORDER_COLOR, height=1).pack(fill="x")

        # Scrollable command list
        self._canvas_frame = tk.Frame(self._root, bg=self.BG)
        self._canvas_frame.pack(fill="both", expand=True)

        self._canvas = tk.Canvas(self._canvas_frame, bg=self.BG, highlightthickness=0,
                                 bd=0)
        self._scrollbar = tk.Scrollbar(self._canvas_frame, orient="vertical",
                                       command=self._canvas.yview)
        self._inner_frame = tk.Frame(self._canvas, bg=self.BG)

        self._inner_frame.bind("<Configure>",
                               lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.create_window((0, 0), window=self._inner_frame, anchor="nw")
        self._canvas.configure(yscrollcommand=self._scrollbar.set)

        self._canvas.pack(side="left", fill="both", expand=True)
        self._scrollbar.pack(side="right", fill="y")

        self._populate_rows()

        # Bindings
        self._root.bind("<Escape>", self._on_escape)
        self._root.bind("<Return>", self._on_enter)
        self._root.bind("<Up>", self._on_up)
        self._root.bind("<Down>", self._on_down)
        self._root.bind("<FocusOut>", self._on_focus_out)
        self._root.bind("<MouseWheel>", self._on_mousewheel)
        self._root.bind("<Button-4>", lambda e: self._canvas.yview_scroll(-3, "units"))
        self._root.bind("<Button-5>", lambda e: self._canvas.yview_scroll(3, "units"))
        # Bound on the entry (runs before the Entry class binding inserts
        # the character): digits pick a command only while the search is empty.
        for i in range(1, 10):
            self._search_entry.bind(f"<Key-{i}>", self._on_number_key)

    def _populate_rows(self) -> None:
        """Fill the command list rows."""
        for w in self._row_widgets:
            w.destroy()
        self._row_widgets.clear()
        self._command_rows = {}

        if self._submenu == "tone":
            self._populate_tone_submenu()
            return
        elif self._submenu == "trans":
            self._populate_trans_submenu()
            return

        # Use smart suggestions if available and not actively searching
        if self._suggestions and not self._is_searching:
            starred = [(n, c) for n, c, s in self._suggestions if s]
            rest = [(n, c) for n, c, s in self._suggestions if not s]

            if starred:
                # "For You" header
                header = tk.Frame(self._inner_frame, bg=self.SEARCH_BG)
                header.pack(fill="x", padx=2, pady=(2, 0))
                self._row_widgets.append(header)
                tk.Label(header, text="  \u2605 For You", bg=self.SEARCH_BG,
                         fg=self.BADGE_STAR, font=self._font_bold, anchor="w",
                         padx=6, pady=3).pack(fill="x")

                for i, (name, cmd) in enumerate(starred):
                    self._add_command_row(name, cmd, i, starred=True)

                # "All Commands" header
                header2 = tk.Frame(self._inner_frame, bg=self.SEARCH_BG)
                header2.pack(fill="x", padx=2, pady=(4, 0))
                self._row_widgets.append(header2)
                tk.Label(header2, text="  All Commands", bg=self.SEARCH_BG,
                         fg=self.FG_DIM, font=self._font_bold, anchor="w",
                         padx=6, pady=3).pack(fill="x")

                items = rest
                offset = len(starred)
            else:
                items = starred + rest
                offset = 0
        else:
            items = self._filtered
            offset = 0

        for i, (name, cmd) in enumerate(items):
            self._add_command_row(name, cmd, i + offset)

        self._update_scroll_height()

    def _add_command_row(self, name: str, cmd: dict, idx: int,
                         starred: bool = False) -> None:
        """Add a single command row to the popup."""
        row = tk.Frame(self._inner_frame, bg=self.BG_ROW, cursor="hand2")
        row.pack(fill="x", padx=2, pady=1)
        self._row_widgets.append(row)
        self._command_rows[idx] = row

        is_llm = cmd.get("llm_required", False)
        is_mock_llm = is_llm and llm.MODE == "mock"
        is_personal = cmd.get("_personal", False)

        # Number
        num_label = str(idx + 1) if idx < 9 else " "
        fg_main = self.FG_DIM if is_mock_llm else self.FG
        tk.Label(row, text=num_label, bg=self.BG_ROW, fg=self.FG_DIM,
                 font=self._font_small, width=2).pack(side="left", padx=(6, 2))

        # Star indicator
        if starred:
            tk.Label(row, text="\u2605", bg=self.BG_ROW, fg=self.BADGE_STAR,
                     font=self._font_small).pack(side="left", padx=(0, 2))

        # Name
        display_name = name.replace("_", " ").title()
        if is_personal:
            display_name = name.replace("personal_", "").replace("_", " ").title()
        tk.Label(row, text=display_name, bg=self.BG_ROW, fg=fg_main,
                 font=self._font_bold, anchor="w", width=16).pack(side="left")

        # Description
        desc = cmd.get("description", "")[:30]
        tk.Label(row, text=desc, bg=self.BG_ROW, fg=self.FG_DIM,
                 font=self._font_small, anchor="w").pack(side="left", fill="x", expand=True)

        # Badge
        if is_personal:
            badge_text, badge_fg = "[ME]", self.BADGE_PERSONAL
        elif is_mock_llm:
            badge_text, badge_fg = "[MOCK]", self.BADGE_MOCK
        elif is_llm:
            badge_text, badge_fg = "[LLM]", self.BADGE_LLM
        else:
            badge_text, badge_fg = "[FAST]", self.BADGE_FAST
        tk.Label(row, text=badge_text, bg=self.BG_ROW, fg=badge_fg,
                 font=self._font_small).pack(side="right", padx=(4, 8))

        # Highlight
        if idx == self._selected_idx:
            self._set_row_bg(row, self.BG_SELECTED)

        # Mouse bindings
        row.bind("<Enter>", lambda e, r=row, j=idx: self._on_row_hover(r, j))
        row.bind("<Leave>", lambda e, r=row, j=idx: self._on_row_leave(r, j))
        row.bind("<Button-1>", lambda e, j=idx: self._on_row_click(j))
        for child in row.winfo_children():
            child.bind("<Enter>", lambda e, r=row, j=idx: self._on_row_hover(r, j))
            child.bind("<Leave>", lambda e, r=row, j=idx: self._on_row_leave(r, j))
            child.bind("<Button-1>", lambda e, j=idx: self._on_row_click(j))

    def _populate_tone_submenu(self) -> None:
        """Show the tone style picker."""
        self._sub_items = _TONE_STYLES
        self._sub_selected = 0

        # Back header
        back = tk.Frame(self._inner_frame, bg=self.SEARCH_BG, cursor="hand2")
        back.pack(fill="x", padx=2, pady=1)
        self._row_widgets.append(back)
        tk.Label(back, text="\u2190 back   Choose tone style", bg=self.SEARCH_BG,
                 fg=self.FG, font=self._font_bold, anchor="w", padx=8, pady=4
                 ).pack(fill="x")
        back.bind("<Button-1>", lambda e: self._back_to_main())
        for child in back.winfo_children():
            child.bind("<Button-1>", lambda e: self._back_to_main())

        for i, style in enumerate(self._sub_items):
            row = tk.Frame(self._inner_frame, bg=self.BG_ROW, cursor="hand2")
            row.pack(fill="x", padx=2, pady=1)
            self._row_widgets.append(row)

            fg = self.FG
            tk.Label(row, text=f"  {style.title()}", bg=self.BG_ROW, fg=fg,
                     font=self._font, anchor="w", padx=8, pady=3).pack(fill="x")

            if i == self._sub_selected:
                self._set_row_bg(row, self.BG_SELECTED)

            idx = i
            row.bind("<Enter>", lambda e, r=row, j=idx: self._on_sub_hover(r, j))
            row.bind("<Leave>", lambda e, r=row, j=idx: self._on_sub_leave(r, j))
            row.bind("<Button-1>", lambda e, j=idx: self._on_sub_click(j))
            for child in row.winfo_children():
                child.bind("<Enter>", lambda e, r=row, j=idx: self._on_sub_hover(r, j))
                child.bind("<Leave>", lambda e, r=row, j=idx: self._on_sub_leave(r, j))
                child.bind("<Button-1>", lambda e, j=idx: self._on_sub_click(j))

        self._update_scroll_height()

    def _populate_trans_submenu(self) -> None:
        """Show the language picker."""
        self._sub_items = _TRANS_LANGS
        self._sub_selected = 0

        # Back header
        back = tk.Frame(self._inner_frame, bg=self.SEARCH_BG, cursor="hand2")
        back.pack(fill="x", padx=2, pady=1)
        self._row_widgets.append(back)
        tk.Label(back, text="\u2190 back   Translate to...", bg=self.SEARCH_BG,
                 fg=self.FG, font=self._font_bold, anchor="w", padx=8, pady=4
                 ).pack(fill="x")
        back.bind("<Button-1>", lambda e: self._back_to_main())
        for child in back.winfo_children():
            child.bind("<Button-1>", lambda e: self._back_to_main())

        for i, (lang_name, code, flag) in enumerate(self._sub_items):
            row = tk.Frame(self._inner_frame, bg=self.BG_ROW, cursor="hand2")
            row.pack(fill="x", padx=2, pady=1)
            self._row_widgets.append(row)

            tk.Label(row, text=f"  {lang_name}", bg=self.BG_ROW, fg=self.FG,
                     font=self._font, anchor="w", padx=8, pady=3).pack(side="left", fill="x", expand=True)
            tk.Label(row, text=flag, bg=self.BG_ROW, font=self._font,
                     padx=8).pack(side="right")

            if i == self._sub_selected:
                self._set_row_bg(row, self.BG_SELECTED)

            idx = i
            row.bind("<Enter>", lambda e, r=row, j=idx: self._on_sub_hover(r, j))
            row.bind("<Leave>", lambda e, r=row, j=idx: self._on_sub_leave(r, j))
            row.bind("<Button-1>", lambda e, j=idx: self._on_sub_click(j))
            for child in row.winfo_children():
                child.bind("<Enter>", lambda e, r=row, j=idx: self._on_sub_hover(r, j))
                child.bind("<Leave>", lambda e, r=row, j=idx: self._on_sub_leave(r, j))
                child.bind("<Button-1>", lambda e, j=idx: self._on_sub_click(j))

        # Custom entry row
        custom_row = tk.Frame(self._inner_frame, bg=self.BG_ROW)
        custom_row.pack(fill="x", padx=2, pady=1)
        self._row_widgets.append(custom_row)
        tk.Label(custom_row, text="  + custom:", bg=self.BG_ROW, fg=self.FG_DIM,
                 font=self._font_small, padx=8).pack(side="left")
        self._custom_entry = tk.Entry(custom_row, bg=self.SEARCH_BG, fg=self.FG,
                                      insertbackground=self.FG, font=self._font_small,
                                      relief="flat", width=10)
        self._custom_entry.pack(side="left", padx=4, pady=2)
        self._custom_entry.bind("<Return>", self._on_custom_lang)

        self._update_scroll_height()

    def _update_scroll_height(self) -> None:
        """Update canvas scroll region and window height."""
        self._root.update_idletasks()
        content_h = self._inner_frame.winfo_reqheight()
        canvas_h = min(content_h, self.MAX_HEIGHT - 80)  # Leave room for preview+search
        self._canvas.configure(height=canvas_h)
        self._root.update_idletasks()
        # Reposition if needed
        w = max(self.MIN_WIDTH, self._root.winfo_reqwidth())
        h = min(self.MAX_HEIGHT, self._root.winfo_reqheight())
        self._root.geometry(f"{w}x{h}")

    def _set_row_bg(self, row: tk.Frame, bg: str) -> None:
        """Set background for a row and all its children."""
        row.configure(bg=bg)
        for child in row.winfo_children():
            try:
                child.configure(bg=bg)
            except tk.TclError:
                pass

    # ── Search ──
    def _on_search(self) -> None:
        q = self._search_var.get().lower()
        self._is_searching = bool(q)
        if not q:
            self._filtered = list(self._cmd_list)
        else:
            self._filtered = [
                (name, cmd) for name, cmd in self._cmd_list
                if q in name.lower()
                or q in cmd.get("description", "").lower()
                or any(q in kw.lower() for kw in cmd.get("keywords", []))
                or any(q in p.lower() for p in cmd.get("prefixes", []))
            ]
        self._selected_idx = 0
        self._populate_rows()

    # ── Keyboard ──
    def _on_escape(self, event=None) -> None:
        if self._submenu:
            self._back_to_main()
        else:
            self._result = None
            self._root.destroy()

    def _on_enter(self, event=None) -> None:
        if self._submenu:
            self._on_sub_click(self._sub_selected)
        elif self._filtered:
            self._select_command(self._selected_idx)

    def _on_up(self, event=None) -> None:
        if self._submenu:
            count = len(self._sub_items)
            if count > 0:
                self._sub_selected = (self._sub_selected - 1) % count
                self._populate_rows()
        else:
            count = len(self._visible_items())
            if count:
                self._selected_idx = (self._selected_idx - 1) % count
                self._populate_rows()
                self._ensure_visible()

    def _on_down(self, event=None) -> None:
        if self._submenu:
            count = len(self._sub_items)
            if count > 0:
                self._sub_selected = (self._sub_selected + 1) % count
                self._populate_rows()
        else:
            count = len(self._visible_items())
            if count:
                self._selected_idx = (self._selected_idx + 1) % count
                self._populate_rows()
                self._ensure_visible()

    def _on_number_key(self, event) -> None:
        if self._submenu or self._search_var.get():
            return
        idx = int(event.char) - 1
        if 0 <= idx < len(self._visible_items()):
            self._select_command(idx)
            return "break"  # don't also type the digit into the search box

    def _on_mousewheel(self, event) -> None:
        delta = event.delta if sys.platform == "darwin" else event.delta // 120
        self._canvas.yview_scroll(-delta, "units")

    def _on_focus_out(self, event) -> None:
        # Only close if focus left the root entirely
        try:
            if not self._root.focus_get():
                self._root.after(100, self._check_focus)
        except Exception:
            pass

    def _check_focus(self) -> None:
        try:
            if not self._root.focus_get():
                self._result = None
                self._root.destroy()
        except Exception:
            pass

    def _ensure_visible(self) -> None:
        """Scroll to keep the selected row visible."""
        widget = self._command_rows.get(self._selected_idx)
        if widget is None:
            return
        self._canvas.update_idletasks()
        y = widget.winfo_y()
        h = widget.winfo_height()
        canvas_h = self._canvas.winfo_height()
        visible_top = self._canvas.canvasy(0)
        visible_bot = visible_top + canvas_h
        if y < visible_top:
            self._canvas.yview_moveto(y / self._inner_frame.winfo_height())
        elif y + h > visible_bot:
            self._canvas.yview_moveto((y + h - canvas_h) / self._inner_frame.winfo_height())

    # ── Mouse ──
    def _on_row_hover(self, row, idx) -> None:
        if idx != self._selected_idx:
            self._set_row_bg(row, self.BG_HOVER)

    def _on_row_leave(self, row, idx) -> None:
        if idx != self._selected_idx:
            self._set_row_bg(row, self.BG_ROW)

    def _on_row_click(self, idx) -> None:
        self._select_command(idx)

    # ── Sub-menu mouse ──
    def _on_sub_hover(self, row, idx) -> None:
        if idx != self._sub_selected:
            self._set_row_bg(row, self.BG_HOVER)

    def _on_sub_leave(self, row, idx) -> None:
        if idx != self._sub_selected:
            self._set_row_bg(row, self.BG_ROW)

    def _on_sub_click(self, idx) -> None:
        if self._submenu == "tone":
            style = self._sub_items[idx]
            cmd_config = self._commands.get("tone", {})
            # Store result with style prepended to payload
            self._result = ("tone", cmd_config, f"{style}: {self._text}")
            self._root.destroy()
        elif self._submenu == "trans":
            _, code, _ = self._sub_items[idx]
            cmd_config = self._commands.get("trans", {})
            self._result = ("trans", cmd_config, f"{code}: {self._text}")
            self._root.destroy()

    def _on_custom_lang(self, event=None) -> None:
        lang = self._custom_entry.get().strip()
        if lang:
            cmd_config = self._commands.get("trans", {})
            self._result = ("trans", cmd_config, f"{lang.upper()}: {self._text}")
            self._root.destroy()

    def _back_to_main(self) -> None:
        self._submenu = None
        self._sub_items = []
        self._sub_selected = 0
        self._custom_entry = None
        self._populate_rows()
        self._search_entry.focus_set()

    # ── Selection ──
    def _visible_items(self) -> list[tuple[str, dict]]:
        """Commands in the order they are displayed (index == row number)."""
        if self._suggestions and not self._is_searching:
            starred = [(n, c) for n, c, s in self._suggestions if s]
            rest = [(n, c) for n, c, s in self._suggestions if not s]
            return starred + rest
        return self._filtered

    def _select_command(self, idx: int) -> None:
        items = self._visible_items()
        if idx >= len(items):
            return
        name, cmd = items[idx]

        # Tone submenu
        if name == "tone":
            self._submenu = "tone"
            self._populate_rows()
            return

        # Trans submenu
        if name == "trans":
            self._submenu = "trans"
            self._populate_rows()
            return

        self._result = (name, cmd, self._text)
        self._root.destroy()

    def run(self) -> tuple | None:
        """Show the popup and block until a choice is made. Returns (cmd_name, cmd_config, payload) or None."""
        try:
            self._root.wait_window(self._root)
        except Exception:
            return None
        return self._result


class ResultPopup:
    """Popup window to display command output (for commands that don't edit text).
    Has a scrollable text area, copy button, and close button."""
    BG = "#1a0a2e"
    FG = "#e0e0e0"
    FG_DIM = "#888888"
    BORDER_COLOR = "#d45cff"
    SEARCH_BG = "#0e0620"
    BTN_BG = "#3a2a6e"
    BTN_COPY_BG = "#00d4aa"
    BTN_COPY_FG = "#000000"

    def __init__(self, title: str, result_text: str, copy_fn=None):
        self._copy_fn = copy_fn
        self._title = title
        self._text = result_text

        _get_tk_root()
        self._root = tk.Toplevel()
        self._root.withdraw()
        self._root.overrideredirect(True)
        self._root.attributes("-topmost", True)
        self._root.configure(bg=self.BG, highlightbackground=self.BORDER_COLOR,
                             highlightthickness=1)

        self._font, self._font_bold, self._font_small = _popup_fonts()

        self._build_ui()

        # Position at center of screen
        self._root.update_idletasks()
        w = 500
        h = 350
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self._root.geometry(f"{w}x{h}+{x}+{y}")
        _present_popup(self._root)

    def _build_ui(self) -> None:
        # Header
        header_frame = tk.Frame(self._root, bg=self.BG)
        header_frame.pack(fill="x")

        tk.Label(header_frame, text=f"\U0001f4ac {self._title}",
                 bg=self.BG, fg=self.BORDER_COLOR,
                 font=self._font_bold, anchor="w", padx=8, pady=6).pack(side="left")

        # Close button
        close_btn = tk.Label(header_frame, text="\u2715", bg=self.BG, fg=self.FG_DIM,
                            font=self._font_bold, cursor="hand2", padx=8, pady=6)
        close_btn.pack(side="right")
        close_btn.bind("<Button-1>", lambda e: self._close())
        close_btn.bind("<Enter>", lambda e: close_btn.configure(fg="#ff4444"))
        close_btn.bind("<Leave>", lambda e: close_btn.configure(fg=self.FG_DIM))

        tk.Frame(self._root, bg=self.BORDER_COLOR, height=1).pack(fill="x")

        # Scrollable text area
        text_frame = tk.Frame(self._root, bg=self.SEARCH_BG)
        text_frame.pack(fill="both", expand=True, padx=6, pady=6)

        scrollbar = tk.Scrollbar(text_frame)
        scrollbar.pack(side="right", fill="y")

        self._text_widget = tk.Text(
            text_frame, bg=self.SEARCH_BG, fg=self.FG,
            font=self._font_small, wrap="word",
            relief="flat", bd=0,
            yscrollcommand=scrollbar.set,
            padx=8, pady=6,
        )
        self._text_widget.pack(fill="both", expand=True)
        self._text_widget.insert("1.0", self._text)
        self._text_widget.config(state="disabled")
        scrollbar.config(command=self._text_widget.yview)

        tk.Frame(self._root, bg=self.BORDER_COLOR, height=1).pack(fill="x")

        # Bottom bar with copy + close buttons
        btn_frame = tk.Frame(self._root, bg=self.BG)
        btn_frame.pack(fill="x", padx=8, pady=6)

        # Copy button
        copy_btn = tk.Label(btn_frame, text="  \U0001f4cb Copy  ", bg=self.BTN_COPY_BG,
                            fg=self.BTN_COPY_FG, font=self._font_bold,
                            cursor="hand2", padx=6, pady=3)
        copy_btn.pack(side="left", padx=(0, 4))
        copy_btn.bind("<Button-1>", lambda e: self._on_copy(copy_btn))
        copy_btn.bind("<Enter>", lambda e: copy_btn.configure(bg="#00eebb"))
        copy_btn.bind("<Leave>", lambda e: copy_btn.configure(bg=self.BTN_COPY_BG))

        # Close button
        close_btn2 = tk.Label(btn_frame, text="  Close (Esc)  ", bg=self.BTN_BG,
                              fg=self.FG, font=self._font_bold,
                              cursor="hand2", padx=6, pady=3)
        close_btn2.pack(side="right")
        close_btn2.bind("<Button-1>", lambda e: self._close())
        close_btn2.bind("<Enter>", lambda e: close_btn2.configure(bg="#4a3a7e"))
        close_btn2.bind("<Leave>", lambda e: close_btn2.configure(bg=self.BTN_BG))

        # Key bindings
        self._root.bind("<Escape>", lambda e: self._close())
        self._root.bind("<Command-c>" if sys.platform == "darwin" else "<Control-c>",
                        lambda e: self._on_copy(copy_btn))

    def _on_copy(self, btn) -> None:
        """Copy result text to clipboard."""
        if self._copy_fn:
            self._copy_fn(self._text)
        btn.configure(text="  \u2713 Copied!  ")
        self._root.after(1500, lambda: btn.configure(text="  \U0001f4cb Copy  "))

    def _close(self) -> None:
        try:
            self._root.destroy()
        except Exception:
            pass

    def run(self) -> None:
        """Show popup and block until user closes it."""
        try:
            self._root.wait_window(self._root)
        except Exception:
            pass
