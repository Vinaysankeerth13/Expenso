#!/bin/bash
# Continue useful backlog work after a failed stage, but return aggregate failure.
set -u
cd "$(dirname "$0")/.." || exit 1
mkdir -p logs
POETRY_BIN="${EXPENSO_POETRY:-$(command -v poetry || true)}"
if [ -z "$POETRY_BIN" ]; then POETRY_BIN="$HOME/.local/bin/poetry"; fi
PIPELINE_PYTHON="${EXPENSO_PYTHON:-}"
if [ -z "$PIPELINE_PYTHON" ]; then
    VENV_PATH=$("$POETRY_BIN" env info --path 2>/dev/null) || exit 1
    PIPELINE_PYTHON="$VENV_PATH/bin/python"
fi
if [ ! -x "$PIPELINE_PYTHON" ]; then
    echo "Cannot locate Python; set EXPENSO_PYTHON or install the Poetry environment." >> logs/launchd.error.log
    exit 1
fi
job_status=0
run_phase() {
    phase_name="$1"
    shift
    if "$@" >> logs/launchd.log 2>> logs/launchd.error.log; then
        echo "$(date): $phase_name completed" >> logs/launchd.log
    else
        phase_status=$?
        if [ "$phase_name" = parsing_quality ] && [ "$phase_status" -eq 2 ]; then
            echo "$(date): Parsing quality unknown (insufficient sample)" >> logs/launchd.log
        else
            echo "$(date): $phase_name failed (exit $phase_status)" >> logs/launchd.error.log
            job_status=1
        fi
    fi
}
run_phase ingestion "$PIPELINE_PYTHON" -m app.ingest --mode incremental
run_phase extraction "$PIPELINE_PYTHON" -m app.pipeline extraction
run_phase classification "$PIPELINE_PYTHON" -m app.pipeline classification
run_phase pipeline_health "$PIPELINE_PYTHON" scripts/check_pipeline_health.py --notify
run_phase parsing_quality "$PIPELINE_PYTHON" scripts/check_parsing_health.py --notify
printf 'Daily job finished with status %s\n' "$job_status" >> logs/launchd.log
exit "$job_status"
