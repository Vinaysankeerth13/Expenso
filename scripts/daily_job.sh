#!/bin/bash

# Expenso Daily Ingestion Job
# Runs ingestion for "yesterday" and extracts transactions
# Enhanced with explicit error logging and resilient execution

cd "$(dirname "$0")/.." || exit

# Resolve the poetry virtualenv python at runtime so that this script survives
# venv recreation (e.g. after `poetry install` or python version changes).
POETRY=/Users/srisan/.local/bin/poetry
PYTHON=$($POETRY env info --path 2>/dev/null)/bin/python
if [ ! -x "$PYTHON" ]; then
    echo "$(date): ❌ FATAL: Cannot locate poetry virtualenv python. Run 'poetry install' in project root." >> logs/launchd.error.log
    exit 1
fi

# Step 1: Ingest new emails from Gmail (can fail due to API issues)
echo "$(date): Starting email ingestion..." >> logs/launchd.log
if "$PYTHON" app/ingest.py --mode incremental >> logs/launchd.log 2>> logs/launchd.error.log; then
    echo "$(date): ✅ Phase 1 (Ingestion) completed successfully" >> logs/launchd.log
else
    INGEST_EXIT_CODE=$?
    echo "$(date): ⚠️ WARNING: Phase 1 (Ingestion) failed with exit code $INGEST_EXIT_CODE" >> logs/launchd.error.log
    echo "$(date): ⚠️ Will retry tomorrow. Check launchd.error.log for details." >> logs/launchd.error.log
fi

# Step 2: Extract transactions from newly ingested emails (can fail due to DB issues)
# Runs regardless of Phase 1 status to process any backlog
echo "$(date): Starting transaction extraction..." >> logs/launchd.log
if "$PYTHON" app/extract_transactions.py --unprocessed-only >> logs/launchd.log 2>> logs/launchd.error.log; then
    echo "$(date): ✅ Phase 2 (Extraction) completed successfully" >> logs/launchd.log
else
    EXTRACT_EXIT_CODE=$?
    echo "$(date): ⚠️ WARNING: Phase 2 (Extraction) failed with exit code $EXTRACT_EXIT_CODE" >> logs/launchd.error.log
    echo "$(date): ⚠️ Will retry tomorrow. Check launchd.error.log for details." >> logs/launchd.error.log
fi

# Step 3: Classify new transactions
echo "$(date): Starting transaction classification..." >> logs/launchd.log
if "$PYTHON" app/classify_transactions.py --unclassified-only >> logs/launchd.log 2>> logs/launchd.error.log; then
    echo "$(date): ✅ Phase 3 (Classification) completed successfully" >> logs/launchd.log
else
    CLASSIFY_EXIT_CODE=$?
    echo "$(date): ⚠️ WARNING: Phase 3 (Classification) failed with exit code $CLASSIFY_EXIT_CODE" >> logs/launchd.error.log
    echo "$(date): ⚠️ Classification will retry tomorrow. Check launchd.error.log for details." >> logs/launchd.error.log
fi

# Step 4: Health check (alert if no emails ingested for 3+ days)
echo "$(date): Running health check..." >> logs/launchd.log
if ./scripts/check_ingestion_health.sh >> logs/launchd.log 2>> logs/launchd.error.log; then
    echo "$(date): ✅ Health check passed" >> logs/launchd.log
else
    echo "$(date): ⚠️ Health check alert triggered (see logs/health_check.log)" >> logs/launchd.log
fi

# Step 5: Parsing quality check (alert if merchant/category extraction is degrading,
# e.g. a bank changed an email template — see logs/parsing_health.log)
echo "$(date): Running parsing quality check..." >> logs/launchd.log
if "$PYTHON" scripts/check_parsing_health.py >> logs/launchd.log 2>> logs/launchd.error.log; then
    echo "$(date): ✅ Parsing quality check passed" >> logs/launchd.log
else
    echo "$(date): ⚠️ Parsing quality alert triggered (see logs/parsing_health.log)" >> logs/launchd.log
fi

echo "$(date): Daily job finished. Check logs for any warnings." >> logs/launchd.log
