"""Database operations for the raw_emails table."""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from dateutil import parser as date_parser

from app.db_connection import get_connection

log = logging.getLogger(__name__)


def parse_email_date(date_str: str) -> datetime:
    """Parses an email header date string into a UTC datetime, defaulting to now on failure."""
    if not date_str:
        return datetime.now(timezone.utc)
    try:
        return date_parser.parse(date_str)
    except Exception:
        log.warning(f"Could not parse date: {date_str}, using now()")
        return datetime.now(timezone.utc)


def insert_raw_emails(emails: List[Dict[str, Any]]) -> int:
    """Persist raw emails idempotently and return the number newly stored."""
    if not emails:
        return 0

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            query = """
                INSERT INTO raw_emails
                (gmail_message_id, thread_id, sender, subject, snippet, raw_body, received_at, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (gmail_message_id) DO NOTHING
            """
            data_tuples = [
                (
                    e.get("id"),
                    e.get("threadId"),
                    e.get("from"),
                    e.get("subject"),
                    e.get("snippet"),
                    e.get("raw_body"),
                    parse_email_date(e.get("date")),
                    "gmail",
                )
                for e in emails
            ]
            cur.executemany(query, data_tuples)
            inserted = cur.rowcount
            conn.commit()
            log.info("Stored %d new emails; skipped %d existing", inserted, len(data_tuples) - inserted)
            return inserted
    except Exception as e:
        log.error(f"Failed to insert raw emails: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()


def get_last_email_date() -> datetime | None:
    """Returns the received_at timestamp of the most recently stored email."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(received_at) FROM raw_emails")
            row = cur.fetchone()
            return row[0] if row and row[0] else None
    except Exception as e:
        log.error(f"Failed to get last email date: {e}")
        return None
    finally:
        conn.close()
