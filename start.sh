#!/usr/bin/env bash
# Starts CouchPad on macOS or Linux: the same steps as start.bat on Windows.
set -u
cd "$(dirname "$0")"

VENV_PY=".venv/bin/python"

if [ ! -x "$VENV_PY" ]; then
    PY=""
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 \
            && "$candidate" -c "import sys; sys.exit(sys.version_info < (3, 11))" >/dev/null 2>&1; then
            PY="$candidate"
            break
        fi
    done
    if [ -z "$PY" ]; then
        echo "Python 3.11 or newer was not found. Install it from https://www.python.org/downloads/"
        exit 1
    fi
    echo "Creating virtual environment in .venv ..."
    "$PY" -m venv .venv || { echo "Failed to create the virtual environment."; exit 1; }
fi

# Exit code 3 means an update was installed: start the new copy of this script.
if [ -z "${PM_UPDATED:-}" ]; then
    "$VENV_PY" -m server.update "$@"
    if [ $? -eq 3 ]; then
        PM_UPDATED=1 exec bash "$0" "$@"
    fi
fi

if ! "$VENV_PY" -c "import fastapi, uvicorn, websockets, pynput, segno" >/dev/null 2>&1; then
    echo "Installing requirements ..."
    "$VENV_PY" -m pip install --disable-pip-version-check -r requirements.txt \
        || { echo "Failed to install requirements."; exit 1; }
fi

exec "$VENV_PY" -m server "$@"
