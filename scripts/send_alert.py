"""
Sends a plain-text alert email via the Gmail API using the existing OAuth2 token.
Called by check_ingestion_health.sh when ingestion has been stale for 3+ days.

Usage:
    python scripts/send_alert.py --days <N>
"""

import argparse
import os
import sys
sys.path.append(os.getcwd())

from dotenv import load_dotenv

from app.gmail_auth import send_email

load_dotenv()


def send_alert(days_stale: int) -> None:
    """Composes and sends a staleness alert email via the Gmail API."""
    subject = f"Expenso Alert: No emails ingested for {days_stale} days"
    body = (
        f"Hey,\n\n"
        f"The Expenso ingestion pipeline hasn't picked up any new emails in {days_stale} days. "
        f"Please check the ingestion pipeline — something seems to have broken.\n\n"
        f"Things to check:\n"
        f"  1. HDFC may have changed their sender address (check Gmail manually)\n"
        f"  2. Gmail API auth may have expired (check logs/launchd.error.log)\n"
        f"  3. Emails may be going to spam or the Promotions tab\n\n"
        f"— Expenso"
    )
    try:
        send_email(subject, body)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"Alert email sent to {os.getenv('ALERT_EMAIL_TO')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, required=True, help="Number of days since last ingestion")
    args = parser.parse_args()
    send_alert(args.days)
