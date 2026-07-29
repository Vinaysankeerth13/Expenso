"""
app/ingest.py

Fetch transaction-related emails from a given sender for a date range
and save raw messages + a JSON index in samples/raw/YYYY-MM/ (and to the
raw_emails DB table).

Usage examples:
  # run for Aug 2025 explicitly (one-off)
  poetry run python app/ingest.py --mode monthly --year 2025 --month 8

  # run for previous month automatically
  poetry run python app/ingest.py --mode monthly --prev-month

  # catch up on everything since the last email in the DB (used by the daily job)
  poetry run python app/ingest.py --mode incremental

Scheduling:
  Production scheduling is a launchd job (scripts/com.srisan.expenso.daily.plist)
  that runs scripts/daily_job.sh nightly, which calls this script with
  `--mode incremental` followed by extraction, classification, and a health check.
"""

import os
import sys
sys.path.append(os.getcwd())
import json
import base64
import pathlib
import argparse
import logging
from typing import List, Dict, Optional
from datetime import datetime, date, timezone
from dateutil.relativedelta import relativedelta

from dotenv import load_dotenv
from googleapiclient.errors import HttpError
import socket
import time
import random

from app import db_raw_emails
from app.gmail_auth import get_gmail_service

load_dotenv()

# config / env
SAMPLES_DIR = pathlib.Path("samples")
RAW_DIR = SAMPLES_DIR / "raw"
DAILY_DIR = SAMPLES_DIR / "daily"
DAILY_DIR.mkdir(parents=True, exist_ok=True)

# HDFC Bank sender addresses (support both old and new)
# HDFC switched from .net to .bank.in around Feb 2026
HDFC_SENDERS = ["alerts@hdfcbank.bank.in", "alerts@hdfcbank.net"]


log = logging.getLogger(__name__)


def build_gmail_query(sender, start_date: date, end_date: date, extra_terms: Optional[List[str]] = None) -> str:
    """Constructs a Gmail search query string filtering by sender(s), date range, and optional keywords.

    Args:
        sender: Single email (str) or list of emails (list) to search from
        start_date: Start date (inclusive)
        end_date: End date (inclusive)
        extra_terms: Optional keywords to search for

    Returns:
        Gmail search query string
    """
    # Gmail uses after:YYYY/MM/DD (inclusive) and before:YYYY/MM/DD (exclusive).
    after = start_date.strftime("%Y/%m/%d")
    before_dt = end_date + relativedelta(days=1)
    before = before_dt.strftime("%Y/%m/%d")

    # Handle multiple senders with OR operator
    if isinstance(sender, list):
        sender_parts = [f'from:{s}' for s in sender]
        sender_clause = f'({" OR ".join(sender_parts)})'
    else:
        sender_clause = f'from:{sender}'

    base = f'{sender_clause} after:{after} before:{before}'

    if extra_terms:
        # prepare OR expression: wrap multi-word terms in quotes
        parts = []
        for t in extra_terms:
            # quote if contains space
            if " " in t:
                parts.append(f'"{t}"')
            else:
                parts.append(t)
        # join with OR and wrap in parentheses
        or_expr = " OR ".join(parts)
        base = f"{base} ({or_expr})"

    return base


def extract_body_from_message(msg: Dict) -> str:
    """Extracts email body, preferring text/plain, without stripping HTML tags."""
    payload = msg.get("payload", {})
    parts = payload.get("parts", [])
    body = ""

    def decode_part(data_b64: str) -> str:
        try:
            return base64.urlsafe_b64decode(data_b64).decode("utf-8", errors="ignore")
        except Exception:
            return ""

    if parts:
        # find text/plain if present (recursive)
        stack = list(parts)
        while stack:
            p = stack.pop(0)
            mime = p.get("mimeType", "")
            if mime == "text/plain" and p.get("body", {}).get("data"):
                body = decode_part(p["body"]["data"])
                break
            if p.get("parts"):
                stack.extend(p.get("parts"))
        if not body:
            # fallback to first data-containing part
            for p in parts:
                data = p.get("body", {}).get("data")
                if data:
                    body = decode_part(data)
                    break
    else:
        data = payload.get("body", {}).get("data")
        if data:
            body = decode_part(data)
    # return raw decoded body (may include html); we'll not aggressively strip here to preserve original content
    return body


