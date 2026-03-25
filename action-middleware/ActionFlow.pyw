from actionflow.app.main import run
from actionflow.app.startup_logging import STARTUP_LOG_PATH, get_startup_logger, log_startup_exception


if __name__ == "__main__":
    try:
        get_startup_logger().info("Entered ActionFlow.pyw windowless entrypoint")
        run()
    except Exception as exc:
        log_startup_exception("Windowed launcher failed", exc)
        try:
            import tkinter as tk
            from tkinter import messagebox

            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                "ActionFlow Startup Error",
                f"ActionFlow could not start.\n\nSee startup log:\n{STARTUP_LOG_PATH}",
                parent=root,
            )
            root.destroy()
        except Exception:
            pass
