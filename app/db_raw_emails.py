import os
import logging
import psycopg2
from psycopg2 import sql
from typing import List, Dict, Any
from datetime import datetime
from dateutil import parser as date_parser

log = logging.getLogger("db_raw_emails")

# Config
DB_NAME = os.getenv("DB_NAME", "expenso")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "postgres")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")

def get_connection():
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
        host=DB_HOST,
        port=DB_PORT
    )

def parse_email_date(date_str: str) -> datetime:
    if not date_str:
        return datetime.utcnow()
    try:
        return date_parser.parse(date_str)
    except Exception:
        log.warning(f"Could not parse date: {date_str}, using now()")
        return datetime.utcnow()

def insert_raw_emails(emails: List[Dict[str, Any]]):
    """
    Batch insert emails into raw_emails table.
    Schema:
    id (serial), gmail_message_id, thread_id, sender, subject, snippet, raw_body, received_at, ingested_at, source
    """
    if not emails:
        return

    conn = get_connection()
    try:
        with conn.cursor() as cur:
            query = """
                INSERT INTO raw_emails 
                (gmail_message_id, thread_id, sender, subject, snippet, raw_body, received_at, source)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (gmail_message_id) 
                DO NOTHING;
            """
            
            # Prepare data
            data_tuples = []
            for e in emails:
                # Extract fields
                gmail_id = e.get("id")
                thread_id = e.get("threadId")
                sender = e.get("from")
                subject = e.get("subject")
                snippet = e.get("snippet")
                raw_body = e.get("raw_body")
                
                # 'date' key might be passed if we extracted it, otherwise look in headers if full object
                # Update: ingest.py constructs a simplified dict. We need to make sure it includes 'date'.
                # For now, assumes 'date' is in the passed dict, or we fallback.
                date_str = e.get("date") # Header Date
                received_at = parse_email_date(date_str)
                
                source = "gmail"
                
                data_tuples.append((
                    gmail_id, thread_id, sender, subject, snippet, raw_body, received_at, source
                ))
            
            cur.executemany(query, data_tuples)
            conn.commit()
            log.info(f"Inserted/Ignored {len(data_tuples)} raw emails into DB.")
            
    except Exception as e:
        log.error(f"DB Error: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()

def get_last_email_date() -> datetime:
    """
    Get the received_at timestamp of the most recent email.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT MAX(received_at) FROM raw_emails")
            row = cur.fetchone()
            if row and row[0]:
                return row[0]
            return None
    except Exception as e:
        log.error(f"Failed to get last email date: {e}")
        return None
    finally:
        conn.close()
