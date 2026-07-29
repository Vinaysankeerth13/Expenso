import argparse
import logging
from datetime import date
from dateutil.relativedelta import relativedelta
from tabulate import tabulate
import sys
import os
sys.path.append(os.getcwd())
from app.ingest import (
    get_gmail_service,
    fetch_messages_for_query,
    build_gmail_query,
    extract_body_from_message,
    HDFC_SENDERS,
)

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

def check_coverage(year=2025, month=8, sender=HDFC_SENDERS):
    """Fetches all emails for a month and reports how many are matched by current keywords vs. missed."""
    # 1. Get Keywords
    # We duplicate them here or import them? Importing is better to stay in sync.
    from app.ingest import get_keywords
    keywords = get_keywords()
    
    log.info(f"Checking coverage for {year}-{month:02d} for sender {sender}")
    log.info(f"Current Keywords: {keywords}")

    # 2. Fetch ALL emails (no keywords in query)
    start = date(year, month, 1)
    end = (start + relativedelta(months=1)) - relativedelta(days=1)
    
    # Pass None for extra_terms to get EVERYTHING
    query = build_gmail_query(sender, start, end, extra_terms=None)
    
    svc = get_gmail_service()
    log.info("Fetching ALL emails (this might take a moment)...")
    messages = fetch_messages_for_query(svc, q=query, max_results=1000)
    
    total = len(messages)
    log.info(f"Total emails found: {total}")
    
    matched = []
    missed = []
    
    # 3. Simulate Filtering
    for msg in messages:
        payload = msg.get("payload", {})
        headers = {h["name"]: h["value"] for h in payload.get("headers", [])}
        subject = headers.get("Subject", "").lower()
        snippet = msg.get("snippet", "").lower()
        body = extract_body_from_message(msg).lower()
        
        # Check against keywords
        is_match = False
        for k in keywords:
            if k in subject or k in snippet or k in body:
                is_match = True
                break
        
        info = {
            "subject": headers.get("Subject", "No Subject"),
            "date": headers.get("Date"),
            "snippet": msg.get("snippet", "")[:100]
        }
        
        if is_match:
            matched.append(info)
        else:
            missed.append(info)
            
    # 4. Report
    print("\n" + "="*60)
    print(f"COVERAGE REPORT FOR {year}-{month:02d}")
    print("="*60)
    print(f"Total Emails:   {total}")
    print(f"Matched:        {len(matched)} ({len(matched)/total*100:.1f}%)")
    print(f"Missed:         {len(missed)} ({len(missed)/total*100:.1f}%)")
    print("-" * 60)
    
    if missed:
        print("\nMISSED EMAILS (Subject Lines):")
        # Group by subject to avoid spamming duplicates
        missed_counts = {}
        for m in missed:
            s = m["subject"]
            missed_counts[s] = missed_counts.get(s, 0) + 1
            
        table_data = [[subj, count] for subj, count in missed_counts.items()]
        print(tabulate(table_data, headers=["Subject", "Count"], tablefmt="grid"))
        
        print("\nAnalyze the list above. If you see valid transactions, add their keywords to app/ingest.py.")
    else:
        print("\nResult: 100% Coverage! No emails were skipped.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=2025, help="Year to check")
    parser.add_argument("--month", type=int, default=8, help="Month to check")
    args = parser.parse_args()
    
    check_coverage(year=args.year, month=args.month)
