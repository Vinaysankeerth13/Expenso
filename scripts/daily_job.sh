#!/bin/bash

# Expenso Daily Ingestion Job
# Runs ingestion for "yesterday" and extracts transactions

cd "$(dirname "$0")/.." || exit

# Ensure poetry env is used
# Adjust path if necessary or rely on poetry in PATH

# Step 1: Ingest new emails from Gmail
echo "$(date): Starting email ingestion..." >> logs/launchd.log
/Users/srisan/.local/bin/poetry run python app/ingest.py --mode incremental >> logs/launchd.log 2>> logs/launchd.error.log

# Step 2: Extract transactions from newly ingested emails
echo "$(date): Starting transaction extraction..." >> logs/launchd.log
/Users/srisan/.local/bin/poetry run python app/extract_transactions.py --unprocessed-only >> logs/launchd.log 2>> logs/launchd.error.log

echo "$(date): Daily job completed successfully" >> logs/launchd.log
