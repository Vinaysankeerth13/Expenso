"""Generate a private, read-only historical repair preview from stored emails."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import argparse
from datetime import datetime, timezone
from app.historical_repair import build_report, read_snapshot, write_report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/historical-repair")
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
    )
    args = parser.parse_args()
    report = build_report(*read_snapshot())
    path = write_report(report, args.output)
    print(f"Report: {path.resolve()}")
    print(
        f"Read-only: {report['raw_email_count']} emails, {report['stored_transaction_count']} stored transactions"
    )
    print("Actions:", report["actions"])


if __name__ == "__main__":
    main()
