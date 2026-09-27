"""Windowless .app entry point. Keep all writable data outside the bundle."""
import os
import sys
from pathlib import Path


def main():
    os.umask(0o077)
    log_dir = Path.home() / "Library" / "Logs" / "ActionFlow"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "app.log"
    if log_path.exists() and log_path.stat().st_size > 2_000_000:
        log_path.replace(log_dir / "app.previous.log")
    sys.stdin = open(os.devnull, "r")
    sys.stdout = sys.stderr = open(log_path, "a", buffering=1, encoding="utf-8")
    import main as application
    if "--smoke-test" in sys.argv:
        # No provider calls, hotkeys, clipboard changes or permission prompts.
        import keyring.backends.macOS
        from actionflow import config, mac_ui
        assert config.CONFIG_EXAMPLE_PATH.is_file()
        assert config.APP_DIR != config.RESOURCE_DIR
        assert keyring.backends.macOS.Keyring.priority > 0
        mac_ui.init_app()
        print("BUNDLE_SMOKE_OK", flush=True)
        return
    try:
        application.main()
    except Exception:
        import traceback
        traceback.print_exc()
        from AppKit import NSAlert
        alert = NSAlert.alloc().init()
        alert.setMessageText_("ActionFlow could not start")
        alert.setInformativeText_(f"Startup details were saved to {log_path}.")
        alert.addButtonWithTitle_("OK")
        alert.runModal()
        raise


if __name__ == "__main__":
    main()
