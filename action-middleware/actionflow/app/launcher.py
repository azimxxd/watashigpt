from __future__ import annotations

import queue
import sys
import threading
from typing import Any

from actionflow.app import runtime
from actionflow.app.bootstrap import BootstrapState, build_bootstrap_state
from actionflow.app.startup_logging import STARTUP_LOG_PATH, get_startup_logger, log_startup_exception
from actionflow.core.config import save_config
from actionflow.core.llm_ops import LLMSetupChoice, PROVIDER_DEFAULT_MODELS, load_llm_secrets
from actionflow.platform.autostart import configure_launch_at_startup, launch_at_startup_enabled
from actionflow.ui.command_palette import CommandPaletteDialog
from actionflow.ui.main_window import MainWindow
from actionflow.ui.qt_compat import QT_AVAILABLE, QTimer, create_application
from actionflow.ui.result_windows import ResultWindowManager
from actionflow.ui.settings_window import SettingsState, SettingsWindow, apply_settings_state, build_settings_state
from actionflow.ui.setup_dialogs import FirstRunSetupDialog
from actionflow.ui.tray import TrayController


def _show_startup_error_dialog(message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("ActionFlow Startup Error", message, parent=root)
        root.destroy()
    except Exception:
        pass


class _TkMainWindow:
    def __init__(self, launcher: "DesktopAppLauncher"):
        import tkinter as tk
        from tkinter import ttk

        self.launcher = launcher
        self.tk = tk
        self.ttk = ttk
        self.root = runtime._get_tk_root()
        self.window = tk.Toplevel(self.root)
        self.window.title("ActionFlow")
        self.window.geometry("960x680")
        self.window.protocol("WM_DELETE_WINDOW", self.window.withdraw)
        self.window.withdraw()
        self._status_var = tk.StringVar()
        self._entries: dict[str, Any] = {}
        self._text_widgets: dict[str, Any] = {}

        notebook = ttk.Notebook(self.window)
        notebook.pack(fill="both", expand=True)
        self._tabs = {
            "status": ttk.Frame(notebook),
            "settings": ttk.Frame(notebook),
            "history": ttk.Frame(notebook),
            "logs": ttk.Frame(notebook),
            "commands": ttk.Frame(notebook),
        }
        notebook.add(self._tabs["status"], text="Status")
        notebook.add(self._tabs["settings"], text="Settings")
        notebook.add(self._tabs["history"], text="History")
        notebook.add(self._tabs["logs"], text="Logs")
        notebook.add(self._tabs["commands"], text="Commands / Help")
        self._notebook = notebook

        ttk.Label(self._tabs["status"], textvariable=self._status_var, justify="left").pack(
            anchor="nw", padx=16, pady=16
        )

        self._build_settings_tab()
        self._build_text_tab("history")
        self._build_text_tab("logs")
        self._build_text_tab("commands")
        self.refresh(self.launcher.runtime_status)

    def _build_settings_tab(self) -> None:
        frame = self._tabs["settings"]
        state = self.launcher._current_settings_state()
        fields = [
            ("llm_provider", "LLM provider", state.llm_provider),
            ("llm_model", "LLM model", state.llm_model),
            ("llm_api_key", "LLM API key", state.llm_api_key),
            ("image_provider", "Image provider", state.image_provider),
            ("image_model", "Image model", state.image_model),
            ("image_api_key", "Image API key", state.image_api_key),
            ("intercept_hotkey", "Intercept hotkey", state.intercept_hotkey),
            ("undo_hotkey", "Undo hotkey", state.undo_hotkey),
            ("silent_toggle_hotkey", "Silent toggle hotkey", state.silent_toggle_hotkey),
            ("ui_mode", "UI mode", state.ui_mode),
            ("log_path", "Log path", state.log_path),
        ]
        row = 0
        for key, label, value in fields:
            self.ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=12, pady=6)
            entry = self.ttk.Entry(frame, width=56)
            entry.insert(0, value)
            if "api_key" in key:
                entry.configure(show="*")
            entry.grid(row=row, column=1, sticky="ew", padx=12, pady=6)
            self._entries[key] = entry
            row += 1

        self._launch_var = self.tk.BooleanVar(value=state.launch_at_startup)
        self._debug_var = self.tk.BooleanVar(value=state.debug_console)
        self.ttk.Checkbutton(frame, text="Launch at startup", variable=self._launch_var).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=12, pady=6
        )
        row += 1
        self.ttk.Checkbutton(frame, text="Debug console mode", variable=self._debug_var).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=12, pady=6
        )
        row += 1
        self.ttk.Button(frame, text="Save Settings", command=self._save_settings).grid(
            row=row, column=0, columnspan=2, sticky="w", padx=12, pady=12
        )
        frame.columnconfigure(1, weight=1)

    def _build_text_tab(self, name: str) -> None:
        widget = self.tk.Text(self._tabs[name], wrap="word")
        widget.pack(fill="both", expand=True, padx=12, pady=12)
        self._text_widgets[name] = widget

    def _save_settings(self) -> None:
        state = SettingsState(
            llm_provider=self._entries["llm_provider"].get().strip(),
            llm_model=self._entries["llm_model"].get().strip(),
            llm_api_key=self._entries["llm_api_key"].get().strip(),
            image_provider=self._entries["image_provider"].get().strip(),
            image_model=self._entries["image_model"].get().strip(),
            image_api_key=self._entries["image_api_key"].get().strip(),
            intercept_hotkey=self._entries["intercept_hotkey"].get().strip(),
            undo_hotkey=self._entries["undo_hotkey"].get().strip(),
            silent_toggle_hotkey=self._entries["silent_toggle_hotkey"].get().strip(),
            ui_mode=self._entries["ui_mode"].get().strip(),
            log_path=self._entries["log_path"].get().strip(),
            launch_at_startup=bool(self._launch_var.get()),
            debug_console=bool(self._debug_var.get()),
        )
        self.launcher.save_settings(state)

    def refresh(self, runtime_status: dict[str, object]) -> None:
        self._status_var.set(
            "ActionFlow background app\n\n"
            f"Runtime: {runtime_status.get('runtime_health', 'starting')}\n"
            f"Reason: {runtime_status.get('runtime_health_reason', '')}\n"
            f"LLM state: {runtime_status.get('llm_state', 'unknown')}\n"
            f"Provider: {runtime_status.get('llm_provider', '')}\n"
            f"Model: {runtime_status.get('llm_model', '')}\n"
            f"Commands loaded: {runtime_status.get('command_count', 0)}\n"
            f"Hotkeys: {runtime_status.get('hotkeys', {})}\n"
            f"Hotkeys registered: {runtime_status.get('hotkeys_registered', False)}\n"
            f"Hotkey backend: {runtime_status.get('hotkey_backend', '')}\n"
            f"Hotkey callbacks: {runtime_status.get('hotkey_callback_count', 0)}\n"
            f"Last hotkey: {runtime_status.get('last_hotkey_callback_at', '-') or '-'}\n"
            f"Last hotkey error: {runtime_status.get('last_hotkey_error', '-') or '-'}\n"
            f"Startup log: {STARTUP_LOG_PATH}"
        )
        history_lines: list[str] = []
        for entry in runtime.load_history_entries_safe(runtime.get_history_path(), limit=50):
            history_lines.append(
                f"{entry.get('ts', '?')[:19]}  {entry.get('command', '?')}  [{entry.get('status', 'success')}]"
            )
            history_lines.append(f"IN:  {str(entry.get('input', '')).replace(chr(10), ' ')[:120]}")
            history_lines.append(f"OUT: {str(entry.get('output', '')).replace(chr(10), ' ')[:120]}")
            history_lines.append("")
        self._set_text("history", "\n".join(history_lines).strip())
        try:
            logs = runtime.get_log_path().read_text(encoding="utf-8", errors="replace")
        except Exception:
            logs = ""
        self._set_text("logs", logs[-120_000:])
        commands_help = []
        for name, config in sorted(runtime.get_available_commands().items()):
            commands_help.append(f"{name}: {config.get('description', '')}")
            commands_help.append(f"  prefixes: {', '.join(config.get('prefixes', []))}")
        self._set_text("commands", "\n".join(commands_help).strip())

    def refresh_runtime(self, runtime_status: dict[str, object]) -> None:
        self.refresh(runtime_status)

    def _set_text(self, tab_name: str, text: str) -> None:
        widget = self._text_widgets[tab_name]
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.config(state="disabled")

    def show_tab(self, name: str) -> None:
        mapping = {"status": 0, "settings": 1, "history": 2, "logs": 3, "commands": 4}
        self._notebook.select(mapping.get(name, 0))
        self.window.deiconify()
        self.window.lift()
        self.window.focus_force()


