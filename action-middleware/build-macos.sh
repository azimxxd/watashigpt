#!/bin/sh
# Build a self-contained application. Never package the user's config or keys.
set -eu
cd "$(dirname "$0")"
[ "$(uname)" = Darwin ] || { echo "Build ActionFlow.app on macOS." >&2; exit 1; }
if command -v uv >/dev/null 2>&1; then
    [ -x .venv/bin/python ] || uv venv --python 3.12 .venv
    uv pip install --python .venv/bin/python -r requirements.txt -r macos/requirements-build.txt
else
    [ -x .venv/bin/python ] || python3 -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt -r macos/requirements-build.txt
fi
.venv/bin/python macos/make_icon.py build/icon
if [ -n "${ACTIONFLOW_CODESIGN_IDENTITY:-}" ]; then
    if ! security find-identity -v -p codesigning | grep -Fq "\"$ACTIONFLOW_CODESIGN_IDENTITY\""; then
        echo "No valid code-signing identity named $ACTIONFLOW_CODESIGN_IDENTITY was found." >&2
        exit 1
    fi
else
    echo "Warning: ad-hoc signing changes the app's macOS permission identity on every build." >&2
    echo "Set ACTIONFLOW_CODESIGN_IDENTITY to a stable certificate name for repeat builds." >&2
fi
.venv/bin/python -m PyInstaller --noconfirm --clean macos/ActionFlow.spec
printf '\nBuilt: %s/dist/ActionFlow.app\nDrag it into Applications and double-click it.\n' "$PWD"
if [ -z "${ACTIONFLOW_CODESIGN_IDENTITY:-}" ]; then
    printf 'NOTE: ad-hoc builds need Accessibility and Input Monitoring regranted after each replacement.\n'
fi
