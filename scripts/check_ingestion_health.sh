#!/bin/bash
# check_ingestion_health.sh
#
# Monitors email ingestion health and sends alerts if no emails have been
# ingested for 3+ days (could indicate sender address changed or API issues)
#
# Usage:
#   ./scripts/check_ingestion_health.sh
#
# Can be scheduled to run daily via launchd or cron

set -e

cd "$(dirname "$0")/.."

ALERT_THRESHOLD_DAYS=3
LOG_FILE="logs/health_check.log"
mkdir -p logs

echo "$(date '+%Y-%m-%d %H:%M:%S') - Running ingestion health check..." | tee -a "$LOG_FILE"

# Load .env for DB credentials — manually parse KEY=VALUE lines only,
# skipping comments and bare values (e.g. email addresses) that break `source`.
if [ -f ".env" ]; then
    while IFS= read -r line; do
        [[ "$line" =~ ^[[:space:]]*# ]] && continue
        [[ -z "$line" ]] && continue
        [[ "$line" =~ ^([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] && export "${BASH_REMATCH[1]}=${BASH_REMATCH[2]}"
    done < .env
fi
DB_NAME="${DB_NAME:-expenso}"
DB_USER="${DB_USER:-postgres}"
DB_PASS="${DB_PASS:-postgres}"
DB_HOST="${DB_HOST:-localhost}"

LAST_EMAIL_DATE=$(PGPASSWORD="$DB_PASS" psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -t -c "SELECT MAX(received_at)::date FROM raw_emails;" 2>/tmp/expenso_health_psql.err | xargs)

if [ -z "$LAST_EMAIL_DATE" ]; then
    PSQL_ERR=$(cat /tmp/expenso_health_psql.err 2>/dev/null)
    echo "⚠️  WARNING: Could not read last email date from DB." | tee -a "$LOG_FILE"
    echo "    psql error: $PSQL_ERR" | tee -a "$LOG_FILE"
    osascript -e 'display notification "Could not read from DB. Check Expenso pipeline." with title "Expenso Alert"'
    exit 1
fi

# Calculate days since last email
TODAY=$(date '+%Y-%m-%d')
LAST_EPOCH=$(date -j -f "%Y-%m-%d" "$LAST_EMAIL_DATE" "+%s" 2>/dev/null || echo 0)
TODAY_EPOCH=$(date -j -f "%Y-%m-%d" "$TODAY" "+%s")
DAYS_SINCE=$((($TODAY_EPOCH - $LAST_EPOCH) / 86400))

echo "Last email received: $LAST_EMAIL_DATE (${DAYS_SINCE} days ago)" | tee -a "$LOG_FILE"

# Alert if stale
if [ "$DAYS_SINCE" -ge "$ALERT_THRESHOLD_DAYS" ]; then
    MESSAGE="⚠️  ALERT: No emails ingested for ${DAYS_SINCE} days (last: ${LAST_EMAIL_DATE})"
    echo "$MESSAGE" | tee -a "$LOG_FILE"

    # macOS notification
    osascript -e "display notification \"Last email: ${LAST_EMAIL_DATE} (${DAYS_SINCE} days ago)\" with title \"Expenso: No Recent Emails!\" sound name \"Glass\""

    # Email alert
    POETRY=/Users/srisan/.local/bin/poetry
    PYTHON=$($POETRY env info --path 2>/dev/null)/bin/python
    "$PYTHON" scripts/send_alert.py --days "$DAYS_SINCE" >> "$LOG_FILE" 2>&1 \
        || echo "$(date '+%Y-%m-%d %H:%M:%S') - WARNING: Failed to send alert email" | tee -a "$LOG_FILE"

    # Log suggestions
    echo "" | tee -a "$LOG_FILE"
    echo "Possible causes:" | tee -a "$LOG_FILE"
    echo "  1. HDFC changed sender email (check Gmail manually)" | tee -a "$LOG_FILE"
    echo "  2. Gmail API auth expired (check logs/launchd.error.log)" | tee -a "$LOG_FILE"
    echo "  3. No transactions made (check your bank account)" | tee -a "$LOG_FILE"
    echo "  4. Emails filtered to spam/promotions" | tee -a "$LOG_FILE"
    echo "" | tee -a "$LOG_FILE"
    echo "Run diagnostic: poetry run python test_gmail_query.py" | tee -a "$LOG_FILE"

    exit 1
else
    echo "✅ Health check passed (ingestion is current)" | tee -a "$LOG_FILE"
    exit 0
fi
