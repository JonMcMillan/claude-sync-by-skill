#!/usr/bin/env sh
# Launcher: run the claude-sync-by-skill engine with the platform's Python 3.
# Usage: ./run.sh [--status | --direction up|down ...]
DIR="$(cd "$(dirname "$0")" && pwd)"
if command -v python3 >/dev/null 2>&1; then
    PY=python3
else
    PY=python
fi
exec "$PY" "$DIR/sync_engine.py" "$@"
