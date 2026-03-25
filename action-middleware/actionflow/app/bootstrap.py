from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from actionflow.core.config import USER_CONFIG_PATH
from actionflow.core.llm_ops import load_llm_secrets, resolve_llm_state
from actionflow.ui.qt_compat import QT_AVAILABLE


@dataclass
class BootstrapState:
    config_path: Path
    log_path: Path
    history_path: Path
    has_user_config: bool
    llm_ready: bool
    llm_needs_setup: bool
    image_key_present: bool
    qt_available: bool


def build_bootstrap_state(config: dict, *, log_path: Path, history_path: Path, force_mock: bool = False) -> BootstrapState:
    secrets = load_llm_secrets()
    llm_resolution = resolve_llm_state(config, secrets=secrets, force_mock=force_mock)
    image_secrets = secrets.get("image", {}) if isinstance(secrets.get("image", {}), dict) else {}
    image_cfg = config.get("image_generation", {}) if isinstance(config.get("image_generation", {}), dict) else {}
    image_key_present = bool(image_cfg.get("api_key") or image_secrets.get("api_key"))
    return BootstrapState(
        config_path=USER_CONFIG_PATH,
        log_path=log_path,
        history_path=history_path,
        has_user_config=USER_CONFIG_PATH.exists(),
        llm_ready=llm_resolution.state == "ready",
        llm_needs_setup=llm_resolution.state == "needs_setup",
        image_key_present=image_key_present,
        qt_available=QT_AVAILABLE,
    )
