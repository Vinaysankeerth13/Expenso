"""
check_parsing_health.py

Monitors extraction/classification *quality* (as opposed to
check_ingestion_health.sh, which monitors ingestion *freshness*). Catches
silent bank-template changes like the 2026-05-07 HDFC UPI wording change,
which didn't stop ingestion but broke merchant extraction and pushed most
outgoing spend into category='Other' for ~10 weeks before it was noticed.

Rather than trying to detect *that* the email wording changed, this tracks
proxy signals that degrade whenever parsing breaks, regardless of the cause:
  - blank-merchant rate on successful outgoing transactions (historically ~0%)
  - category='Other' rate on the same (historically ~0-2%)

If either crosses its threshold over the trailing window, sends an alert
email (reusing the same Gmail-send path as the ingestion-staleness alert)
that includes sample raw email snippets, so the actual new wording is
visible immediately instead of requiring a DB investigation.

Usage:
    poetry run python scripts/check_parsing_health.py
    poetry run python scripts/check_parsing_health.py --window-days 7

Intended to run daily via daily_job.sh, alongside check_ingestion_health.sh.
"""

import argparse
import os
import sys
sys.path.append(os.getcwd())

from datetime import datetime

from dotenv import load_dotenv

from app import db_health
from app.gmail_auth import send_email

load_dotenv()

LOG_FILE = "logs/parsing_health.log"

MIN_SAMPLE = 5  # don't alert on a handful of transactions — too noisy to be meaningful
BLANK_MERCHANT_THRESHOLD_PCT = 15.0
OTHER_CATEGORY_THRESHOLD_PCT = 20.0


def _log(line: str) -> None:
    print(line)
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def _format_examples(examples, fields) -> str:
    lines = []
    for ex in examples:
        parts = [f"{k}={ex.get(k)}" for k in fields]
        lines.append("  - " + " | ".join(parts))
        lines.append(f"    snippet: {ex.get('snippet', '')[:200]}")
    return "\n".join(lines)


def check_parsing_health(window_days: int = 7) -> bool:
    """Returns True if healthy, False if an alert was triggered (and sent)."""
    _log(f"{datetime.now():%Y-%m-%d %H:%M:%S} - Running parsing health check (window={window_days}d)...")

    blank = db_health.get_blank_merchant_stats(window_days)
    other = db_health.get_other_category_stats(window_days)

    breaches = []

    if blank["total"] >= MIN_SAMPLE:
        pct = 100.0 * blank["blank"] / blank["total"]
        _log(f"Blank merchant rate: {blank['blank']}/{blank['total']} ({pct:.1f}%)")
        if pct >= BLANK_MERCHANT_THRESHOLD_PCT:
            examples = db_health.get_blank_merchant_examples(window_days)
            breaches.append((
                "Blank merchant rate",
                f"{pct:.1f}% of outgoing transactions ({blank['blank']}/{blank['total']}) "
                f"have no merchant captured (threshold: {BLANK_MERCHANT_THRESHOLD_PCT}%).",
                _format_examples(examples, ["date", "source", "subject"]),
            ))
    else:
        _log(f"Blank merchant check skipped (only {blank['total']} transactions in window, need >= {MIN_SAMPLE})")

    if other["total"] >= MIN_SAMPLE:
        pct = 100.0 * other["other"] / other["total"]
        _log(f"'Other' category rate: {other['other']}/{other['total']} ({pct:.1f}%)")
        if pct >= OTHER_CATEGORY_THRESHOLD_PCT:
            examples = db_health.get_other_category_examples(window_days)
            breaches.append((
                "'Other' category rate",
                f"{pct:.1f}% of outgoing transactions ({other['other']}/{other['total']}) "
                f"landed in category='Other' (threshold: {OTHER_CATEGORY_THRESHOLD_PCT}%).",
                _format_examples(examples, ["date", "merchant"]),
            ))
    else:
        _log(f"'Other' category check skipped (only {other['total']} transactions in window, need >= {MIN_SAMPLE})")

    if not breaches:
        _log("Parsing health check passed.")
        return True

    subject = "Expenso Alert: Transaction parsing/classification quality has degraded"
    body_parts = [
        "Hey,\n",
        "The Expenso pipeline is ingesting emails fine, but extraction or "
        "classification quality has dropped — this usually means a bank changed "
        "an email template and the regex-based parser needs updating "
        "(see app/transaction_event.py and app/classify_transactions.py).\n",
    ]
    for title, summary, examples in breaches:
        body_parts.append(f"--- {title} ---\n{summary}\nExamples:\n{examples}\n")
    body_parts.append(
        "Check logs/parsing_health.log for the full numbers, or re-run:\n"
        "  poetry run python scripts/check_parsing_health.py\n\n— Expenso"
    )
    body = "\n".join(body_parts)

    for title, summary, _ in breaches:
        _log(f"⚠️  ALERT: {title} — {summary}")

    try:
        send_email(subject, body)
        _log("Alert email sent.")
    except RuntimeError as e:
        _log(f"WARNING: Failed to send alert email: {e}")

    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-days", type=int, default=7, help="Trailing window size in days (default: 7)")
    args = parser.parse_args()

    healthy = check_parsing_health(args.window_days)
    sys.exit(0 if healthy else 1)
