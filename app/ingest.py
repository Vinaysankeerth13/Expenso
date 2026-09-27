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
from datetime import datetime, date, timezone, time as daytime, timedelta
from zoneinfo import ZoneInfo
from dateutil.relativedelta import relativedelta

from googleapiclient.errors import HttpError
import socket
import time
import random

from app import db_raw_emails, db_pipeline
from app.gmail_auth import get_gmail_service


# config / env
SAMPLES_DIR = pathlib.Path("samples")
RAW_DIR = SAMPLES_DIR / "raw"
DAILY_DIR = SAMPLES_DIR / "daily"

# HDFC Bank sender addresses (support both old and new)
# HDFC switched from .net to .bank.in around Feb 2026
HDFC_SENDERS = ["alerts@hdfcbank.bank.in", "alerts@hdfcbank.net"]


log = logging.getLogger(__name__)


def build_gmail_query(
    sender, start_date: date, end_date: date, extra_terms: Optional[List[str]] = None
) -> str:
    """Constructs a Gmail search query string filtering by sender(s), date range, and optional keywords.

    Args:
        sender: Single email (str) or list of emails (list) to search from
        start_date: Start date (inclusive)
        end_date: End date (inclusive)
        extra_terms: Optional keywords to search for

    Returns:
        Gmail search query string
    """
    # Epoch bounds avoid Gmail interpreting calendar dates in Pacific time.
    # after is exclusive, so include the exact start-of-day second.
    zone = ZoneInfo(os.environ.get("EXPENSO_TIMEZONE", "Asia/Kolkata"))
    after = int(datetime.combine(start_date, daytime.min, zone).timestamp()) - 1
    before = int(
        datetime.combine(end_date + timedelta(days=1), daytime.min, zone).timestamp()
    )

    # Handle multiple senders with OR operator
    if isinstance(sender, list):
        sender_parts = [f"from:{s}" for s in sender]
        sender_clause = f'({" OR ".join(sender_parts)})'
    else:
        sender_clause = f"from:{sender}"

    base = f"{sender_clause} after:{after} before:{before}"

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

            sleep_time = delay * (backoff**i) + random.uniform(0, 1)
            log.warning(f"Request failed ({e}), retrying in {sleep_time:.2f}s...")
            time.sleep(sleep_time)


def iter_message_pages(
    service, q: str, max_results: Optional[int] = None, *, counts=None
):
    """Yield bounded pages, consuming all continuation tokens by default.

    An explicit cap raises IncompleteRun if more messages remain, so callers
    cannot mistake a partial fetch for complete coverage. A retry starts the
    same date window again; already committed pages are idempotent.
    """
    if max_results is not None and max_results < 1:
        raise ValueError("max_results must be positive or None")
    token = None
    seen_tokens = set()
    fetched = 0
    while True:
        page_size = min(100, max_results - fetched) if max_results else 100
        response = execute_with_retry(
            service.users()
            .messages()
            .list(userId="me", q=q, maxResults=page_size, pageToken=token)
        )
        page = []
        ids = response.get("messages", []) or []
        for message in ids[:page_size]:
            try:
                full = execute_with_retry(
                    service.users()
                    .messages()
                    .get(userId="me", id=message["id"], format="full")
                )
            except Exception:
                if counts is not None:
                    counts["failed"] += 1
                raise
            if counts is not None:
                counts["fetched"] += 1
            if full.get("id") != message["id"]:
                raise ValueError("Gmail returned a mismatched message identity")
            page.append(full)
        fetched += len(page)
        if page:
            yield page
        token = response.get("nextPageToken")
        if len(ids) > page_size or (token and max_results and fetched >= max_results):
            raise db_pipeline.IncompleteRun(
                "Fetch cap reached; window remains retryable"
            )
        if not token:
            return
        if token in seen_tokens:
            raise db_pipeline.IncompleteRun("Gmail repeated a continuation token")
        seen_tokens.add(token)


def fetch_messages_for_query(
    service, q: str, max_results: Optional[int] = None
) -> List[Dict]:
    """Compatibility helper; production ingestion streams one page at a time."""
    return [
        message
        for page in iter_message_pages(service, q, max_results)
        for message in page
    ]