def execute_with_retry(request, retries=5, delay=2, backoff=2):
    """Executes a Google API request with exponential backoff retry for network resilience."""
    for i in range(retries):
        try:
            return request.execute()
        except (TimeoutError, socket.timeout, HttpError) as e:
            # Check for 5xx errors or specific transport errors if it's an HttpError
            if isinstance(e, HttpError):
                if e.resp.status < 500:
                    raise e
            
            if i == retries - 1:
                log.error(f"Request failed after {retries} retries: {e}")
                raise e
            
            sleep_time = delay * (backoff ** i) + random.uniform(0, 1)
            log.warning(f"Request failed ({e}), retrying in {sleep_time:.2f}s...")
            time.sleep(sleep_time)



def fetch_messages_for_query(service, q: str, max_results: int = 500) -> List[Dict]:
    """Fetches full email message objects (including body/headers) matching the query."""
    messages = []
    page_token = None
    fetched = 0
    while True:
        req = service.users().messages().list(userId="me", q=q, maxResults=min(max_results - fetched, 100), pageToken=page_token)
        resp = execute_with_retry(req)
        ids = resp.get("messages", []) or []
        if not ids:
            break
        for m in ids:
            get_req = service.users().messages().get(userId="me", id=m["id"], format="full")
            full = execute_with_retry(get_req)
            messages.append(full)
            fetched += 1
            if fetched >= max_results:
                break
        page_token = resp.get("nextPageToken")
        if not page_token or fetched >= max_results:
            break
    log.info("Fetched %d messages for query: %s", len(messages), q)
    return messages


def save_samples(messages: List[Dict], ym_folder: str, index_path: pathlib.Path):
    """Saves raw email JSONs to disk, creates a summary index, and inserts metadata into the DB."""
    outdir = RAW_DIR / ym_folder
    outdir.mkdir(parents=True, exist_ok=True)
    index = []
    
    # List for batch DB insert
    db_items = []
    
    for msg in messages:
        mid = msg.get("id")
        # build simplified metadata
        headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
        body = extract_body_from_message(msg)
        item = {
            "id": mid,
            "threadId": msg.get("threadId"),
            "from": headers.get("From"),
            "to": headers.get("To"),
            "subject": headers.get("Subject"),
            "date": headers.get("Date"), # Added for DB
            "snippet": msg.get("snippet"),
            "raw_body": body,
            "fetched_at": datetime.now(timezone.utc).isoformat()
        }
        # write per-message raw file
        fname = outdir / f"msg_{mid}.json"
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(msg, f, ensure_ascii=False, indent=2)
        index.append(item)
        db_items.append(item)

    # write index
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    log.info("Saved %d messages raw to %s and index to %s", len(messages), outdir, index_path)
    
    # Insert into DB
    try:
        db_raw_emails.insert_raw_emails(db_items)
    except Exception as e:
        log.error(f"Failed to insert into DB: {e}")
        
    return index_path


def ingest_range(sender: str, start: date, end: date, keywords: Optional[List[str]] = None, max_results: int = 500, mode: str = "monthly"):
    """Orchestrates email fetching and saving for a specified date range and mode."""
    query = build_gmail_query(sender=sender, start_date=start, end_date=end, extra_terms=keywords)
    svc = get_gmail_service()
    msgs = fetch_messages_for_query(svc, q=query, max_results=max_results)
    
    # Decide storage paths
    if mode == "monthly":
        ym = f"{start.year:04d}-{start.month:02d}"
        idx_path = SAMPLES_DIR / f"sample_emails_{ym}.json"
        save_samples(msgs, ym, idx_path)
        return idx_path
    else:
        # daily
        ym = f"{start.year:04d}-{start.month:02d}"
        ymd = f"{start.year:04d}-{start.month:02d}-{start.day:02d}"
        idx_path = DAILY_DIR / f"sample_emails_{ymd}.json"
        save_samples(msgs, ym, idx_path)
        return idx_path


