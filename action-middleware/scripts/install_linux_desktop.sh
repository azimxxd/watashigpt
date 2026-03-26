#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DESKTOP="$ROOT/dist/linux/actionflow.desktop"
DIST_ICON="$ROOT/dist/linux/actionflow.png"
TARGET_DESKTOP="${XDG_DATA_HOME:-$HOME/.local/share}/applications/actionflow.desktop"
TARGET_ICON="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps/actionflow.png"

if [[ ! -f "$DIST_DESKTOP" ]]; then
  echo "Missing $DIST_DESKTOP"
  echo "Run scripts/build_linux.sh first."
  exit 1
fi

mkdir -p "$(dirname "$TARGET_DESKTOP")"
mkdir -p "$(dirname "$TARGET_ICON")"
cp "$DIST_DESKTOP" "$TARGET_DESKTOP"
cp "$DIST_ICON" "$TARGET_ICON"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$(dirname "$TARGET_DESKTOP")" || true
fi

echo "Installed desktop launcher:"
echo "  $TARGET_DESKTOP"
echo "Installed icon:"
echo "  $TARGET_ICON"
