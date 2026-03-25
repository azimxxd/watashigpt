from actionflow.platform.base import PlatformServices


def test_platform_services_dataclass():
    services = PlatformServices(clipboard=None, hotkeys=None, windows=None, system=None)
    assert services.clipboard is None
    assert services.hotkeys is None
    assert services.windows is None
    assert services.system is None
