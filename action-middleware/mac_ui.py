# ActionFlow — native macOS interface
#
# A Spotlight/Raycast-style command palette built with AppKit (PyObjC):
#   - non-activating floating panel: the app you were typing in stays active,
#     so the result is pasted straight back without refocusing anything
#   - vibrancy, rounded corners, SF Symbols, follows light/dark mode
#   - search field doubles as a free-form instruction ("make it shorter")
#   - streaming preview for LLM results: ↵ replace, ⇥ retry, type to refine
#
# The palette is UI only. A controller object (see main.py) supplies the items
# and decides what each one does:
#
#   controller.items(query, submenu)  -> list[dict]   (see _Row for keys)
#   controller.activate(item, query)  -> dict:
#       {"kind": "run"}                         close and run the command
#       {"kind": "submenu", "id": str, "title": str}
#       {"kind": "stream", "title": str, "icon": str, "tint": str,
#        "factory": callable -> iterator[str]}  preview with streamed text
#       {"kind": "message", "title": str, "text": str}  inline notice
#   controller.refine(result_text, instruction) -> "stream" dict (as above)
#
# All methods here must be called on the main thread.

from __future__ import annotations

import queue
import threading
import time

import objc
import Quartz  # noqa: F401 — registers CGColorRef so NSColor.CGColor() bridges cleanly
from AppKit import (
    NSApplication, NSApplicationActivationPolicyAccessory, NSBezierPath,
    NSColor, NSDate, NSDefaultRunLoopMode, NSEvent, NSFont, NSFontWeightMedium,
    NSFontWeightRegular, NSFontWeightSemibold, NSImage, NSImageSymbolConfiguration,
    NSImageView, NSMakeRect, NSPanel, NSProgressIndicator, NSScreen, NSScrollView,
    NSTableColumn, NSTableRowView, NSTableView, NSTextField, NSTextView, NSView,
    NSVisualEffectView, NSAnimationContext, NSPasteboard, NSPasteboardTypeString,
    NSAttributedString, NSForegroundColorAttributeName, NSFontAttributeName,
    NSMutableParagraphStyle, NSParagraphStyleAttributeName, NSMouseInRect,
    NSLineBreakByTruncatingTail,
)
from Foundation import NSObject

# ── Constants (numeric where PyObjC doesn't export the enum name) ──
_STYLE_BORDERLESS = 0
_STYLE_NONACTIVATING = 1 << 7
_STYLE_FULLSIZE = 1 << 15
_BACKING_BUFFERED = 2
_LEVEL_POPUP = 101                       # NSPopUpMenuWindowLevel
_COLLECTION = (1 << 0) | (1 << 8)        # canJoinAllSpaces | fullScreenAuxiliary
_MATERIAL_POPOVER = 6
_MATERIAL_HUD = 13
_BLENDING_BEHIND = 0
_STATE_ACTIVE = 1
_EVENT_KEYDOWN_MASK = 1 << 10
_EVENT_ANY = 0xFFFFFFFFFFFFFFFF
_MOD_CMD = 1 << 20
_MOD_SHIFT = 1 << 17
_MOD_OPT = 1 << 19

_KEY_RETURN, _KEY_ENTER, _KEY_TAB, _KEY_ESC = 36, 76, 48, 53
_KEY_UP, _KEY_DOWN, _KEY_C = 126, 125, 8
_DIGIT_KEYS = {18: 1, 19: 2, 20: 3, 21: 4, 23: 5, 22: 6, 26: 7, 28: 8, 25: 9}

W, H = 700, 470
_SEARCH_H, _CONTEXT_H, _FOOTER_H = 58, 24, 38
_ROW_H, _HEADER_H = 42, 28

_TINTS = {
    "purple": "systemPurpleColor", "blue": "systemBlueColor",
    "orange": "systemOrangeColor", "green": "systemGreenColor",
    "pink": "systemPinkColor", "teal": "systemTealColor",
    "gray": "systemGrayColor", "indigo": "systemIndigoColor",
    "red": "systemRedColor", "yellow": "systemYellowColor",
}


