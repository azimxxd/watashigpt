from pathlib import Path

from actionflow.app import paths


def test_source_mode_uses_repo_config_path(monkeypatch):
    monkeypatch.setattr(paths.sys, "frozen", False, raising=False)
    assert paths.user_config_path().name == "config.yaml"
    assert "action-middleware" in str(paths.user_config_path())


def test_frozen_mode_uses_user_app_dirs(monkeypatch):
    monkeypatch.setattr(paths.sys, "frozen", True, raising=False)
    config_path = paths.user_config_path()
    runtime_log = paths.runtime_log_path()
    startup_log = paths.startup_log_path()

    assert config_path.name == "config.yaml"
    assert runtime_log.name == "actionflow.log"
    assert startup_log.name == "actionflow_startup.log"
    assert "ActionFlow" in str(config_path) or "actionflow" in str(config_path)


def test_packaging_assets_and_scripts_exist():
    root = Path(__file__).resolve().parents[1]
    expected = [
        root / "assets" / "actionflow.svg",
        root / "assets" / "actionflow.png",
        root / "assets" / "actionflow.ico",
        root / "packaging" / "ActionFlow.spec",
        root / "packaging" / "linux" / "actionflow.desktop.in",
        root / "scripts" / "build_windows.ps1",
        root / "scripts" / "build_windows.bat",
        root / "scripts" / "build_linux.sh",
        root / "scripts" / "install_linux_desktop.sh",
    ]
    for path in expected:
        assert path.exists(), f"Missing packaging asset: {path}"
