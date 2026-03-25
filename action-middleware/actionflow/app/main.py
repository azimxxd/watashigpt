from __future__ import annotations

import argparse

from .launcher import DesktopAppLauncher
from .startup_logging import STARTUP_LOG_PATH, get_startup_logger, log_startup_exception
from .runtime import install_systemd_service, main, show_history


def run() -> None:
    logger = get_startup_logger()
    parser = argparse.ArgumentParser(description="ActionFlow by WatashiGPT")
    parser.add_argument("--install", action="store_true", help="Install as a systemd service")
    parser.add_argument("--history", action="store_true", help="Browse last 50 history entries")
    parser.add_argument("--grep", type=str, default=None, help="Filter history by command name (use with --history)")
    parser.add_argument("--mock-llm", action="store_true", help="Run LLM commands in explicit mock mode")
    parser.add_argument("--debug-console", action="store_true", help="Run the legacy console runtime instead of GUI mode")
    parser.add_argument("--banner", action="store_true", help="Keep the full ASCII banner permanently in debug console mode")
    parser.add_argument("--no-tray", action="store_true", help="Disable system tray icon in debug console mode")
    args = parser.parse_args()

    try:
        logger.info(
            "Entry run: install=%s history=%s debug_console=%s mock=%s",
            args.install,
            args.history,
            args.debug_console,
            args.mock_llm,
        )
        if args.install:
            install_systemd_service()
        elif args.history:
            show_history(grep_filter=args.grep)
        elif args.debug_console:
            logger.info("Starting legacy debug console runtime")
            main(keep_banner=args.banner, no_tray=args.no_tray, force_mock_llm=args.mock_llm)
        else:
            logger.info("Starting normal GUI/tray launcher")
            launcher = DesktopAppLauncher(debug_console=False, force_mock_llm=args.mock_llm)
            launcher.run()
    except Exception as exc:
        log_startup_exception("Application entrypoint failed", exc)
        raise SystemExit(f"Startup failed. See {STARTUP_LOG_PATH}")


if __name__ == "__main__":
    run()
