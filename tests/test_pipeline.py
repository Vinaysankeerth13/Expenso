"""Pipeline completeness, recovery, health and nightly-exit regressions."""

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from app import db_pipeline, ingest
from app.pipeline_health import assess_health, get_pipeline_health
from postgres_support import TemporaryPostgres


class Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class Gmail:
    def __init__(self, count, fail_at=None, empty_first=False):
        self.count = count
        self.fail_at = fail_at
        self.empty_first = empty_first
        self.list_calls = 0

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **kwargs):
        self.list_calls += 1
        token = kwargs["pageToken"]
        if self.empty_first and token is None:
            return Request({"messages": [], "nextPageToken": "empty"})
        start = int(token) if token and token != "empty" else 0
        end = min(start + kwargs["maxResults"], self.count)
        result = {"messages": [{"id": f"m{i}"} for i in range(start, end)]}
        if end < self.count:
            result["nextPageToken"] = str(end)
        return Request(result)

    def get(self, **kwargs):
        i = int(kwargs["id"][1:])
        if i == self.fail_at:
            return Request(RuntimeError("simulated provider failure"))
        return Request(
            {
                "id": kwargs["id"],
                "snippet": "Synthetic",
                "payload": {
                    "headers": [
                        {"name": "from", "value": "bank@example.invalid"},
                        {"name": "date", "value": "25 Sep 2026 12:00:00 +0000"},
                    ]
                },
            }
        )


class PaginationTests(unittest.TestCase):
    def test_all_pages_including_empty_continuation_page(self):
        service = Gmail(601, empty_first=True)
        rows = ingest.fetch_messages_for_query(service, "synthetic")
        self.assertEqual(len(rows), 601)
        self.assertEqual(service.list_calls, 8)

    def test_explicit_cap_never_returns_a_complete_result(self):
        with self.assertRaises(db_pipeline.IncompleteRun):
            ingest.fetch_messages_for_query(Gmail(501), "synthetic", max_results=500)
        self.assertEqual(
            len(ingest.fetch_messages_for_query(Gmail(500), "synthetic", 500)), 500
        )

    def test_repeated_continuation_token_fails(self):
        service = Gmail(0)
        with patch.object(
            service, "list", return_value=Request({"nextPageToken": "same"})
        ):
            with self.assertRaises(db_pipeline.IncompleteRun):
                list(ingest.iter_message_pages(service, "synthetic"))

    def test_search_bounds_use_configured_timezone(self):
        with patch.dict(os.environ, {"EXPENSO_TIMEZONE": "Asia/Kolkata"}):
            query = ingest.build_gmail_query(
                "bank@example.invalid", date(2026, 9, 1), date(2026, 9, 1)
            )
        self.assertIn("after:1788200999", query)
        self.assertIn("before:1788287400", query)


