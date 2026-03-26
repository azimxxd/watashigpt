#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$ROOT/dist/linux"
APP_DIR="$ROOT/dist/ActionFlow"
APP_BIN="$APP_DIR/ActionFlow"
DESKTOP_TEMPLATE="$ROOT/packaging/linux/actionflow.desktop.in"
DESKTOP_OUT="$DIST_DIR/actionflow.desktop"
ICON_SRC="$ROOT/assets/actionflow.png"
ICON_OUT="$DIST_DIR/actionflow.png"

cd "$ROOT"

python3 scripts/generate_icons.py
python3 -m pip install pyinstaller
python3 -m PyInstaller packaging/ActionFlow.spec --noconfirm --clean

mkdir -p "$DIST_DIR"
cp "$ICON_SRC" "$ICON_OUT"

EXEC_ESCAPED="${APP_BIN//\\/\\\\}"
ICON_ESCAPED="${ICON_OUT//\\/\\\\}"
sed \
  -e "s|{{EXEC}}|$EXEC_ESCAPED|g" \
  -e "s|{{ICON}}|$ICON_ESCAPED|g" \
  "$DESKTOP_TEMPLATE" > "$DESKTOP_OUT"

chmod +x "$APP_BIN"

echo "Build complete."
echo "Binary:       $APP_BIN"
echo "Desktop file: $DESKTOP_OUT"
