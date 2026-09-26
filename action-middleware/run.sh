#!/bin/sh
# ActionFlow launcher — creates the venv on first run, then starts the app.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
    echo "Creating virtualenv..."
    if command -v uv >/dev/null 2>&1; then
        uv venv -q --python 3.12 .venv && uv pip install -q --python .venv/bin/python -r requirements.txt
    else
        python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
    fi || { echo "Setup failed"; exit 1; }
fi

if [ "$(uname)" = "Linux" ]; then
    exec sudo -E .venv/bin/python main.py "$@"
fi
exec .venv/bin/python main.py "$@"
