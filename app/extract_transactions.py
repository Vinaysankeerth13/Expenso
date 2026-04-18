"""
Transaction Extraction Script

Reads raw emails from the database and extracts transaction events.
Only processes emails with status='success' (actual money movement).

Usage:
    # Extract all unprocessed emails
    poetry run python app/extract_transactions.py --unprocessed-only

    # Extract specific number of emails (for testing)
    poetry run python app/extract_transactions.py --limit 10

    # Extract all emails (re-process everything)
    poetry run python app/extract_transactions.py --all

    # Extract emails from a specific date range
    poetry run python app/extract_transactions.py --start-date 2025-12-01 --end-date 2025-12-31
"""

import sys
import os
sys.path.append(os.getcwd())

import argparse
import logging
from typing import List, Dict, Any
from datetime import datetime, date
from dateutil import parser as date_parser

from app import db_transactions
from app.transaction_event import extract_transaction, generate_transaction_id

log = logging.getLogger("extract_transactions")


def extract_and_insert_transactions(emails: List[Dict], filter_success_only: bool = True) -> Dict[str, int]:
    """
    Extracts transactions from raw emails and inserts into database.

    Args:
        emails: List of raw email dicts
        filter_success_only: If True, only insert transactions with status='success'

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
        'errors': 0
    }

    transactions_to_insert = []

    for email in emails:
        try:
            # Extract transaction event
            event = extract_transaction(email)
            stats['extracted'] += 1

            # Count by status
            status = event.get('status', 'unknown')
            stats[status] = stats.get(status, 0) + 1

            # Filter: only insert successful transactions (actual money movement)
            if filter_success_only and status != 'success':
                stats['skipped'] += 1
                log.debug(f"Skipping {status} transaction from email {email.get('id')}")
                continue

            # Generate transaction ID
            event['transaction_id'] = generate_transaction_id(event)

            transactions_to_insert.append(event)

        except Exception as e:
            stats['errors'] += 1
            log.error(f"Failed to extract transaction from email {email.get('id')}: {e}")

    # Batch insert transactions
    if transactions_to_insert:
        try:
            inserted_count = db_transactions.insert_transactions(transactions_to_insert)
            stats['inserted'] = inserted_count
            log.info(f"Successfully inserted {inserted_count} transactions")
        except Exception as e:
            log.error(f"Failed to insert transactions into database: {e}")
            raise
    else:
        log.info("No transactions to insert (all filtered out or extraction failed)")

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
    print(f"  ⏭️  Skipped (non-success): {stats['skipped']}")
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


def extract_all(limit: int = None):
    """Extract transactions from all emails (re-process everything)."""
    log.info("Fetching all emails from database...")

    # Use get_unprocessed_email_ids with a modified query or fetch all
    # For now, we'll just process unprocessed ones
    # To truly process ALL, we'd need a new query in db_transactions.py
    # For MVP, let's just warn the user

    log.warning("--all flag: Currently only processes unprocessed emails.")
    log.warning("To re-process ALL emails, truncate transactions table first:")
    log.warning("  psql -U postgres -d expenso -c 'TRUNCATE transactions;'")

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
        help='Re-process ALL emails (warning: may create duplicates if not careful)'
    )

    parser.add_argument(
        '--limit',
        type=int,
        help='Limit number of emails to process (useful for testing)'
    )

    parser.add_argument(
        '--include-failed',
        action='store_true',
        help='Include failed/attempted transactions (default: success only)'
    )

    parser.add_argument(
        '--stats',
        action='store_true',
        help='Show transaction statistics and exit'
    )

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
    if args.limit:
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