class PipelineDatabaseTests(TemporaryPostgres):
    def test_partial_extraction_progress_survives_later_failure(self):
        from app.pipeline import run_stage
        from app.db_extraction import store_extraction

        self.raw.insert_raw_emails(
            [
                {
                    "id": "later",
                    "snippet": "Synthetic",
                    "date": "2 September 2026 12:00:00 +0000",
                }
            ]
        )

        def fail_later(event):
            if event["raw_email_id"] == "later":
                raise RuntimeError("synthetic later failure")
            return store_extraction(event)

        with patch("app.extract_transactions.store_extraction", side_effect=fail_later):
            with self.assertRaises(RuntimeError):
                run_stage("extraction")
        self.assertEqual(
            self.sql(
                "SELECT status, counts->>'review', counts->>'failed' FROM pipeline_runs"
            ),
            [("failed", "1", "1")],
        )
        self.assertEqual(self.db.get_unprocessed_email_ids(), ["later"])

    def test_failed_review_retry_remains_eligible(self):
        from app.extract_transactions import extract_and_insert_transactions

        emails = self.db.get_raw_emails_by_ids(["fixture"])
        extract_and_insert_transactions(emails)
        self.assertEqual(self.db.get_unprocessed_email_ids(), [])
        with patch(
            "app.extract_transactions.store_extraction",
            side_effect=RuntimeError("injected"),
        ):
            with self.assertRaises(RuntimeError):
                extract_and_insert_transactions(emails)
        self.assertEqual(self.db.get_unprocessed_email_ids(), ["fixture"])
        self.assertEqual(get_pipeline_health()["counts"]["extraction_backlog"], 1)

    def run_ingest(
        self, service, start=date(2026, 9, 1), end=date(2026, 9, 2), cap=None
    ):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(ingest, "get_gmail_service", return_value=service),
            patch.object(ingest, "RAW_DIR", Path(tmp) / "raw"),
            patch.object(ingest, "DAILY_DIR", Path(tmp) / "daily"),
        ):
            return ingest.ingest_range(
                "bank@example.invalid", start, end, max_results=cap
            )

    def test_ingestion_full_window_counts_and_idempotent_overlap(self):
        scope = db_pipeline.ingestion_scope("bank@example.invalid", None)
        self.run_ingest(Gmail(501))
        self.assertEqual(
            db_pipeline.get_checkpoint(scope)["covered_through"], date(2026, 9, 2)
        )
        self.assertEqual(
            self.sql("SELECT counts->>'stored', status FROM pipeline_runs"),
            [("501", "success")],
        )
        self.run_ingest(Gmail(502))
        self.assertEqual(
            self.sql(
                "SELECT counts->>'stored', counts->>'skipped' FROM pipeline_runs ORDER BY id DESC LIMIT 1"
            ),
            [("1", "501")],
        )
        self.assertEqual(self.sql("SELECT COUNT(*) FROM raw_emails"), [(503,)])

    def test_failed_later_page_preserves_checkpoint_and_retries_saved_pages(self):
        scope = db_pipeline.ingestion_scope("bank@example.invalid", None)
        self.run_ingest(Gmail(1))
        with self.assertRaises(RuntimeError):
            self.run_ingest(Gmail(201, fail_at=100), end=date(2026, 9, 3))
        self.assertEqual(
            db_pipeline.get_checkpoint(scope)["covered_through"], date(2026, 9, 2)
        )
        self.assertEqual(
            self.sql(
                "SELECT status, counts->>'stored' FROM pipeline_runs ORDER BY id DESC LIMIT 1"
            ),
            [("failed", "99")],
        )
        self.run_ingest(Gmail(201), end=date(2026, 9, 3))
        self.assertEqual(
            db_pipeline.get_checkpoint(scope)["covered_through"], date(2026, 9, 3)
        )
        self.assertEqual(
            self.sql(
                "SELECT counts->>'stored', counts->>'skipped' FROM pipeline_runs ORDER BY id DESC LIMIT 1"
            ),
            [("101", "100")],
        )

    def test_capped_or_failed_storage_cannot_create_checkpoint(self):
        scope = db_pipeline.ingestion_scope("bank@example.invalid", None)
        with self.assertRaises(db_pipeline.IncompleteRun):
            self.run_ingest(Gmail(101), cap=100)
        self.assertIsNone(db_pipeline.get_checkpoint(scope))
        with patch.object(
            ingest.db_raw_emails,
            "insert_raw_emails",
            side_effect=RuntimeError("storage failed"),
        ):
            with self.assertRaises(RuntimeError):
                self.run_ingest(Gmail(1))
        self.assertIsNone(db_pipeline.get_checkpoint(scope))
        self.assertEqual(
            self.sql("SELECT status FROM pipeline_runs ORDER BY id"),
            [("incomplete",), ("failed",)],
        )

    def test_disconnected_window_does_not_claim_gap_coverage(self):
        scope = db_pipeline.ingestion_scope("bank@example.invalid", None)
        self.run_ingest(Gmail(0))
        self.run_ingest(Gmail(0), date(2026, 9, 10), date(2026, 9, 11))
        self.assertEqual(
            db_pipeline.get_checkpoint(scope)["covered_through"], date(2026, 9, 2)
        )
        start, end = db_pipeline.incremental_window(
            scope, today=date(2026, 9, 12), overlap_days=7
        )
        self.assertEqual((start, end), (date(2026, 8, 27), date(2026, 9, 11)))

    def test_missing_checkpoint_ignores_latest_message_date(self):
        start, end = db_pipeline.incremental_window(
            "new-scope", today=date(2026, 9, 26)
        )
        self.assertEqual((start, end), (date(2026, 8, 27), date(2026, 9, 25)))

    def test_failed_first_scan_keeps_its_oldest_day_when_calendar_advances(self):
        scope = db_pipeline.ingestion_scope("bank@example.invalid", None)
        with self.assertRaises(db_pipeline.IncompleteRun):
            self.run_ingest(Gmail(101), date(2026, 8, 1), date(2026, 9, 2), cap=100)
        start, end = db_pipeline.incremental_window(scope, today=date(2026, 9, 26))
        self.assertEqual((start, end), (date(2026, 8, 1), date(2026, 9, 25)))
        self.run_ingest(Gmail(0), start, end)
        self.assertEqual(db_pipeline.pending_ingestion_window(scope)["count"], 0)

    def test_mid_page_failure_reports_fetched_but_unpersisted_messages(self):
        with self.assertRaises(RuntimeError):
            self.run_ingest(Gmail(50, fail_at=20))
        self.assertEqual(
            self.sql(
                "SELECT counts->>'fetched', counts->>'stored', counts->>'unpersisted', counts->>'failed' FROM pipeline_runs"
            ),
            [("20", "0", "20", "1")],
        )

    def test_historical_snapshot_is_read_only_and_does_not_mutate_rows(self):
        from app.historical_repair import read_snapshot, build_report

        self.db.insert_transactions([self.event()])
        before = self.sql("SELECT transaction_id, amount, updated_at FROM transactions")
        report = build_report(*read_snapshot())
        self.assertEqual(report["stored_transaction_count"], 1)
        self.assertEqual(
            self.sql("SELECT transaction_id, amount, updated_at FROM transactions"),
            before,
        )
        self.assertEqual(self.sql("SELECT COUNT(*) FROM pipeline_runs"), [(0,)])

    def test_scope_lock_prevents_concurrent_scan(self):
        scope = db_pipeline.ingestion_scope("bank@example.invalid", None)
        with db_pipeline.ingestion_lock(scope):
            with self.assertRaises(db_pipeline.IncompleteRun):
                with db_pipeline.ingestion_lock(scope):
                    self.fail("Concurrent ingestion entered its critical section")

    def test_failed_classification_remains_retryable_and_stage_is_incomplete(self):
        from app.pipeline import run_stage

        self.db.insert_transactions([self.event()])
        fallback = {
            "category": "Other",
            "source": "rule",
            "confidence": 0,
            "rationale": "temporary outage",
        }
        with patch(
            "app.classify_transactions.classify_transaction", return_value=fallback
        ):
            with self.assertRaises(db_pipeline.IncompleteRun):
                run_stage("classification")
        self.assertEqual(self.sql("SELECT category FROM transactions"), [(None,)])
        self.assertEqual(
            self.sql("SELECT status FROM pipeline_runs"), [("incomplete",)]
        )

    def test_outcomes_and_readiness_are_visible(self):
        from app.pipeline import run_stage
        from fastapi.testclient import TestClient
        from app.main import app

        run_stage("extraction")  # the fixture's synthetic text requires review
        self.assertEqual(
            self.sql("SELECT outcome FROM email_processing_outcomes"), [("review",)]
        )
        result = get_pipeline_health()
        self.assertEqual(result["counts"]["review_backlog"], 1)
        self.assertNotEqual(result["status"], "healthy")
        with TestClient(app) as client:
            self.assertEqual(client.get("/ready").status_code, 200)
            self.assertEqual(client.get("/pipeline").status_code, 200)

    def test_processing_failure_is_recorded_and_still_retryable(self):
        from app.extract_transactions import extract_and_insert_transactions

        with patch(
            "app.extract_transactions.store_extraction",
            side_effect=RuntimeError("injected"),
        ):
            with self.assertRaises(RuntimeError):
                extract_and_insert_transactions(
                    self.db.get_raw_emails_by_ids(["fixture"])
                )
        self.assertEqual(
            self.sql("SELECT outcome, error_code FROM email_processing_outcomes"),
            [("failed", "RuntimeError")],
        )
        self.assertEqual(self.db.get_unprocessed_email_ids(), ["fixture"])


