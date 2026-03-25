from __future__ import annotations

import threading


def start_tray_thread(enabled: bool, target) -> None:
    if enabled:
        threading.Thread(target=target, daemon=True).start()

