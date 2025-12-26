#!/bin/bash

# Expenso Daily Ingestion Job
# Runs ingestion for "yesterday"

cd "$(dirname "$0")/.." || exit

# Ensure poetry env is used
# Adjust path if necessary or rely on poetry in PATH
/Users/srisan/.local/bin/poetry run python app/ingest.py --mode incremental
