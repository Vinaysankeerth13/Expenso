#!/bin/bash

# Expenso Daily Ingestion Job
# Runs ingestion for "yesterday" and extracts transactions
# Enhanced with explicit error logging and resilient execution

cd "$(dirname "$0")/.." || exit

# Step 1: Ingest new emails from Gmail (can fail due to API issues)
echo "$(date): Starting email ingestion..." >> logs/launchd.log
if /Users/srisan/.local/bin/poetry run python app/ingest.py --mode incremental >> logs/launchd.log 2>> logs/launchd.error.log; then
    echo "$(date): ✅ Phase 1 (Ingestion) completed successfully" >> logs/launchd.log
else
    INGEST_EXIT_CODE=$?
    echo "$(date): ⚠️ WARNING: Phase 1 (Ingestion) failed with exit code $INGEST_EXIT_CODE" >> logs/launchd.error.log
    echo "$(date): ⚠️ Will retry tomorrow. Check launchd.error.log for details." >> logs/launchd.error.log
fi

# Step 2: Extract transactions from newly ingested emails (can fail due to DB issues)
# Runs regardless of Phase 1 status to process any backlog
echo "$(date): Starting transaction extraction..." >> logs/launchd.log
if /Users/srisan/.local/bin/poetry run python app/extract_transactions.py --unprocessed-only >> logs/launchd.log 2>> logs/launchd.error.log; then
    echo "$(date): ✅ Phase 2 (Extraction) completed successfully" >> logs/launchd.log
else
    EXTRACT_EXIT_CODE=$?
    echo "$(date): ⚠️ WARNING: Phase 2 (Extraction) failed with exit code $EXTRACT_EXIT_CODE" >> logs/launchd.error.log
    echo "$(date): ⚠️ Will retry tomorrow. Check launchd.error.log for details." >> logs/launchd.error.log
fi

echo "$(date): Daily job finished. Check logs for any warnings." >> logs/launchd.log
