"""
Transaction Extraction Script

Reads raw emails from the database and extracts transaction events.
Only validated successful events enter the ledger; other messages await review.

Usage:
    # Extract all unprocessed emails
    poetry run python app/extract_transactions.py --unprocessed-only

    # Extract specific number of emails (for testing)
    poetry run python app/extract_transactions.py --limit 10

    # Compatibility alias for unprocessed extraction
    poetry run python app/extract_transactions.py --all

    # Retry reviewed messages after a parser fix
    poetry run python app/extract_transactions.py --retry-review --limit 100
"""

import sys
import os
sys.path.append(os.getcwd())

import argparse
import logging
from typing import List, Dict

from app import db_transactions
from app.transaction_event import extract_transaction
from app.db_extraction import store_extraction, get_reviews

log = logging.getLogger(__name__)


def extract_and_insert_transactions(emails: List[Dict], filter_success_only: bool = True, *, progress=None) -> Dict[str, int]:
    """
    Extracts transactions from raw emails and inserts into database.

    Args:
        emails: List of raw email dicts
        filter_success_only: Retained for compatibility; only validated success
            events enter the ledger. Other messages are always queued for review.

    Returns:
        Dict with extraction statistics
    """
    stats = {
        'total_emails': len(emails),
        'extracted': 0,
        'success': 0,
        'failed': 0,
        'attempted': 0,
        'unknown': 0,
        'inserted': 0,
        'skipped': 0,
        'errors': 0,
        'review': 0,
        'rejected': 0,
        'duplicate': 0
    }

    for email in emails:
        event = extract_transaction(email)
        stats['extracted'] += 1
        status = event.get('status', 'unknown')
        stats[status] = stats.get(status, 0) + 1
        # Even --include-failed cannot admit unconfirmed movement to the ledger.
        # Non-success and malformed events remain visible in the review queue.
        try:
            outcome = store_extraction(event)
        except Exception as exc:
            if progress is not None:
                progress['failed'] = progress.get('failed', 0) + 1
            from app.db_connection import get_connection
            from app.db_pipeline import record_outcome
            try:
                conn = get_connection()
                try:
                    with conn:
                        record_outcome(conn, event, 'failed', type(exc).__name__)
                finally:
                    conn.close()
            except Exception:
                log.exception('Could not persist message failure; original error follows')
            raise
        stats[outcome] += 1
        if progress is not None:
            progress[outcome] = progress.get(outcome, 0) + 1
        if outcome in ('review', 'rejected'):
            stats['skipped'] += 1

    return stats


def print_stats(stats: Dict[str, int]):
    """Pretty print extraction statistics."""
    print("\n" + "="*60)
    print("📊 EXTRACTION STATISTICS")
    print("="*60)
    print(f"Total emails processed:     {stats['total_emails']}")
    print(f"Transactions extracted:     {stats['extracted']}")
    print(f"\n--- By Status ---")
    print(f"  ✅ Success:               {stats['success']}")
    print(f"  ❌ Failed:                {stats['failed']}")
    print(f"  ⏳ Attempted:             {stats['attempted']}")
    print(f"  ❓ Unknown:               {stats['unknown']}")
    print(f"\n--- Database Operations ---")
    print(f"  💾 Inserted to DB:        {stats['inserted']}")
    print(f"  ⏭️  Excluded from ledger:  {stats['skipped']}")
    print(f"  🔎 Awaiting review:        {stats['review']}")
    print(f"  Known non-transactions:   {stats['rejected']}")
    print(f"  ↔️  Duplicate notices:     {stats['duplicate']}")
    print(f"  ⚠️  Errors:                {stats['errors']}")
    print("="*60 + "\n")


def extract_unprocessed():
    """Extract transactions from emails not yet processed."""
    log.info("Finding unprocessed emails...")
    unprocessed_ids = db_transactions.get_unprocessed_email_ids()

    if not unprocessed_ids:
        log.info("✅ No unprocessed emails found. All caught up!")
        return

    log.info(f"Found {len(unprocessed_ids)} unprocessed emails")

    # Fetch email data
    emails = db_transactions.get_raw_emails_by_ids(unprocessed_ids)
    log.info(f"Retrieved {len(emails)} email records from database")

    # Extract and insert
    stats = extract_and_insert_transactions(emails, filter_success_only=True)
    print_stats(stats)


def extract_all():
    """
    Compatibility alias for unprocessed extraction, not a historical backfill.
    Replay now preserves classification, but parser/event-identity changes still
    need a separate reviewed backfill workflow before enabling full re-extraction.
    """
    log.warning("--all is currently equivalent to --unprocessed-only.")

    extract_unprocessed()


def extract_with_limit(limit: int):
    """Extract transactions from limited number of emails (for testing)."""
    log.info(f"Fetching up to {limit} unprocessed emails...")

    unprocessed_ids = db_transactions.get_unprocessed_email_ids()

    if not unprocessed_ids:
        log.info("✅ No unprocessed emails found.")
        return

    # Limit the IDs
    limited_ids = unprocessed_ids[:limit]
    log.info(f"Processing {len(limited_ids)} emails (out of {len(unprocessed_ids)} unprocessed)")

    # Fetch and process
    emails = db_transactions.get_raw_emails_by_ids(limited_ids)
    stats = extract_and_insert_transactions(emails, filter_success_only=True)
    print_stats(stats)


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Extract transactions from raw emails and insert into database"
    )

    parser.add_argument(
        '--unprocessed-only',
        action='store_true',
        help='Extract only from emails not yet processed (default behavior)'
    )

    parser.add_argument(
        '--all',
        action='store_true',
        help='Compatibility alias for --unprocessed-only; does not backfill history'
    )

    parser.add_argument(
        '--limit',
        type=int,
        help='Limit number of emails to process (useful for testing)'
    )

    parser.add_argument(
        '--include-failed',
        action='store_true',
        help='Compatibility option: failed/attempted messages always go to review'
    )

    parser.add_argument(
        '--stats',
        action='store_true',
        help='Show transaction statistics and exit'
    )

    parser.add_argument('--retry-review', action='store_true',
                        help='Reparse up to --limit (default 100) review messages after a parser fix')
    args = parser.parse_args()

    # Show stats only
    if args.stats:
        stats = db_transactions.get_transaction_stats()
        print("\n" + "="*60)
        print("📊 TRANSACTION DATABASE STATISTICS")
        print("="*60)
        for key, value in stats.items():
            print(f"{key:30s}: {value}")
        print("="*60 + "\n")
        return

    # Default: unprocessed only
    if args.retry_review:
        reviews = get_reviews(limit=args.limit or 100)['reviews']
        emails = db_transactions.get_raw_emails_by_ids([r['raw_email_id'] for r in reviews])
        print_stats(extract_and_insert_transactions(emails))
    elif args.limit:
        extract_with_limit(args.limit)
    elif args.all:
        extract_all()
    else:
        # Default: unprocessed only
        extract_unprocessed()

    # Show final stats
    log.info("Fetching updated transaction statistics...")
    stats = db_transactions.get_transaction_stats()
    print("\n📈 Current Database State:")
    print(f"  Total transactions: {stats.get('total_transactions', 0)}")
    print(f"  Successful: {stats.get('success_count', 0)}")
    print(f"  Total spent: ₹{stats.get('total_spent', 0):,.2f}")
    print(f"  Total received: ₹{stats.get('total_received', 0):,.2f}")
    print(f"  Date range: {stats.get('earliest_transaction')} to {stats.get('latest_transaction')}")
    print(f"  Unique merchants: {stats.get('distinct_merchants', 0)}\n")


if __name__ == "__main__":
    main()