class DesktopAppLauncher:
    def __init__(self, *, debug_console: bool = False, force_mock_llm: bool = False):
        self.debug_console = debug_console
        self.force_mock_llm = force_mock_llm
        self.logger = get_startup_logger()
        self.headless_test_mode = "pytest" in sys.modules
        self.use_qt = QT_AVAILABLE
        self.app = create_application() if self.use_qt else None
        self.result_windows = ResultWindowManager()
        self.runtime_status: dict[str, object] = {}
        self.bootstrap_state: BootstrapState | None = None
        self.tray: TrayController | None = None
        self.settings_window: SettingsWindow | None = None
        self.main_window: MainWindow | _TkMainWindow | None = None
        self._timer = QTimer() if self.use_qt else None
        self._setup_handlers_installed = False
        self._tk_root = None
        self._ui_action_queue: queue.Queue[callable] = queue.Queue()
        self.logger.info("DesktopAppLauncher created: qt_available=%s debug_console=%s", self.use_qt, debug_console)

    def initialize(self) -> dict[str, object]:
        self.logger.info("Launcher initialization started")
        if not self.use_qt:
            self.logger.info("Qt unavailable; using Tk + tray fallback launcher")
        self.runtime_status = runtime.initialize_background_runtime(force_mock_llm=self.force_mock_llm)
        self.logger.info("Background runtime initialized")
        self.bootstrap_state = build_bootstrap_state(
            runtime.CONFIG,
            log_path=runtime.get_log_path(),
            history_path=runtime.get_history_path(),
            force_mock=self.force_mock_llm,
        )
        self.logger.info(
            "Bootstrap state ready: qt=%s llm_needs_setup=%s",
            self.bootstrap_state.qt_available,
            self.bootstrap_state.llm_needs_setup,
        )
        self._install_setup_handlers()
        self._build_windows()
        self.logger.info("Main window created")
        self._build_tray()
        self.logger.info(
            "Tray created=%s tray_error=%s",
            bool(self.tray and self.tray.icon_created),
            getattr(self.tray, "last_error", None),
        )
        self._start_polling()
        self.logger.info("Runtime polling started")
        if self.bootstrap_state.llm_needs_setup:
            self.logger.info("Running first-run setup")
            self.run_first_run_setup()
        if self.tray is None or not self.tray.icon_created:
            self.logger.warning("Tray icon unavailable; showing main window")
            self.open_main_window()
        self.refresh_main_window()
        self.logger.info("Launcher initialization completed")
        return self.runtime_status

    def run(self, *, start_event_loop: bool = True) -> int:
        self.logger.info("Launcher run started: qt=%s start_event_loop=%s", self.use_qt, start_event_loop)
        try:
            self.initialize()
            if not start_event_loop:
                self.logger.info("Startup completed without entering event loop")
                return 0
            if self.use_qt and self.app is not None:
                self.logger.info("Entering Qt event loop")
                code = int(self.app.exec())
                self.logger.info("Qt event loop exited with code %s", code)
                return code
            return self._run_tk_loop()
        except Exception as exc:
            log_startup_exception("GUI startup failed", exc)
            _show_startup_error_dialog(
                f"ActionFlow could not start in GUI mode.\n\nSee startup log:\n{STARTUP_LOG_PATH}\n\n{exc}"
            )
            if (
                start_event_loop
                and not self.use_qt
                and not self.headless_test_mode
                and sys.stdout
                and getattr(sys.stdout, "write", None)
            ):
                self.logger.info("Falling back to legacy console runtime after GUI startup failure")
                runtime.main(force_mock_llm=self.force_mock_llm)
                return 0
            return 1

    def _run_tk_loop(self) -> int:
        if self.headless_test_mode:
            self.logger.info("Headless test mode active; skipping Tk mainloop")
            return 0
        if not runtime._TKINTER_AVAILABLE:
            raise RuntimeError("PySide6 is unavailable and tkinter fallback is not available")
        self._tk_root = runtime._get_tk_root()
        self.logger.info("Entering Tk mainloop fallback")
        self._tk_root.mainloop()
        self.logger.info("Tk mainloop exited")
        return 0

    def _install_setup_handlers(self) -> None:
        if self._setup_handlers_installed:
            return
        runtime.set_setup_handlers(
            llm_handler=self.prompt_llm_setup,
            image_handler=self.prompt_image_setup,
        )
        self._setup_handlers_installed = True
        self.logger.info("Setup handlers installed")

    def _current_settings_state(self) -> SettingsState:
        secrets = load_llm_secrets()
        llm_secret = secrets.get("llm", {}) if isinstance(secrets.get("llm", {}), dict) else {}
        image_secret = secrets.get("image", {}) if isinstance(secrets.get("image", {}), dict) else {}
        state = build_settings_state(
            runtime.CONFIG,
            llm_api_key=str(llm_secret.get("api_key", "")).strip(),
            image_api_key=str(image_secret.get("api_key", "")).strip(),
        )
        state.launch_at_startup = launch_at_startup_enabled() or state.launch_at_startup
        state.debug_console = self.debug_console or state.debug_console
        return state

    def _build_windows(self) -> None:
        if self.use_qt or self.headless_test_mode:
            self.settings_window = SettingsWindow(self._current_settings_state(), self.save_settings)
            self.main_window = MainWindow(
                self.runtime_status,
                runtime.get_available_commands(),
                runtime.get_history_path(),
                runtime.get_log_path(),
                self.settings_window,
            )
            return
        if not runtime._TKINTER_AVAILABLE:
            raise RuntimeError("PySide6 is not installed and tkinter fallback is unavailable")
        self._tk_root = runtime._get_tk_root()
        self.main_window = _TkMainWindow(self)

    def _build_tray(self) -> None:
        if self.use_qt:
            callbacks = {
                "Open": self.open_main_window,
                "Quick Command": self.open_quick_command,
                "Settings": self.open_settings,
                "History": self.open_history,
                "Logs": self.open_logs,
                "Restart Hotkeys": self.restart_hotkeys,
                "About": self.open_about,
                "Exit": self.exit_app,
            }
        else:
            callbacks = {
                "Open": lambda: self._enqueue_ui_action(self.open_main_window),
                "Quick Command": lambda: self._enqueue_ui_action(self.open_quick_command),
                "Settings": lambda: self._enqueue_ui_action(self.open_settings),
                "History": lambda: self._enqueue_ui_action(self.open_history),
                "Logs": lambda: self._enqueue_ui_action(self.open_logs),
                "Restart Hotkeys": lambda: self._enqueue_ui_action(self.restart_hotkeys),
                "About": lambda: self._enqueue_ui_action(self.open_about),
                "Exit": lambda: self._enqueue_ui_action(self.exit_app),
            }
        self.tray = TrayController(callbacks)

    def _start_polling(self) -> None:
        runtime.set_runtime_polling_active(True)
        if self.use_qt:
            assert self._timer is not None
            self._timer.timeout.connect(self.poll_runtime_events)
            self._timer.start(200)
            return
        if self.headless_test_mode:
            return
        if self._tk_root is None:
            self._tk_root = runtime._get_tk_root()

        def _tick():
            try:
                self.poll_runtime_events()
            finally:
                if self._tk_root is not None and self._tk_root.winfo_exists():
                    self._tk_root.after(200, _tick)

        self._tk_root.after(200, _tick)

    def _enqueue_ui_action(self, action) -> None:
        self._ui_action_queue.put(action)

    def poll_runtime_events(self) -> None:
        while True:
            try:
                action = self._ui_action_queue.get_nowait()
            except queue.Empty:
                break
            action()

        while True:
            picker_text = runtime.poll_picker_request()
            if picker_text is None:
                break
            if self.use_qt or self.headless_test_mode:
                self.open_command_palette(picker_text)
            else:
                runtime._handle_popup(picker_text)

        while True:
            result = runtime.poll_result_request()
            if result is None:
                break
            title, text, critical, special_ui = result
            if special_ui or critical or runtime.notifications.should_show_result_popup(critical=critical, special_ui=special_ui):
                if self.use_qt or self.headless_test_mode:
                    self.result_windows.show_result(title, text)
                else:
                    runtime._show_result_popup(title, text)
            self.refresh_main_window()

    def refresh_main_window(self) -> None:
        self.runtime_status = runtime.get_runtime_snapshot()
        if self.tray is not None:
            status_name = str(self.runtime_status.get("runtime_health", "starting"))
            status_message = str(self.runtime_status.get("runtime_health_reason", "background runtime active"))
            self.tray.update_status(status_name, status_message)
        if self.main_window is not None:
            self.main_window.refresh_runtime(self.runtime_status)

    def open_main_window(self) -> None:
        if self.main_window is not None:
            self.main_window.show_tab("status")

    def open_settings(self) -> None:
        if self.main_window is not None:
            self.main_window.show_tab("settings")

    def open_history(self) -> None:
        if self.main_window is not None:
            self.main_window.show_tab("history")

    def open_logs(self) -> None:
        if self.main_window is not None:
            self.main_window.show_tab("logs")

    def open_about(self) -> None:
        about_text = (
            f"ActionFlow / WatashiGPT\n"
            f"Version {runtime.__version__}\n\n"
            "Desktop background app MVP with tray startup, global hotkeys, "
            "GUI settings, history/log viewers, setup dialogs, and result windows.\n\n"
            f"Startup log: {STARTUP_LOG_PATH}\n"
            f"Runtime log: {runtime.get_log_path()}"
        )
        if self.use_qt or self.headless_test_mode:
            self.result_windows.show_result(
                "About ActionFlow",
                about_text,
            )
        else:
            runtime._show_result_popup(
                "About ActionFlow",
                about_text,
            )

    def open_quick_command(self) -> None:
        if self.use_qt or self.headless_test_mode:
            self.open_command_palette("")
        else:
            runtime._handle_popup("")

    def open_command_palette(self, selected_text: str) -> None:
        dialog = CommandPaletteDialog(selected_text, runtime.get_available_commands())
        accepted = True
        if hasattr(dialog, "exec"):
            accepted = bool(dialog.exec())
        result = dialog.selected_result()
        if not accepted or result is None:
            return
        threading.Thread(
            target=runtime.dispatch_picker_selection,
            args=(result.command_name, result.payload, selected_text or result.payload),
            daemon=True,
        ).start()

    def run_first_run_setup(self) -> None:
        if self.use_qt or self.headless_test_mode:
            dialog = FirstRunSetupDialog(
                title="ActionFlow First-Run Setup",
                message="Configure your LLM provider and API keys now, or skip and keep the app running in setup-required mode.",
            )
            accepted = True
            if hasattr(dialog, "exec"):
                accepted = bool(dialog.exec())
            if not accepted:
                return
            result = dialog.to_result()
            self.apply_setup_result(result.llm_choice, result.image_api_key)
            return
        choice = self.prompt_llm_setup("first run")
        if choice.action != "cancel":
            self.apply_setup_result(choice)

    def apply_setup_result(self, llm_choice: LLMSetupChoice, image_api_key: str = "") -> None:
        state = self._current_settings_state()
        if llm_choice.action == "configure":
            state.llm_provider = llm_choice.provider
            state.llm_model = llm_choice.model
            state.llm_api_key = llm_choice.api_key
        elif llm_choice.action == "mock":
            runtime.CONFIG.setdefault("llm", {})["mode"] = "mock"
        if image_api_key:
            state.image_api_key = image_api_key
        self.save_settings(state)

    def prompt_llm_setup(self, command_name: str = "") -> LLMSetupChoice:
        if self.use_qt or self.headless_test_mode:
            dialog = FirstRunSetupDialog(
                title="LLM Setup Required",
                message=f"The command '{command_name or 'LLM command'}' needs a configured provider and API key.",
            )
            accepted = True
            if hasattr(dialog, "exec"):
                accepted = bool(dialog.exec())
            if not accepted:
                return LLMSetupChoice(action="cancel")
            result = dialog.to_result()
            if result.image_api_key:
                runtime.save_secret_values(image_api_key=result.image_api_key)
            return result.llm_choice

        import tkinter.simpledialog as simpledialog

        root = runtime._get_tk_root()
        provider = simpledialog.askstring(
            "LLM Setup Required",
            f"Provider for {command_name or 'LLM command'}:",
            initialvalue=runtime.CONFIG.get("llm", {}).get("provider", "groq") or "groq",
            parent=root,
        )
        if not provider:
            return LLMSetupChoice(action="cancel")
        provider = provider.strip().lower()
        if provider == "mock":
            return LLMSetupChoice(action="mock", model=runtime.CONFIG.get("llm", {}).get("model", ""))
        api_key = simpledialog.askstring(
            "LLM Setup Required",
            f"API key for {provider}:",
            show="*",
            parent=root,
        )
        if not api_key:
            return LLMSetupChoice(action="cancel")
        model = simpledialog.askstring(
            "LLM Setup Required",
            f"Model for {provider}:",
            initialvalue=PROVIDER_DEFAULT_MODELS.get(provider, runtime.CONFIG.get("llm", {}).get("model", "")),
            parent=root,
        )
        return LLMSetupChoice(
            action="configure",
            provider=provider,
            api_key=api_key.strip(),
            model=(model or PROVIDER_DEFAULT_MODELS.get(provider, "")).strip(),
        )

    def prompt_image_setup(self, provider: str) -> str:
        if self.use_qt or self.headless_test_mode:
            dialog = FirstRunSetupDialog(
                title="Image Setup Required",
                message=f"The image provider '{provider}' needs an API key.",
            )
            accepted = True
            if hasattr(dialog, "exec"):
                accepted = bool(dialog.exec())
            if not accepted:
                return ""
            result = dialog.to_result()
            if result.llm_choice.action == "configure":
                self.apply_setup_result(result.llm_choice, result.image_api_key)
            elif result.image_api_key:
                runtime.save_secret_values(image_api_key=result.image_api_key)
            return result.image_api_key

        import tkinter.simpledialog as simpledialog

        return str(
            simpledialog.askstring(
                "Image Setup Required",
                f"API key for image provider '{provider}':",
                show="*",
                parent=runtime._get_tk_root(),
            )
            or ""
        ).strip()

    def save_settings(self, state: SettingsState) -> None:
        updated_config = apply_settings_state(runtime.CONFIG, state)
        save_config(updated_config)
        runtime.save_secret_values(
            llm_api_key=state.llm_api_key or None,
            image_api_key=state.image_api_key or None,
        )
        configure_launch_at_startup(state.launch_at_startup)
        runtime.CONFIG.clear()
        runtime.CONFIG.update(updated_config)
        self.runtime_status = runtime.reload_runtime_config()
        self.bootstrap_state = build_bootstrap_state(
            runtime.CONFIG,
            log_path=runtime.get_log_path(),
            history_path=runtime.get_history_path(),
            force_mock=self.force_mock_llm,
        )
        if self.use_qt or self.headless_test_mode:
            self.settings_window = SettingsWindow(self._current_settings_state(), self.save_settings)
            self.main_window = MainWindow(
                self.runtime_status,
                runtime.get_available_commands(),
                runtime.get_history_path(),
                runtime.get_log_path(),
                self.settings_window,
            )
        else:
            self.refresh_main_window()
        self.logger.info("Settings saved and runtime reloaded")

    def restart_hotkeys(self) -> None:
        runtime.restart_hotkeys()
        self.logger.info("Hotkeys restarted")
        self.refresh_main_window()

    def exit_app(self) -> None:
        self.logger.info("Exit requested")
        runtime._exit_event.set()
        runtime.set_runtime_polling_active(False)
        if self.tray is not None:
            self.tray.stop()
        if self.use_qt and self.app is not None and hasattr(self.app, "quit"):
            self.app.quit()
            return
        if self._tk_root is None and runtime._TKINTER_AVAILABLE:
            self._tk_root = runtime._get_tk_root()
        if self._tk_root is not None and self._tk_root.winfo_exists():
            self._tk_root.quit()
