"""Shared Gmail OAuth2 authentication and alert-sending, used by email
ingestion and the pipeline health-check alerts (scripts/send_alert.py,
scripts/check_parsing_health.py). Keeping one copy means a fix here (e.g.
the non-interactive fail-fast guard below) can't silently drift out of
sync between callers, which is what happened before this was split out.
"""

import base64
import os
import sys
import logging
from email.message import EmailMessage
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

log = logging.getLogger(__name__)

CREDENTIALS_PATH: str = os.getenv("GMAIL_CREDENTIALS", "./credentials.json")
TOKEN_PATH: str = os.getenv("TOKEN_PATH", "./token.json")
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly", "https://www.googleapis.com/auth/gmail.send"]


def get_gmail_service(credentials_path: Optional[str] = None, token_path: Optional[str] = None):
    """Authenticates with Gmail API using OAuth2 and returns the service object."""
    credentials_path = credentials_path or CREDENTIALS_PATH
    token_path = token_path or TOKEN_PATH
    creds = None
    if os.path.exists(token_path):
        try:
            creds = Credentials.from_authorized_user_file(token_path, SCOPES)
        except Exception:
            creds = None
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception as e:
                log.error(f"Failed to refresh OAuth token: {e}")
                if os.path.exists(token_path):
                    try:
                        os.remove(token_path)
                        log.info(f"Removed invalid token file: {token_path}")
                    except Exception as rm_err:
                        log.error(f"Could not remove token file {token_path}: {rm_err}")
                raise RuntimeError(
                    "OAuth refresh token is invalid or expired (invalid_grant). "
                    "The token file has been removed. Please run the ingestion script manually "
                    "once to re-authenticate via browser: `poetry run python app/ingest.py --mode incremental`"
                ) from e
        else:
            if not sys.stdin.isatty():
                # flow.run_local_server() blocks indefinitely waiting for a browser
                # redirect. Under launchd/cron there's no display to complete it,
                # so a missing/expired token would hang the process forever instead
                # of failing. Fail fast so the daily job can log, retry, and alert.
                raise RuntimeError(
                    "No valid OAuth token found and this is not an interactive "
                    "session (no TTY) — refusing to start a browser-based auth "
                    "flow, which would hang forever. Please run "
                    "`poetry run python app/ingest.py --mode incremental` "
                    "manually from a terminal to re-authenticate via browser."
                )
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        # save in JSON format (google oauth helper uses .to_json)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    service = build("gmail", "v1", credentials=creds)
    return service


def send_email(subject: str, body: str, to: Optional[str] = None) -> None:
    """Sends a plain-text email via the Gmail API using the shared OAuth2 token.

    Raises RuntimeError if no recipient is given/configured, or if auth fails
    (e.g. an expired token under a non-interactive session — see
    get_gmail_service).
    """
    recipient = to or os.getenv("ALERT_EMAIL_TO")
    if not recipient:
        raise RuntimeError("No recipient given and ALERT_EMAIL_TO is not set in .env")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["To"] = recipient
    msg.set_content(body)

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service = get_gmail_service()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