def save_samples(
    messages: List[Dict], ym_folder: str, index_path: pathlib.Path, *, counts=None
):
    """Saves raw email JSONs to disk, creates a summary index, and inserts metadata into the DB."""
    outdir = RAW_DIR / ym_folder
    outdir.mkdir(parents=True, exist_ok=True)
    index = []

    # List for batch DB insert
    db_items = []

    for msg in messages:
        mid = msg.get("id")
        if not mid or not all(c.isalnum() or c in "_-" for c in mid):
            raise ValueError("Invalid Gmail message identity")
        # build simplified metadata
        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        body = extract_body_from_message(msg)
        item = {
            "id": mid,
            "threadId": msg.get("threadId"),
            "from": headers.get("from"),
            "to": headers.get("to"),
            "subject": headers.get("subject"),
            "date": (
                datetime.fromtimestamp(
                    int(msg["internalDate"]) / 1000, timezone.utc
                ).isoformat()
                if msg.get("internalDate")
                else headers.get("date")
            ),
            "snippet": msg.get("snippet"),
            "raw_body": body,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        # write per-message raw file
        fname = outdir / f"msg_{mid}.json"
        with open(fname, "w", encoding="utf-8") as f:
            json.dump(msg, f, ensure_ascii=False, indent=2)
        index.append(item)
        db_items.append(item)

    # write index
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    log.info(
        "Saved %d messages raw to %s and index to %s", len(messages), outdir, index_path
    )

    # A disk copy is not a successful database write. Let persistence errors
    # propagate so the window remains retryable and the job exits nonzero.
    try:
        inserted = db_raw_emails.insert_raw_emails(db_items)
    except Exception:
        if counts is not None:
            counts["failed"] += len(db_items)
        raise
    if counts is not None:
        counts["stored"] += inserted
        counts["skipped"] += len(db_items) - inserted
    return index_path


def ingest_range(
    sender, start: date, end: date, keywords=None, max_results=None, mode="monthly"
):
    """Stream complete windows; checkpoint only after every page is persisted."""
    if start > end:
        raise ValueError("Ingestion start must not be after end")
    scope = db_pipeline.ingestion_scope(sender, keywords)
    query = build_gmail_query(sender, start, end, keywords)
    with db_pipeline.stage_run(
        "ingestion",
        scope=scope,
        start=start,
        end=end,
        advance_checkpoint=end < db_pipeline.pipeline_today(),
    ) as counts:
        counts.update(fetched=0, stored=0, skipped=0, pages=0, failed=0)
        with db_pipeline.ingestion_lock(scope):
            service = get_gmail_service()
            index_paths = []
            for page_number, page in enumerate(
                iter_message_pages(service, query, max_results, counts=counts), 1
            ):
                # Unique attempt directories avoid overwriting an earlier run's
                # index while retaining each page for diagnosis.
                if not index_paths:
                    import uuid

                    run_folder = (
                        f"{start.isoformat()}_{end.isoformat()}_{uuid.uuid4().hex}"
                    )
                path = DAILY_DIR / run_folder / f"page_{page_number:05d}.json"
                save_samples(
                    page, f"{start.year:04d}-{start.month:02d}", path, counts=counts
                )
                index_paths.append(path)
                counts["pages"] += 1
            log.info("Ingestion complete: %s", counts)
            return index_paths


def get_keywords():
    """Returns a list of keywords used to filter relevant transaction emails."""
    return [
        "receipt",
        "payment",
        "transaction",
        "debited",
        "credited",
        "withdrawal",
        "order",
        "txn",
        "failed",
        "attempted",
    ]


# CLI entrypoint
def cli():
    """Parses command-line arguments and triggers the appropriate ingestion mode (monthly/daily/incremental)."""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        type=str,
        choices=["monthly", "daily", "incremental"],
        default="monthly",
        help="Ingestion mode",
    )

    # Date args
    parser.add_argument("--year", type=int, help="Year (e.g. 2025)")
    parser.add_argument("--month", type=int, help="Month number (1-12)")
    parser.add_argument("--day", type=int, help="Day number (1-31)")

    # Shortcuts
    parser.add_argument(
        "--prev-month",
        action="store_true",
        help="Run for previous month (monthly mode)",
    )
    parser.add_argument(
        "--yesterday", action="store_true", help="Run for yesterday (daily mode)"
    )
    parser.add_argument(
        "--today", action="store_true", help="Run for today (daily mode)"
    )  # just in case

    parser.add_argument(
        "--max-results",
        type=int,
        default=None,
        help="Optional cap; reaching it with more pages fails the run without advancing coverage",
    )
    parser.add_argument(
        "--overlap-days", type=int, default=7, help="Recent days to rescan (default 7)"
    )
    parser.add_argument(
        "--initial-days",
        type=int,
        default=30,
        help="Initial lookback without a checkpoint (default 30)",
    )
    parser.add_argument(
        "--sender",
        type=str,
        default=None,
        help="Sender email address (default: both HDFC senders)",
    )
    args = parser.parse_args()

    # Use both HDFC senders by default, or single sender if specified
    sender = args.sender if args.sender else HDFC_SENDERS
    keywords = get_keywords()

    if args.mode == "monthly":
        if args.prev_month:
            today = db_pipeline.pipeline_today()
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
            target_date = db_pipeline.pipeline_today() - relativedelta(days=1)
        elif args.today:
            target_date = db_pipeline.pipeline_today()
        elif args.year and args.month and args.day:
            target_date = date(args.year, args.month, args.day)
        else:
            parser.error(
                "Daily mode requires --yesterday, --today, OR --year, --month, --day"
            )
            return

        log.info(f"Daily Ingest: {target_date}")
        ingest_range(
            sender, target_date, target_date, keywords, args.max_results, mode="daily"
        )

    elif args.mode == "incremental":
        scope = db_pipeline.ingestion_scope(sender, keywords)
        start_date, end_date = db_pipeline.incremental_window(
            scope, overlap_days=args.overlap_days, initial_days=args.initial_days
        )
        log.info("Incremental ingestion: %s through %s", start_date, end_date)
        ingest_range(
            sender, start_date, end_date, keywords, args.max_results, mode="daily"
        )


if __name__ == "__main__":
    cli()
