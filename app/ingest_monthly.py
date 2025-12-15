"""
app/ingest_monthly.py

Fetch transaction-related emails from a given sender for a month-range
and save raw messages + a JSON index in samples/raw/YYYY-MM/.

Usage examples:
  # run for Aug 2025 explicitly (one-off)
  poetry run python app/ingest_monthly.py --year 2025 --month 8

  # run for previous month automatically (useful from cron)
  poetry run python app/ingest_monthly.py --prev-month

Scheduling:
  - Cron (recommended for production) example (run at 03:00 on the 1st of every month):
      0 3 1 * * cd /Users/srisan/Desktop/Expenso && poetry run python app/ingest_monthly.py --prev-month

  - Or run as a long-running process using APScheduler (example included in this file).
"""

import os
import json
import base64
import re
import html
import pathlib
import argparse
import logging
from typing import List, Dict, Optional
from datetime import datetime, date
from dateutil.relativedelta import relativedelta

from dotenv import load_dotenv
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

# Optional: APScheduler for long-running scheduling
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

load_dotenv()

# config / env
CREDENTIALS_PATH: str = os.getenv("GMAIL_CREDENTIALS", "./credentials.json")
TOKEN_PATH: str = os.getenv("TOKEN_PATH", "./token.json")
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
SAMPLES_DIR = pathlib.Path("samples")
RAW_DIR = SAMPLES_DIR / "raw"

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ingest_monthly")


def get_gmail_service(credentials_path: Optional[str] = None, token_path: Optional[str] = None):
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
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(credentials_path, SCOPES)
            creds = flow.run_local_server(port=0)
        # save in JSON format (google oauth helper uses .to_json)
        with open(token_path, "w") as f:
            f.write(creds.to_json())
    service = build("gmail", "v1", credentials=creds)
    return service


def build_gmail_query(sender: str, start_date: date, end_date: date, extra_terms: Optional[List[str]] = None) -> str:
    """
    Build a Gmail query string.
    - sender: exact email (from:)
    - start_date, end_date: inclusive date objects (we convert to after:YYYY/MM/DD and before:YYYY/MM/DD where before = end_date + 1 day)
    - extra_terms: list of keywords/subject fragments to include (OR)
    """
    # Gmail uses after:YYYY/MM/DD (inclusive) and before:YYYY/MM/DD (exclusive).
    after = start_date.strftime("%Y/%m/%d")
    before_dt = end_date + relativedelta(days=1)
    before = before_dt.strftime("%Y/%m/%d")

    base = f'from:{sender} after:{after} before:{before}'

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
    """
    Conservative extraction: prefer text/plain parts, fallback to first part; return decoded string (no HTML tag stripping).
    For cleaning we'll deliberately keep raw body saved; downstream cleaning can use BeautifulSoup.
    """
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


def fetch_messages_for_query(service, q: str, max_results: int = 500) -> List[Dict]:
    """
    Fetch full message objects for the given Gmail query `q`. Returns list of full message JSONs.
    """
    messages = []
    page_token = None
    fetched = 0
    while True:
        req = service.users().messages().list(userId="me", q=q, maxResults=min(max_results - fetched, 100), pageToken=page_token)
        resp = req.execute()
        ids = resp.get("messages", []) or []
        if not ids:
            break
        for m in ids:
            full = service.users().messages().get(userId="me", id=m["id"], format="full").execute()
            messages.append(full)
            fetched += 1
            if fetched >= max_results:
                break
        page_token = resp.get("nextPageToken")
        if not page_token or fetched >= max_results:
            break
    log.info("Fetched %d messages for query: %s", len(messages), q)
    return messages


