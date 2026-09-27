#!/bin/bash
# Compatibility entry point; completion-based health replaces last-email age.
set -eu
cd "$(dirname "$0")/.."
PIPELINE_PYTHON="${EXPENSO_PYTHON:-}"
if [ -z "$PIPELINE_PYTHON" ]; then
    POETRY_BIN="${EXPENSO_POETRY:-$(command -v poetry || true)}"
    if [ -z "$POETRY_BIN" ]; then POETRY_BIN="$HOME/.local/bin/poetry"; fi
    VENV_PATH=$("$POETRY_BIN" env info --path)
    PIPELINE_PYTHON="$VENV_PATH/bin/python"
fi
exec "$PIPELINE_PYTHON" scripts/check_pipeline_health.py
