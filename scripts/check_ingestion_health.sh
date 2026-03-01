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

# Query database for last email received date
LAST_EMAIL_DATE=$(psql -U postgres -d expenso -t -c "SELECT MAX(received_at)::date FROM raw_emails;" 2>/dev/null | xargs)

if [ -z "$LAST_EMAIL_DATE" ]; then
    echo "⚠️  WARNING: No emails found in database!" | tee -a "$LOG_FILE"
    osascript -e 'display notification "No emails in database! Check Expenso pipeline." with title "Expenso Alert"'
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
