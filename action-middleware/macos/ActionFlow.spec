# Build on macOS using the project's virtual environment.
import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

root = Path(SPECPATH).parent
signing_identity = os.environ.get("ACTIONFLOW_CODESIGN_IDENTITY") or None
analysis = Analysis(
    [str(root / "macos" / "launcher.py")],
    pathex=[str(root)],
    datas=[(str(root / "config.yaml.example"), ".")] + collect_data_files("dateparser"),
    hiddenimports=collect_submodules("keyring.backends") + ["Quartz", "AppKit", "ApplicationServices"],
    excludes=["tkinter", "pytest", "pyflakes"],
)
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name="ActionFlow",
          console=False, argv_emulation=False, codesign_identity=signing_identity)
collection = COLLECT(exe, analysis.binaries, analysis.datas, name="ActionFlow")
app = BUNDLE(collection, name="ActionFlow.app", bundle_identifier="com.watashigpt.actionflow",
             icon=str(root / "build" / "icon" / "ActionFlow.icns"),
             info_plist={"CFBundleDisplayName": "ActionFlow", "CFBundleShortVersionString": "1.2.0",
                         "LSUIElement": True, "NSHighResolutionCapable": True,
                         "NSAppleEventsUsageDescription": "ActionFlow works with selected text in your applications."})