def get_keywords():
    """Returns a list of keywords used to filter relevant transaction emails."""
    return ["receipt", "payment", "transaction", "debited", "credited", "withdrawal", "order", "txn", "failed", "attempted"]


# CLI entrypoint
def cli():
    """Parses command-line arguments and triggers the appropriate ingestion mode (monthly/daily/incremental)."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", type=str, choices=["monthly", "daily", "incremental"], default="monthly", help="Ingestion mode")
    
    # Date args
    parser.add_argument("--year", type=int, help="Year (e.g. 2025)")
    parser.add_argument("--month", type=int, help="Month number (1-12)")
    parser.add_argument("--day", type=int, help="Day number (1-31)")
    
    # Shortcuts
    parser.add_argument("--prev-month", action="store_true", help="Run for previous month (monthly mode)")
    parser.add_argument("--yesterday", action="store_true", help="Run for yesterday (daily mode)")
    parser.add_argument("--today", action="store_true", help="Run for today (daily mode)") # just in case

    parser.add_argument("--max-results", type=int, default=500, help="Maximum emails to fetch")
    parser.add_argument("--sender", type=str, default=None, help="Sender email address (default: both HDFC senders)")
    args = parser.parse_args()

    # Use both HDFC senders by default, or single sender if specified
    sender = args.sender if args.sender else HDFC_SENDERS
    keywords = get_keywords()
    
    if args.mode == "monthly":
        if args.prev_month:
            today = date.today()
            first = date(today.year, today.month, 1) - relativedelta(months=1)
            y, m = first.year, first.month
        elif args.year and args.month:
            y, m = args.year, args.month
        else:
            parser.error("Monthly mode requires --prev-month OR --year and --month")
            return

        start = date(y, m, 1)
        end = (start + relativedelta(months=1)) - relativedelta(days=1)
        log.info(f"Monthly Ingest: {start} to {end}")
        ingest_range(sender, start, end, keywords, args.max_results, mode="monthly")

    elif args.mode == "daily":
        target_date = None
        if args.yesterday:
            target_date = date.today() - relativedelta(days=1)
        elif args.today:
            target_date = date.today()
        elif args.year and args.month and args.day:
            target_date = date(args.year, args.month, args.day)
        else:
            parser.error("Daily mode requires --yesterday, --today, OR --year, --month, --day")
            return
        
        log.info(f"Daily Ingest: {target_date}")
        ingest_range(sender, target_date, target_date, keywords, args.max_results, mode="daily")

    elif args.mode == "incremental":
        # Check DB for last date
        from datetime import timedelta
        last_dt = db_raw_emails.get_last_email_date()
        yesterday = date.today() - relativedelta(days=1)
        
        if last_dt:
            # Start from next day
            start_date = last_dt.date() + timedelta(days=1)
        else:
            # Fallback if DB empty: Fetch last 30 days? Or just yesterday?
            # Let's default to start of current month to be safe/useful
            log.info("No DB history found. Defaulting to start of current month.")
            today = date.today()
            start_date = date(today.year, today.month, 1)
            
        end_date = yesterday
        
        if start_date > end_date:
            log.info(f"Incremental Ingest: DB is up to date (Last: {last_dt.date()}, End: {end_date}). Nothing to do.")
            return

        log.info(f"Incremental Ingest: Catching up from {start_date} to {end_date}")
        ingest_range(sender, start_date, end_date, keywords, args.max_results, mode="daily")


if __name__ == "__main__":
    cli()
