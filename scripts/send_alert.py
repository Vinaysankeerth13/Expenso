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
from email.message import EmailMessage

from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

load_dotenv()

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send"]
CREDENTIALS_PATH = os.getenv("GMAIL_CREDENTIALS", "./credentials.json")
TOKEN_PATH = os.getenv("TOKEN_PATH", "./token.json")


def _get_service():
    """Loads OAuth2 credentials and returns an authenticated Gmail service."""
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(TOKEN_PATH, "w") as f:
                f.write(creds.to_json())
        else:
            print("ERROR: OAuth2 token missing or invalid. Run ingest.py once to re-authenticate.", file=sys.stderr)
            sys.exit(1)
    return build("gmail", "v1", credentials=creds)


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
    service = _get_service()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"Alert email sent to {recipient}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, required=True, help="Number of days since last ingestion")
    args = parser.parse_args()
    send_alert(args.days)