class HealthTests(unittest.TestCase):
    def snapshot(self):
        now = datetime.now(timezone.utc)
        return {
            "coverage": {
                "covered_through": now.date() - timedelta(days=1),
                "last_success_at": now,
            },
            "stages": {
                stage: {"status": "success", "finished_at": now}
                for stage in ("ingestion", "extraction", "classification")
            },
            "counts": {"recent_candidates": 0, "recent_accepted": 0},
        }

    def test_completed_empty_search_is_healthy_but_quality_is_unknown(self):
        result = assess_health(self.snapshot())
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["activity"], "no_recent_candidates")
        self.assertEqual(result["quality_evidence"], "insufficient_sample")

    def test_candidates_without_events_are_not_healthy(self):
        data = self.snapshot()
        data["counts"]["recent_candidates"] = 10
        self.assertEqual(assess_health(data)["status"], "unknown")
        data["counts"]["extraction_backlog"] = 10
        self.assertEqual(assess_health(data)["status"], "incomplete")

    def test_interrupted_and_failed_stages_are_not_success(self):
        data = self.snapshot()
        data["stages"]["ingestion"]["status"] = "running"
        self.assertEqual(assess_health(data)["status"], "incomplete")
        data["stages"]["ingestion"]["status"] = "failed"
        self.assertEqual(assess_health(data)["status"], "failed")

    def test_zero_sample_parsing_check_returns_unknown_without_alert(self):
        from scripts import check_parsing_health as quality

        with (
            patch.object(
                quality.db_health,
                "get_blank_merchant_stats",
                return_value={"blank": 0, "total": 0},
            ),
            patch.object(
                quality.db_health,
                "get_other_category_stats",
                return_value={"other": 0, "total": 0},
            ),
            patch.object(quality, "_log"),
            patch.object(quality, "send_email") as sender,
        ):
            self.assertIsNone(quality.check_parsing_health())
            sender.assert_not_called()


class NightlyTests(unittest.TestCase):
    def test_job_returns_failure_and_continues_backlog_stages(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "scripts").mkdir()
            shutil.copy(root / "scripts/daily_job.sh", base / "scripts/daily_job.sh")
            fake = base / "fake-python"
            fake.write_text(
                '#!/bin/bash\nprintf "%s\\n" "$*" >> "$PHASE_TRACE"\ncase "$*" in *app.ingest*) exit 7;; *) exit 0;; esac\n'
            )
            fake.chmod(0o700)
            trace = base / "trace"
            result = subprocess.run(
                ["bash", str(base / "scripts/daily_job.sh")],
                env={
                    **os.environ,
                    "EXPENSO_PYTHON": str(fake),
                    "PHASE_TRACE": str(trace),
                },
                capture_output=True,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("app.pipeline extraction", trace.read_text())
            self.assertIn("app.pipeline classification", trace.read_text())
            self.assertIn("check_pipeline_health.py", trace.read_text())
