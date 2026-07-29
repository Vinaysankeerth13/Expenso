"""
Sends a plain-text alert email via the Gmail API using the existing OAuth2 token.
Called by check_ingestion_health.sh when ingestion has been stale for 3+ days.

Usage:
    python scripts/send_alert.py --days <N>
"""

import argparse
import base64
import os
import sys
sys.path.append(os.getcwd())
from email.message import EmailMessage

from dotenv import load_dotenv

from app.gmail_auth import get_gmail_service

load_dotenv()


def send_alert(days_stale: int) -> None:
    """Composes and sends a staleness alert email via the Gmail API."""
    recipient = os.getenv("ALERT_EMAIL_TO")
    if not recipient:
        print("ERROR: ALERT_EMAIL_TO must be set in .env", file=sys.stderr)
        sys.exit(1)

    msg = EmailMessage()
    msg["Subject"] = f"Expenso Alert: No emails ingested for {days_stale} days"
    msg["To"] = recipient
    msg.set_content(
        f"Hey,\n\n"
        f"The Expenso ingestion pipeline hasn't picked up any new emails in {days_stale} days. "
        f"Please check the ingestion pipeline — something seems to have broken.\n\n"
        f"Things to check:\n"
        f"  1. HDFC may have changed their sender address (check Gmail manually)\n"
        f"  2. Gmail API auth may have expired (check logs/launchd.error.log)\n"
        f"  3. Emails may be going to spam or the Promotions tab\n\n"
        f"— Expenso"
    )

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    try:
        service = get_gmail_service()
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"Alert email sent to {recipient}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, required=True, help="Number of days since last ingestion")
    args = parser.parse_args()
    send_alert(args.days)