def save_samples_for_month(messages: List[Dict], year: int, month: int):
    """
    Save raw messages (one json file per message) and a month index JSON.
    Directory layout:
      samples/raw/YYYY-MM/
        msg_<id>.json
      samples/sample_emails_YYYY-MM.json  (index)
    """
    ym = f"{year:04d}-{month:02d}"
    outdir = RAW_DIR / ym
    outdir.mkdir(parents=True, exist_ok=True)
    index = []
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
            "snippet": msg.get("snippet"),
            "raw_body": body,
            "fetched_at": datetime.utcnow().isoformat() + "Z"
        }
        # write per-message raw file
        fname = outdir / f"msg_{mid}.json"
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(msg, f, ensure_ascii=False, indent=2)
        index.append(item)

    # write month index
    idx_path = SAMPLES_DIR / f"sample_emails_{ym}.json"
    with open(idx_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    log.info("Saved %d messages raw to %s and index to %s", len(messages), outdir, idx_path)
    return idx_path


def ingest_month(sender: str, year: int, month: int, keywords: Optional[List[str]] = None, max_results: int = 500):
    """
    High-level: fetch messages from sender in year/month containing keywords and save them.
    """
    start = date(year, month, 1)
    # last day of month: add one month then minus one day
    end = (start + relativedelta(months=1)) - relativedelta(days=1)

    query = build_gmail_query(sender=sender, start_date=start, end_date=end, extra_terms=keywords)
    svc = get_gmail_service()
    msgs = fetch_messages_for_query(svc, q=query, max_results=max_results)
    idx = save_samples_for_month(msgs, year, month)
    return idx


# small helper to compute previous month
def prev_month_year():
    today = date.today()
    first_this_month = date(today.year, today.month, 1)
    prev_first = first_this_month - relativedelta(months=1)
    return prev_first.year, prev_first.month


# CLI entrypoint
def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, help="Year (e.g. 2025)")
    parser.add_argument("--month", type=int, help="Month number (1-12)")
    parser.add_argument("--prev-month", action="store_true", help="Run for previous month")
    parser.add_argument("--max-results", type=int, default=500, help="Maximum emails to fetch")
    parser.add_argument("--sender", type=str, default="alerts@hdfcbank.net", help="Sender email address (default alerts@hdfcbank.net)")
    args = parser.parse_args()

    if args.prev_month:
        y, m = prev_month_year()
    else:
        if not args.year or not args.month:
            parser.error("Either --prev-month or both --year and --month must be provided")
        y, m = args.year, args.month

    # transaction related terms (tune as needed)
    keywords = ["receipt", "payment", "transaction", "debited", "credited", "withdrawal", "order", "txn", "failed", "attempted"]

    log.info("Ingesting for %04d-%02d from sender=%s", y, m, args.sender)
    idx_path = ingest_month(sender=args.sender, year=y, month=m, keywords=keywords, max_results=args.max_results)
    log.info("Done. Index saved to %s", idx_path)


# APScheduler helper: run this process as a long-running service if desired
def run_scheduler():
    """
    Example: schedule to run at 03:00 on the 1st day of every month.
    If you choose this, run `poetry run python app/ingest_monthly.py --scheduler` in a screen/systemd service.
    """
    sched = BackgroundScheduler()
    # CronTrigger: minute hour day month day_of_week
    trigger = CronTrigger(minute=0, hour=3, day=1)  # 03:00 on day 1 every month
    def job():
        try:
            y, m = prev_month_year()
            log.info("Scheduler job running for %04d-%02d", y, m)
            ingest_month(sender="alerts@hdfcbank.net", year=y, month=m, keywords=["receipt","payment","transaction","debited","credited"], max_results=1000)
        except Exception as e:
            log.exception("Scheduled job failed: %s", e)

    sched.add_job(job, trigger)
    log.info("Scheduler started - will run at 03:00 on the 1st of every month")
    sched.start()
    try:
        import time
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        sched.shutdown()


if __name__ == "__main__":
    # allow running the scheduler with --scheduler
    import sys
    if "--scheduler" in sys.argv:
        run_scheduler()
    else:
        cli()