def _tint(name: str):
    return getattr(NSColor, _TINTS.get(name, "systemGrayColor"))()


def _symbol(name: str, size: float = 13, weight=NSFontWeightMedium):
    image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if image is None:
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_("command", None)
    config = NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, weight)
    return image.imageWithSymbolConfiguration_(config)


def _label(text: str, size: float = 13, weight=NSFontWeightRegular, color=None,
           frame=None) -> NSTextField:
    field = NSTextField.labelWithString_(text)
    field.setFont_(NSFont.systemFontOfSize_weight_(size, weight))
    field.setTextColor_(color or NSColor.labelColor())
    field.setLineBreakMode_(NSLineBreakByTruncatingTail)
    field.cell().setTruncatesLastVisibleLine_(True)
    if frame is not None:
        field.setFrame_(frame)
    return field


def _layer_view(frame, color=None, radius: float = 0) -> NSView:
    view = NSView.alloc().initWithFrame_(frame)
    view.setWantsLayer_(True)
    if color is not None:
        view.layer().setBackgroundColor_(color.CGColor())
    if radius:
        view.layer().setCornerRadius_(radius)
    return view


# ============================================================
# App bootstrap / event pumping
# ============================================================

_app_ready = False


def init_app() -> None:
    """Create the NSApplication as a menu-bar-less accessory app (no Dock icon)."""
    global _app_ready
    if _app_ready:
        return
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    app.finishLaunching()
    _app_ready = True


def pump(timeout: float = 0.0) -> None:
    """Process pending Cocoa events (menu bar clicks, window events, timers),
    waiting up to `timeout` seconds for the first one."""
    app = NSApplication.sharedApplication()
    until = NSDate.dateWithTimeIntervalSinceNow_(timeout)
    while True:
        event = app.nextEventMatchingMask_untilDate_inMode_dequeue_(
            _EVENT_ANY, until, NSDefaultRunLoopMode, True)
        if event is None:
            break
        app.sendEvent_(event)
        until = NSDate.distantPast()
    app.updateWindows()


# ============================================================
# Cocoa subclasses
# ============================================================

class AFPanel(NSPanel):
    """Borderless panels refuse key status by default — we need typing."""

    def canBecomeKeyWindow(self):
        return True

    def canBecomeMainWindow(self):
        return False


class AFRowView(NSTableRowView):
    """Subtle rounded highlight instead of the blue system selection."""

    def drawSelectionInRect_(self, rect):
        bounds = NSMakeRect(6, 1, self.bounds().size.width - 12, self.bounds().size.height - 2)
        NSColor.labelColor().colorWithAlphaComponent_(0.10).setFill()
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(bounds, 8, 8).fill()

    def isEmphasized(self):
        return False


class AFPaletteDelegate(NSObject):
    """Table data source/delegate + search field + window delegate."""

    def initWithPalette_(self, palette):
        self = objc.super(AFPaletteDelegate, self).init()
        if self is None:
            return None
        self.palette = palette
        return self

    # table
    def numberOfRowsInTableView_(self, table):
        return len(self.palette.rows)

    def tableView_heightOfRow_(self, table, row):
        return _HEADER_H if self.palette.rows[row].get("header") else _ROW_H

    def tableView_shouldSelectRow_(self, table, row):
        return not self.palette.rows[row].get("header")

    def tableView_rowViewForRow_(self, table, row):
        return AFRowView.alloc().init()

    def tableView_viewForTableColumn_row_(self, table, column, row):
        return self.palette.make_row_view(row)

    def rowClicked_(self, sender):
        row = sender.clickedRow()
        if 0 <= row < len(self.palette.rows) and not self.palette.rows[row].get("header"):
            self.palette.select_row(row)
            self.palette.activate_selected()

    # search field
    def controlTextDidChange_(self, notification):
        self.palette.query_changed()

    # window
    def windowDidResignKey_(self, notification):
        self.palette.lost_focus()


# ============================================================
# Palette
# ============================================================

