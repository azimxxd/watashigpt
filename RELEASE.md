# ActionFlow Desktop MVP Release Notes

## Recommended End-User Launch Path

### Windows

- build or distribute `dist/ActionFlow/ActionFlow.exe`
- launch `ActionFlow.exe`
- the app should start as a tray/background desktop program without a console

### Linux

- build the packaged app with `scripts/build_linux.sh`
- install the launcher with `scripts/install_linux_desktop.sh`
- start ActionFlow from the desktop menu / application launcher

## Build Outputs

### Windows

- `action-middleware/dist/ActionFlow/ActionFlow.exe`

### Linux

- `action-middleware/dist/ActionFlow/ActionFlow`
- `action-middleware/dist/linux/actionflow.desktop`
- `action-middleware/dist/linux/actionflow.png`

## Runtime Data Locations

### Windows

- config: `%APPDATA%\ActionFlow\config.yaml`
- secrets: `%APPDATA%\ActionFlow\actionflow_secrets.yaml`
- runtime log: `%LOCALAPPDATA%\ActionFlow\logs\actionflow.log`
- startup log: `%LOCALAPPDATA%\ActionFlow\logs\actionflow_startup.log`
- history: `%LOCALAPPDATA%\ActionFlow\actionflow_history.jsonl`

### Linux

- config: `~/.config/actionflow/config.yaml`
- secrets: `~/.config/actionflow/actionflow_secrets.yaml`
- runtime log: `~/.local/state/actionflow/logs/actionflow.log`
- startup log: `~/.local/state/actionflow/logs/actionflow_startup.log`
- history: `~/.local/state/actionflow/actionflow_history.jsonl`

## Debug / Development

These remain available for development only:

- `python main.py --debug-console`
- `python main.py`

They are not the recommended end-user launch path.

## Remaining Gaps Before A Fully Polished Release

- no full installer / auto-updater yet
- no code signing yet
- no MSI / AppImage / Flatpak packaging yet
- platform-specific hotkey behavior can still depend on OS permissions and desktop session details
