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
.venv/bin/python -m PyInstaller --noconfirm --clean macos/ActionFlow.spec
printf '\nBuilt: %s/dist/ActionFlow.app\nDrag it into Applications and double-click it.\n' "$PWD"
printf 'NOTE: a rebuilt app has a new code signature. After replacing ActionFlow.app,\n'
printf 'remove it and add it again under Privacy & Security > Accessibility (and Input\n'
printf 'Monitoring), or run:  tccutil reset Accessibility com.watashigpt.actionflow\n'