class CommandPalette:
    """Blocking palette. run() returns an outcome dict or None when cancelled:

        {"kind": "run", "item": item, "query": str}
        {"kind": "replace", "item": item, "text": str, "seconds": float}
        {"kind": "copied"}
    """

    def __init__(self, controller, context: str = "", status: str = "",
                 result: tuple[str, str] | None = None) -> None:
        init_app()
        self.controller = controller
        self.context_text = context
        self.status_text = status
        self.rows: list[dict] = []
        self.submenu: dict | None = None      # {"id", "title"} while in a submenu
        self.state = "list"                   # "list" | "preview"
        self.outcome = None
        self.done = False
        self._ui_queue: queue.Queue = queue.Queue()
        self._stream_token = 0
        self._stream_item = None
        self._stream_spec = None
        self._stream_text = ""
        self._stream_started = 0.0
        self._streaming = False
        self._result_mode = result is not None
        self._opened_at = time.time()

        self.delegate = AFPaletteDelegate.alloc().initWithPalette_(self)
        self._build_window()
        if result is not None:
            title, text = result
            self._enter_preview({"title": title, "icon": "doc.text", "tint": "blue"})
            self._set_preview_text(text)
            self._finish_stream(static=True)
        else:
            self.reload()

    # ── Window construction ──────────────────────────────────

    def _build_window(self) -> None:
        panel = AFPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, W, H),
            _STYLE_BORDERLESS | _STYLE_NONACTIVATING | _STYLE_FULLSIZE,
            _BACKING_BUFFERED, False)
        panel.setLevel_(_LEVEL_POPUP)
        panel.setCollectionBehavior_(_COLLECTION)
        panel.setFloatingPanel_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setBecomesKeyOnlyIfNeeded_(False)
        panel.setOpaque_(False)
        panel.setBackgroundColor_(NSColor.clearColor())
        panel.setHasShadow_(True)
        panel.setMovableByWindowBackground_(True)
        panel.setReleasedWhenClosed_(False)
        panel.setDelegate_(self.delegate)
        self.panel = panel

        root = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, W, H))
        root.setMaterial_(_MATERIAL_POPOVER)
        root.setBlendingMode_(_BLENDING_BEHIND)
        root.setState_(_STATE_ACTIVE)
        root.setWantsLayer_(True)
        root.layer().setCornerRadius_(16)
        root.layer().setMasksToBounds_(True)
        root.layer().setBorderWidth_(0.5)
        root.layer().setBorderColor_(NSColor.separatorColor().CGColor())
        panel.setContentView_(root)
        self.root = root

        # Search row
        top = H - _SEARCH_H
        self.search_icon = NSImageView.alloc().initWithFrame_(NSMakeRect(20, top + 18, 22, 22))
        self.search_icon.setImage_(_symbol("magnifyingglass", 17))
        self.search_icon.setContentTintColor_(NSColor.secondaryLabelColor())
        root.addSubview_(self.search_icon)

        field = NSTextField.alloc().initWithFrame_(NSMakeRect(52, top + 15, W - 110, 28))
        field.setBezeled_(False)
        field.setBordered_(False)
        field.setDrawsBackground_(False)
        field.setFocusRingType_(1)  # none
        field.setFont_(NSFont.systemFontOfSize_weight_(20, NSFontWeightRegular))
        field.setTextColor_(NSColor.labelColor())
        field.cell().setUsesSingleLineMode_(True)
        field.cell().setScrollable_(True)
        field.setDelegate_(self.delegate)
        root.addSubview_(field)
        self.field = field
        self._set_placeholder("Search commands or type an instruction…")

        self.spinner = NSProgressIndicator.alloc().initWithFrame_(NSMakeRect(W - 44, top + 20, 18, 18))
        self.spinner.setStyle_(1)        # spinning
        self.spinner.setControlSize_(1)  # small
        self.spinner.setDisplayedWhenStopped_(False)
        root.addSubview_(self.spinner)

        # Context line (what was selected, where)
        ctx_y = top - _CONTEXT_H
        self.context_label = _label(self.context_text, 12, color=NSColor.secondaryLabelColor(),
                                    frame=NSMakeRect(22, ctx_y + 4, W - 44, 16))
        root.addSubview_(self.context_label)
        root.addSubview_(_layer_view(NSMakeRect(0, ctx_y - 1, W, 1),
                                     NSColor.separatorColor().colorWithAlphaComponent_(0.6)))

        # Content area: list or preview (swapped in place)
        content_frame = NSMakeRect(0, _FOOTER_H + 1, W, ctx_y - _FOOTER_H - 2)
        self.content_frame = content_frame

        table = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, W, content_frame.size.height))
        column = NSTableColumn.alloc().initWithIdentifier_("main")
        column.setWidth_(W - 16)
        table.addTableColumn_(column)
        table.setHeaderView_(None)
        table.setBackgroundColor_(NSColor.clearColor())
        table.setIntercellSpacing_((0, 0))
        table.setGridStyleMask_(0)
        table.setFocusRingType_(1)
        table.setRefusesFirstResponder_(True)
        table.setDataSource_(self.delegate)
        table.setDelegate_(self.delegate)
        table.setTarget_(self.delegate)
        table.setAction_("rowClicked:")
        try:
            table.setStyle_(4)  # NSTableViewStylePlain (no automatic insets)
        except Exception:
            pass
        self.table = table

        list_scroll = NSScrollView.alloc().initWithFrame_(content_frame)
        list_scroll.setDocumentView_(table)
        list_scroll.setDrawsBackground_(False)
        list_scroll.setHasVerticalScroller_(True)
        list_scroll.setAutohidesScrollers_(True)
        list_scroll.setScrollerStyle_(1)  # overlay
        list_scroll.setBorderType_(0)
        list_scroll.contentView().setDrawsBackground_(False)
        list_scroll.setContentInsets_((6, 0, 6, 0))
        root.addSubview_(list_scroll)
        self.list_scroll = list_scroll

        # Preview (hidden until a streaming command runs)
        preview = NSView.alloc().initWithFrame_(content_frame)
        preview.setHidden_(True)
        ph = content_frame.size.height
        self.preview_badge = _layer_view(NSMakeRect(20, ph - 40, 26, 26), radius=7)
        preview.addSubview_(self.preview_badge)
        self.preview_icon = NSImageView.alloc().initWithFrame_(NSMakeRect(24, ph - 36, 18, 18))
        preview.addSubview_(self.preview_icon)
        self.preview_title = _label("", 14, NSFontWeightSemibold,
                                    frame=NSMakeRect(56, ph - 36, W - 200, 20))
        preview.addSubview_(self.preview_title)
        self.preview_meta = _label("", 12, color=NSColor.tertiaryLabelColor(),
                                   frame=NSMakeRect(W - 220, ph - 35, 200, 18))
        self.preview_meta.setAlignment_(2)  # right
        preview.addSubview_(self.preview_meta)

        text_scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(8, 4, W - 16, ph - 52))
        text_scroll.setDrawsBackground_(False)
        text_scroll.setHasVerticalScroller_(True)
        text_scroll.setAutohidesScrollers_(True)
        text_scroll.setScrollerStyle_(1)
        text_scroll.setBorderType_(0)
        text_view = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, W - 32, ph - 52))
        text_view.setEditable_(False)
        text_view.setSelectable_(True)
        text_view.setDrawsBackground_(False)
        text_view.setRichText_(False)
        text_view.setTextContainerInset_((14, 8))
        text_view.setVerticallyResizable_(True)
        text_view.textContainer().setWidthTracksTextView_(True)
        text_scroll.setDocumentView_(text_view)
        preview.addSubview_(text_scroll)
        self.text_view = text_view
        self.text_scroll = text_scroll
        root.addSubview_(preview)
        self.preview = preview

        # Footer
        root.addSubview_(_layer_view(NSMakeRect(0, _FOOTER_H, W, 1),
                                     NSColor.separatorColor().colorWithAlphaComponent_(0.6)))
        footer_bg = _layer_view(NSMakeRect(0, 0, W, _FOOTER_H),
                                NSColor.labelColor().colorWithAlphaComponent_(0.03))
        root.addSubview_(footer_bg)
        logo = NSImageView.alloc().initWithFrame_(NSMakeRect(18, 11, 16, 16))
        logo.setImage_(_symbol("wand.and.stars", 12, NSFontWeightSemibold))
        logo.setContentTintColor_(NSColor.systemPurpleColor())
        root.addSubview_(logo)
        self.status_label = _label(self.status_text, 12, color=NSColor.secondaryLabelColor(),
                                   frame=NSMakeRect(40, 11, 280, 16))
        root.addSubview_(self.status_label)
        self.hints_view = NSView.alloc().initWithFrame_(NSMakeRect(W / 2 - 20, 0, W / 2 + 10, _FOOTER_H))
        root.addSubview_(self.hints_view)

    def _set_placeholder(self, text: str) -> None:
        attrs = {
            NSForegroundColorAttributeName: NSColor.placeholderTextColor(),
            NSFontAttributeName: NSFont.systemFontOfSize_weight_(20, NSFontWeightRegular),
        }
        self.field.setPlaceholderAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(text, attrs))

    def _set_hints(self, hints: list[tuple[str, str]]) -> None:
        """Right-aligned 'Label [key]' pairs in the footer."""
        for sub in list(self.hints_view.subviews()):
            sub.removeFromSuperview()
        x = self.hints_view.frame().size.width - 16
        for label, key in reversed(hints):
            key_label = _label(key, 11, NSFontWeightMedium, NSColor.secondaryLabelColor())
            key_label.sizeToFit()
            key_w = max(22, key_label.frame().size.width + 12)
            x -= key_w
            cap = _layer_view(NSMakeRect(x, 9, key_w, 20),
                              NSColor.labelColor().colorWithAlphaComponent_(0.08), radius=5)
            key_label.setFrame_(NSMakeRect(0, 2, key_w, 15))
            key_label.setAlignment_(1)  # center
            cap.addSubview_(key_label)
            self.hints_view.addSubview_(cap)
            text = _label(label, 12, color=NSColor.secondaryLabelColor())
            text.sizeToFit()
            text_w = text.frame().size.width
            x -= text_w + 6
            text.setFrame_(NSMakeRect(x, 11, text_w, 16))
            self.hints_view.addSubview_(text)
            x -= 16

    # ── Rows ─────────────────────────────────────────────────

    def make_row_view(self, row: int) -> NSView:
        item = self.rows[row]
        if item.get("header"):
            view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, _HEADER_H))
            view.addSubview_(_label(item["title"].upper(), 11, NSFontWeightSemibold,
                                    NSColor.tertiaryLabelColor(),
                                    frame=NSMakeRect(22, 5, W - 44, 14)))
            return view

        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, W, _ROW_H))
        tint = _tint(item.get("tint", "gray"))
        badge = _layer_view(NSMakeRect(18, 8, 26, 26), tint.colorWithAlphaComponent_(0.18), 7)
        icon = NSImageView.alloc().initWithFrame_(NSMakeRect(4, 4, 18, 18))
        icon.setImage_(_symbol(item.get("icon", "command"), 13))
        icon.setContentTintColor_(tint)
        badge.addSubview_(icon)
        view.addSubview_(badge)

        # Fixed right-hand slots so ⌘N hints line up whether or not a row has a tag
        tag = item.get("tag")
        if tag:
            tag_color = _tint(item.get("tag_tint", "purple"))
            tag_label = _label(tag, 10.5, NSFontWeightSemibold, tag_color)
            tag_label.sizeToFit()
            tag_w = tag_label.frame().size.width + 14
            pill = _layer_view(NSMakeRect(W - 30 - tag_w, 12, tag_w, 18),
                               tag_color.colorWithAlphaComponent_(0.16), 9)
            tag_label.setFrame_(NSMakeRect(0, 2, tag_w, 14))
            tag_label.setAlignment_(1)
            pill.addSubview_(tag_label)
            view.addSubview_(pill)
        right = W - 30 - 62
        shortcut = item.get("shortcut")
        if shortcut:
            hint = _label(shortcut, 12, color=NSColor.tertiaryLabelColor(),
                          frame=NSMakeRect(right - 34, 12, 34, 16))
            hint.setAlignment_(2)
            view.addSubview_(hint)
        right -= 40

        title = _label(item["title"], 13.5, NSFontWeightMedium, frame=NSMakeRect(56, 12, 200, 18))
        title.sizeToFit()
        title_w = min(title.frame().size.width + 2, 260)
        title.setFrame_(NSMakeRect(56, 12, title_w, 18))
        view.addSubview_(title)
        subtitle = item.get("subtitle", "")
        if subtitle:
            sub_x = 56 + title_w + 10
            view.addSubview_(_label(subtitle, 12.5, color=NSColor.secondaryLabelColor(),
                                    frame=NSMakeRect(sub_x, 12.5, max(0, right - sub_x - 8), 17)))
        return view

    def reload(self) -> None:
        query = str(self.field.stringValue() or "")
        self.rows = self.controller.items(query, self.submenu["id"] if self.submenu else None)
        # ⌘1…⌘9 hints on the first selectable rows
        n = 0
        for item in self.rows:
            if item.get("header"):
                continue
            n += 1
            item["shortcut"] = f"⌘{n}" if n <= 9 else ""
        self.table.reloadData()
        first = next((i for i, r in enumerate(self.rows) if not r.get("header")), -1)
        if first >= 0:
            self.select_row(first)
        if self.submenu:
            self._set_hints([("Choose", "↵"), ("Back", "esc")])
        else:
            self._set_hints([("Run", "↵"), ("Navigate", "↑↓"), ("Close", "esc")])

    def select_row(self, row: int) -> None:
        from AppKit import NSIndexSet
        self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)
        self.table.scrollRowToVisible_(row)

    def _move(self, delta: int) -> None:
        selectable = [i for i, r in enumerate(self.rows) if not r.get("header")]
        if not selectable:
            return
        current = self.table.selectedRow()
        pos = selectable.index(current) if current in selectable else -1
        self.select_row(selectable[(pos + delta) % len(selectable)])

    def _selectable_by_number(self, n: int) -> int:
        selectable = [i for i, r in enumerate(self.rows) if not r.get("header")]
        return selectable[n - 1] if 0 < n <= len(selectable) else -1

    def query_changed(self) -> None:
        if self.state == "list":
            self.reload()

    # ── Actions ──────────────────────────────────────────────

    def activate_selected(self) -> None:
        row = self.table.selectedRow()
        if row < 0 or row >= len(self.rows):
            return
        item = self.rows[row]
        if item.get("header"):
            return
        query = str(self.field.stringValue() or "")
        action = self.controller.activate(item, query)
        kind = action.get("kind")
        if kind == "run":
            self._close({"kind": "run", "item": item, "query": query})
        elif kind == "submenu":
            self.submenu = {"id": action["id"], "title": action["title"]}
            self.field.setStringValue_("")
            self._set_placeholder(action["title"])
            self.search_icon.setImage_(_symbol("chevron.left", 15))
            self.reload()
        elif kind == "stream":
            self._stream_item = item
            self._start_stream(action)
        elif kind == "message":
            self._enter_preview({"title": action["title"], "icon": "exclamationmark.triangle",
                                 "tint": "orange"})
            self._set_preview_text(action["text"])
            self._finish_stream(static=True, notice=True)

    def _enter_preview(self, spec: dict) -> None:
        self.state = "preview"
        self.list_scroll.setHidden_(True)
        self.preview.setHidden_(False)
        tint = _tint(spec.get("tint", "purple"))
        self.preview_badge.layer().setBackgroundColor_(tint.colorWithAlphaComponent_(0.18).CGColor())
        self.preview_icon.setImage_(_symbol(spec.get("icon", "sparkles"), 13))
        self.preview_icon.setContentTintColor_(tint)
        self.preview_title.setStringValue_(spec.get("title", ""))
        self.preview_meta.setStringValue_("")
        self.field.setStringValue_("")
        self.search_icon.setImage_(_symbol("sparkles", 16))
        self._set_placeholder("Refine the result… (e.g. shorter, more formal)")

    def _set_preview_text(self, text: str) -> None:
        style = NSMutableParagraphStyle.alloc().init()
        style.setLineSpacing_(3)
        attrs = {
            NSFontAttributeName: NSFont.systemFontOfSize_(14),
            NSForegroundColorAttributeName: NSColor.labelColor(),
            NSParagraphStyleAttributeName: style,
        }
        self.text_view.textStorage().setAttributedString_(
            NSAttributedString.alloc().initWithString_attributes_(text, attrs))
        self.text_view.scrollToEndOfDocument_(None)

    def _start_stream(self, spec: dict) -> None:
        self._stream_spec = spec
        self._enter_preview(spec)
        self._stream_text = ""
        self._set_preview_text("")
        self._streaming = True
        self._stream_started = time.time()
        self._stream_token += 1
        token = self._stream_token
        self.spinner.startAnimation_(None)
        self.preview_meta.setStringValue_("Generating…")
        self._set_hints([("Stop", "esc")])

        def worker() -> None:
            try:
                for chunk in spec["factory"]():
                    if token != self._stream_token:
                        return
                    self._ui_queue.put(("chunk", token, chunk))
                self._ui_queue.put(("done", token, None))
            except Exception as exc:
                self._ui_queue.put(("error", token, str(exc)))

        threading.Thread(target=worker, daemon=True, name="palette-stream").start()

    def _finish_stream(self, static: bool = False, notice: bool = False) -> None:
        self._streaming = False
        self.spinner.stopAnimation_(None)
        if notice:
            self._set_hints([("Back", "esc")])
        elif static:
            self._set_hints([("Copy", "⌘C"), ("Close", "esc")])
        else:
            seconds = time.time() - self._stream_started
            words = len(self._stream_text.split())
            self.preview_meta.setStringValue_(f"{words} words · {seconds:.1f}s")
            self._set_hints([("Replace", "↵"), ("Copy", "⌘C"), ("Retry", "⇥"), ("Back", "esc")])

    def _drain_ui_queue(self) -> None:
        dirty = False
        while True:
            try:
                kind, token, payload = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            if token != self._stream_token:
                continue
            if kind == "chunk":
                self._stream_text += payload
                dirty = True
            elif kind == "done":
                self._stream_text = self._stream_text.strip()
                self._set_preview_text(self._stream_text)
                dirty = False
                self._finish_stream()
            elif kind == "error":
                self._streaming = False
                self.spinner.stopAnimation_(None)
                self.preview_meta.setStringValue_("Failed")
                self._set_preview_text(f"⚠︎  {payload}\n\nYour text was not changed.")
                self._stream_text = ""
                self._set_hints([("Retry", "⇥"), ("Back", "esc")])
        if dirty:
            self._set_preview_text(self._stream_text)

    def _copy_result(self) -> None:
        text = self._stream_text or str(self.text_view.string() or "")
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, NSPasteboardTypeString)
        self._close({"kind": "copied"})

    def _back_to_list(self) -> None:
        self._stream_token += 1  # abandon a running stream
        self._streaming = False
        self.spinner.stopAnimation_(None)
        self.state = "list"
        self.preview.setHidden_(True)
        self.list_scroll.setHidden_(False)
        self.field.setStringValue_("")
        self.search_icon.setImage_(_symbol("magnifyingglass", 17))
        self._set_placeholder("Search commands or type an instruction…")
        self.reload()

    # ── Keyboard ─────────────────────────────────────────────

    def handle_key(self, event):
        """Local key monitor. Return None to swallow the event."""
        if event.window() is not None and event.window() != self.panel:
            return event
        code = event.keyCode()
        mods = event.modifierFlags()
        cmd = bool(mods & _MOD_CMD)
        field_text = str(self.field.stringValue() or "")

        if code == _KEY_ESC:
            if self.state == "preview" and not self._result_mode:
                self._back_to_list()
            elif self.submenu:
                self.submenu = None
                self.search_icon.setImage_(_symbol("magnifyingglass", 17))
                self._set_placeholder("Search commands or type an instruction…")
                self.field.setStringValue_("")
                self.reload()
            else:
                self._close(None)
            return None

        if self.state == "list":
            if code == _KEY_UP:
                self._move(-1)
                return None
            if code == _KEY_DOWN:
                self._move(1)
                return None
            if code in (_KEY_RETURN, _KEY_ENTER):
                self.activate_selected()
                return None
            if cmd and code in _DIGIT_KEYS:
                row = self._selectable_by_number(_DIGIT_KEYS[code])
                if row >= 0:
                    self.select_row(row)
                    self.activate_selected()
                return None
            return event

        # preview
        if self._result_mode:
            if (cmd and code == _KEY_C and not field_text) or code in (_KEY_RETURN, _KEY_ENTER):
                self._copy_result()
                return None
            return event
        if self._streaming:
            return event if code not in (_KEY_RETURN, _KEY_ENTER, _KEY_TAB) else None
        if code == _KEY_TAB and self._stream_spec is not None:
            self._start_stream(self._stream_spec)
            return None
        if cmd and code == _KEY_C and not field_text and self._stream_text:
            self._copy_result()
            return None
        if code in (_KEY_RETURN, _KEY_ENTER):
            if field_text.strip() and self._stream_text:
                spec = self.controller.refine(self._stream_text, field_text.strip())
                self._start_stream(spec)
            elif self._stream_text:
                self._close({"kind": "replace", "item": self._stream_item,
                             "text": self._stream_text,
                             "seconds": time.time() - self._stream_started})
            return None
        return event

    def lost_focus(self) -> None:
        # Clicked elsewhere — behave like Spotlight and go away (ignore the
        # first moments: showing the panel can briefly shuffle key status).
        if not self.done and time.time() - self._opened_at > 0.3:
            self._close(None)

    # ── Lifecycle ────────────────────────────────────────────

    def _position(self) -> None:
        mouse = NSEvent.mouseLocation()
        screen = next((s for s in NSScreen.screens() if NSMouseInRect(mouse, s.frame(), False)),
                      NSScreen.mainScreen())
        visible = screen.visibleFrame()
        x = visible.origin.x + (visible.size.width - W) / 2
        y = visible.origin.y + visible.size.height * 0.78 - H
        self.panel.setFrameOrigin_((x, max(visible.origin.y, y)))

    def _close(self, outcome) -> None:
        if self.done:
            return
        self.done = True
        self.outcome = outcome
        self._stream_token += 1

    def run(self):
        """Show the palette and block (pumping events) until it closes."""
        self._position()
        self.panel.setAlphaValue_(0.0)
        self.panel.makeKeyAndOrderFront_(None)
        self.panel.makeFirstResponder_(self.field)
        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(0.12)
        self.panel.animator().setAlphaValue_(1.0)
        NSAnimationContext.endGrouping()

        monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            _EVENT_KEYDOWN_MASK, self.handle_key)
        try:
            while not self.done:
                pump(0.02)
                self._drain_ui_queue()
        finally:
            NSEvent.removeMonitor_(monitor)
            self.panel.orderOut_(None)
            self.panel.close()
            pump(0)
        return self.outcome


def show_result(title: str, text: str, status: str = "") -> bool:
    """Read-only palette for command output (WIKI, DEFINE, COUNT, history).
    Returns True if the user copied the text."""

    class _NoItems:
        def items(self, query, submenu):
            return []

        def activate(self, item, query):
            return {"kind": "run"}

        def refine(self, text, instruction):
            raise RuntimeError("not supported")

    palette = CommandPalette(_NoItems(), context=title, status=status, result=(title, text))
    palette.field.setEditable_(False)
    palette._set_placeholder(title)
    return (palette.run() or {}).get("kind") == "copied"
