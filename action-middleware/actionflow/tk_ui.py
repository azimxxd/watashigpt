"""Tk command picker + result popup — the Linux UI (macOS uses mac_ui.py)."""

from __future__ import annotations

import sys

from actionflow import llm

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


class WritingPalette:
    """Linux counterpart of the native writing palette, sharing its controller."""

    def __init__(self, controller, context='', status='', copy_text=None):
        import queue
        self.controller = controller
        self.copy_text = copy_text
        self.outcome = None
        self._stream_spec = None
        self._stream_text = ''
        self._stream_item = None
        self._stream_started = 0
        self._token = 0
        self._queue = queue.Queue()
        self._running = False
        self._closed = False
        self._submenu = None
        self._state = 'list'
        self._mode = 'result'
        self.win = tk.Toplevel(get_root())
        self.win.withdraw()
        self.win.title('ActionFlow')
        self.win.geometry('740x540')
        self.win.minsize(650,440)
        self.win.attributes('-topmost', True)
        self.win.protocol('WM_DELETE_WINDOW', lambda:self.close(None))
        header=tk.Frame(self.win)
        header.pack(fill='x')
        tk.Label(header,text=context,anchor='w',padx=16,pady=8).pack(side='left',fill='x',expand=True)
        self.language_button=tk.Button(header,text='Language: ' + controller.preferences['language'],command=self.choose_language)
        self.language_button.pack(side='right',padx=12)
        self.query = tk.StringVar()
        self.entry = tk.Entry(self.win,textvariable=self.query,font=('TkDefaultFont',15))
        self.entry.pack(fill='x',padx=16,pady=8)
        self.hint = tk.Label(self.win,text='Choose an action or type your own instruction',anchor='w')
        self.hint.pack(fill='x',padx=16)
        self.listbox = tk.Listbox(self.win,font=('TkDefaultFont',13),activestyle='dotbox',exportselection=False)
        self.listbox.pack(fill='both',expand=True,padx=16,pady=8)
        self.preview = tk.Text(self.win,wrap='word',font=('TkDefaultFont',12),padx=12,pady=12,state='disabled')
        self.preview.tag_configure('delete',foreground='#b91c1c',overstrike=True)
        self.preview.tag_configure('insert',background='#dcfce7',foreground='#14532d')
        self.toolbar = tk.Frame(self.win)
        self.toolbar.pack(fill='x',padx=12,pady=8)
        self.buttons=[]
        for title,fn in [('Result',lambda:self.render('result')),('Changes',lambda:self.render('changes')),
                         ('Original',lambda:self.render('original')),('Save action',self.save),
                         ('Copy',self.copy),('Replace',self.accept)]:
            b=tk.Button(self.toolbar,text=title,command=fn,state='disabled')
            b.pack(side='left',padx=3)
            self.buttons.append(b)
        self.status = tk.Label(self.win,text=status,anchor='w',padx=16,pady=8)
        self.status.pack(fill='x')
        self.query.trace_add('write',lambda *a:self.reload() if self._state == 'list' else None)
        self.listbox.bind('<Double-Button-1>',lambda e:self.activate())
        self.win.bind('<Return>',lambda e:self.enter())
        self.win.bind('<Escape>',lambda e:self.back())
        self.entry.bind('<Down>',lambda e:self.move(1))
        self.entry.bind('<Up>',lambda e:self.move(-1))
        self.win.bind('<Control-d>',lambda e:self.render('changes'))
        self.win.bind('<Control-s>',lambda e:self.save())
        self.win.bind('<Control-c>',lambda e:self.copy() if self._state == 'preview' and not self.query.get() else None)
        self.win.bind('<Tab>',self.retry)
        self.reload()

    def choose_language(self):
        if self._running: return
        if self._state == 'preview': self.back()
        self._submenu='trans'
        self.query.set('')
        self.hint.config(text='Choose a translation language')
        self.reload()

    def reload(self):
        self.language_button.config(text='Language: ' + self.controller.preferences['language'])
        self.rows = self.controller.items(self.query.get(),self._submenu)
        self.listbox.delete(0,'end')
        for row in self.rows:
            self.listbox.insert('end',row['title'] + ('   ·   ' + row['subtitle'] if row.get('subtitle') else ''))
        if self.rows: self.listbox.selection_set(0)

    def move(self,delta):
        if not self.rows: return 'break'
        current = self.listbox.curselection()
        i = ((current[0] if current else 0) + delta) % len(self.rows)
        self.listbox.selection_clear(0,'end')
        self.listbox.selection_set(i)
        self.listbox.see(i)
        return 'break'

    def activate(self):
        from tkinter import messagebox
        from actionflow import preferences
        selected = self.listbox.curselection()
        if not selected: return
        item = self.rows[selected[0]]
        try: action = self.controller.activate(item,self.query.get())
        except (OSError,ValueError):
            messagebox.showerror('Could not save','Check access to your home folder.',parent=self.win)
            return
        kind = action['kind']
        if kind == 'submenu':
            self._submenu = action['id']
            self.query.set('')
            self.hint.config(text=action['title'] + ' · Esc to go back')
            self.reload()
        elif kind in ('reload','home'):
            if kind == 'home': self._submenu = None
            self.query.set('')
            self.reload()
        elif kind == 'stream':
            self._stream_item = item
            self.start(action)
        elif kind == 'run': self.close({'kind':'run','item':item,'query':self.query.get()})
        elif kind == 'message':
            messagebox.showinfo(action['title'],action['text'],parent=self.win)
        elif kind == 'connect': self.connect(action['provider'])
        elif kind == 'delete':
            if messagebox.askyesno('Remove action','Remove ' + action['title'] + '?',parent=self.win):
                try:
                    preferences.remove_action(action['id'])
                    self.controller.refresh()
                    self.reload()
                except OSError: messagebox.showerror('Could not save','Check access to your home folder.',parent=self.win)

    def enter(self):
        if self._running: return 'break'
        if self._state == 'list': self.activate()
        elif self._stream_text and self.query.get().strip():
            self.start(self.controller.refine(self._stream_text,self.query.get().strip()))
        elif self._stream_text: self.accept()
        return 'break'

    def start(self,spec):
        import time
        import threading
        self._stream_spec=spec
        self._stream_started=time.time()
        self._stream_text=''
        self._state='preview'
        self.query.set('')
        self.listbox.pack_forget()
        self.preview.pack(fill='both',expand=True,padx=16,pady=8,before=self.toolbar)
        self._running=True
        self._token += 1
        token=self._token
        for b in self.buttons: b.config(state='disabled')
        self.hint.config(text='Generating… · Esc to cancel')
        self.set_text('')
        def worker():
            try:
                for chunk in spec['factory']():
                    if token != self._token: return
                    self._queue.put(('chunk',token,chunk))
                self._queue.put(('done',token,None))
            except Exception:
                self._queue.put(('error',token,'Could not generate a result. Check your connection and retry with Tab.'))
        threading.Thread(target=worker,daemon=True).start()

    def set_text(self,text):
        self.preview.config(state='normal')
        self.preview.delete('1.0','end')
        self.preview.insert('end',text)
        self.preview.config(state='disabled')

    def render(self,mode):
        from actionflow import product
        if self._running or not self._stream_text: return 'break'
        self._mode=mode
        if mode == 'original': self.set_text(self.controller.text)
        elif mode == 'result': self.set_text(self._stream_text)
        else:
            self.preview.config(state='normal')
            self.preview.delete('1.0','end')
            for kind,text in product.diff_segments(self.controller.text,self._stream_text):
                self.preview.insert('end',text,kind)
            self.preview.config(state='disabled')
        return 'break'

    def drain(self):
        import queue
        from actionflow import product, connection
        while True:
            try: kind,token,data=self._queue.get_nowait()
            except queue.Empty: break
            if token != self._token: continue
            if kind == 'chunk':
                self._stream_text += data
                self.set_text(self._stream_text)
            elif kind == 'done':
                self._running=False
                self._stream_text=self._stream_text.strip()
                if not self._stream_text:
                    self.hint.config(text='Empty result · Tab to retry')
                    continue
                self.render('changes')
                for b in self.buttons: b.config(state='normal')
                spec=self._stream_spec or {}
                if not spec.get('cmd_config',{}).get('instruction') or spec.get('refinement'):
                    self.buttons[3].config(state='disabled')
                self.hint.config(text=product.change_summary(self.controller.text,self._stream_text) + ' · Red: removed · Green: added')
                self.status.config(text='Enter: replace · Type + Enter: refine · Tab: retry · Esc: back')
                self.controller.record('generated',spec.get('cmd_name',''))
            elif kind == 'connected':
                self._running=False
                try:
                    connection.finish(data)
                    self.controller.refresh()
                    self.back()
                    self._submenu=None
                    self.reload()
                    self.status.config(text='Connected · ' + llm.provider)
                except Exception:
                    self.set_text('Connection verified, but settings or key could not be saved. Check your system keyring.')
            elif kind == 'error':
                self._running=False
                self._stream_text=''
                self.set_text(data + '\n\nYour selected text has not changed.')
                self.hint.config(text='Failed · Esc to go back')
                self.controller.record('failed')
        if not self._closed: self.win.after(30,self.drain)

    def retry(self,event=None):
        if self._state == 'preview' and not self._running and self._stream_spec:
            self.start(self._stream_spec)
            return 'break'

    def accept(self):
        import time
        if self._running or not self._stream_text: return
        self.close({'kind':'replace','item':self._stream_item,'text':self._stream_text,
                    'seconds':time.time()-self._stream_started})

    def copy(self):
        if self._running or not self._stream_text: return
        try: self.copy_text(self._stream_text)
        except Exception:
            self.status.config(text='Copy failed; your result is still here')
            return
        self.controller.remember(self._stream_spec or {})
        self.controller.record('copied')
        self.close({'kind':'copied'})
        return 'break'

    def save(self):
        from tkinter import simpledialog, messagebox
        spec=self._stream_spec or {}
        instruction=spec.get('cmd_config',{}).get('instruction')
        if self._running or not self._stream_text or not instruction or spec.get('refinement'): return
        name=simpledialog.askstring('Save action','Name (only the instruction is saved, never your selected text):',
                                    initialvalue=instruction[:60],parent=self.win)
        if name is not None:
            try:
                self.controller.save(spec,name)
                self.status.config(text='Action saved')
            except (ValueError,OSError) as exc: messagebox.showerror('Could not save',str(exc),parent=self.win)
        return 'break'

    def connect(self,provider):
        import threading
        from tkinter import simpledialog
        from actionflow import connection
        info=llm.PROVIDERS[provider]
        key=''
        if not info.local:
            key=simpledialog.askstring('Connect AI','API key from ' + info.key_url + '\nSaved in your system keyring.',show='*',parent=self.win)
            if key is None: return
        model=simpledialog.askstring('Choose model','Keep the default or enter a model available to you:',initialvalue=info.default_model,parent=self.win)
        if model is None: return
        self._stream_spec=None
        self._stream_text=''
        self._state='preview'
        self.listbox.pack_forget()
        self.preview.pack(fill='both',expand=True,padx=16,pady=8,before=self.toolbar)
        self.set_text('Verifying connection… Esc to cancel.')
        self._running=True
        self._token += 1
        token=self._token
        def worker():
            try: self._queue.put(('connected',token,connection.validate(provider,key,model)))
            except Exception: self._queue.put(('error',token,'Could not connect. Check your key, model and network.'))
        threading.Thread(target=worker,daemon=True).start()

    def back(self):
        if self._state == 'preview':
            if self._stream_text: self.controller.record('discarded')
            self._token += 1
            self._running=False
            self._stream_text=''
            self._stream_spec=None
            self._state='list'
            self.preview.pack_forget()
            self.listbox.pack(fill='both',expand=True,padx=16,pady=8,before=self.toolbar)
            for b in self.buttons: b.config(state='disabled')
            self.query.set('')
            self.reload()
        elif self._submenu:
            self._submenu=None
            self.query.set('')
            self.reload()
        else: self.close(None)
        self.hint.config(text='Choose an action or type your own instruction') if not self._closed else None
        return 'break'

    def close(self,outcome):
        if self._closed: return
        if outcome is None and self._stream_text: self.controller.record('discarded')
        self._closed=True
        self._token += 1
        self.outcome=outcome
        self.win.destroy()

    def run(self):
        _present_popup(self.win)
        self.entry.focus_set()
        self.win.after(30,self.drain)
        self.win.wait_window()
        return self.outcome
