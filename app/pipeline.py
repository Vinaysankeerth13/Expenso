"""Scheduled stage entry points with durable status and nonzero failure exits."""

import argparse
from app.db_pipeline import stage_run, IncompleteRun


def run_stage(stage):
    with stage_run(stage) as counts:
        if stage == "extraction":
            from app import db_transactions
            from app.extract_transactions import extract_and_insert_transactions

            ids = db_transactions.get_unprocessed_email_ids()
            counts.update(
                total_emails=len(ids), inserted=0, duplicate=0, review=0, failed=0
            )
            for offset in range(0, len(ids), 100):
                batch_ids = ids[offset : offset + 100]
                emails = db_transactions.get_raw_emails_by_ids(batch_ids)
                if len(emails) != len(batch_ids):
                    raise IncompleteRun("Some pending raw messages could not be loaded")
                extract_and_insert_transactions(emails, progress=counts)
        elif stage == "classification":
            from app.classify_transactions import batch_classify

            counts.update(batch_classify())
            if counts.get("failed"):
                raise IncompleteRun("Classification failures remain retryable")
            from app.db_transactions import get_uncategorized_transactions

            if get_uncategorized_transactions(limit=1):
                raise IncompleteRun("Classification batch limit left a backlog")
        else:
            raise ValueError("Unsupported pipeline stage")
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=("extraction", "classification"))
    args = parser.parse_args()
    run_stage(args.stage)
