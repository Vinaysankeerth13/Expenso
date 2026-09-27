"""Reviewed corrections must be atomic, conservative and preserve original rows."""

import copy
from decimal import Decimal
from unittest.mock import patch

from psycopg2.extras import RealDictCursor
from postgres_support import TemporaryPostgres
from app.db_extraction import get_reviews, store_extraction
from app.reviewed_repair import run_repair, snapshot, message_state
from app.transaction_event import extract_transaction
from app.pipeline_health import get_pipeline_health


class ReviewedRepairTests(TemporaryPostgres):
    def state(self):
        conn = self.connect()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                return snapshot(cur)
        finally:
            conn.close()

    def manifest(self, action, raw_id="fixture"):
        before = message_state(self.state(), raw_id)
        event = extract_transaction(dict(before["raw"], id=raw_id))
        return {
            "batch_id": "synthetic-review",
            "items": [
                {
                    "raw": before["raw"],
                    "transactions": before["transactions"],
                    "action": action,
                    "reason": "Synthetic reviewed evidence",
                    "expected_event": event,
                }
            ],
        }

    def purchase(self):
        self.sql(
            "UPDATE raw_emails SET snippet=%s, raw_body=NULL WHERE gmail_message_id='fixture'",
            (
                "HDFC Bank Credit Card ending in 1234. You made a transaction of Rs.100.00 at Test Shop on 01-09-2026 10:20:00.",
            ),
        )

    def test_preview_apply_and_repeat_protection(self):
        self.purchase()
        manifest = self.manifest("insert")
        before = self.state()
        preview = run_repair(manifest)
        self.assertEqual(self.state(), before)
        applied = run_repair(manifest, expected_digest=preview["digest"])
        self.assertTrue(applied["applied"])
        self.assertEqual(applied["transaction_count"], 1)
        self.assertEqual(
            self.sql("SELECT action FROM historical_correction_audit"), [("insert",)]
        )
        with self.assertRaisesRegex(ValueError, "already been applied"):
            run_repair(manifest, expected_digest=preview["digest"])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM transactions"), [(1,)])

    def test_stale_preview_aborts(self):
        self.purchase()
        manifest = self.manifest("insert")
        preview = run_repair(manifest)
        self.sql("UPDATE raw_emails SET subject='changed'")
        with self.assertRaises(ValueError):
            run_repair(manifest, expected_digest=preview["digest"])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM transactions"), [(0,)])

    def test_quarantine_preserves_manual_fields_and_audit(self):
        self.db.insert_transactions(
            [
                self.event(
                    category="Food & Dining",
                    classification_source="manual",
                    notes="Keep this",
                )
            ]
        )
        manifest = self.manifest("quarantine")
        old = self.state()["transactions"][0]
        preview = run_repair(manifest)
        run_repair(manifest, expected_digest=preview["digest"])
        self.assertEqual(self.state()["transactions"][0], dict(old, status="unknown"))
        self.assertEqual(
            self.sql(
                "SELECT before_state->'transactions'->0->>'status' FROM historical_correction_audit"
            ),
            [("success",)],
        )
        self.assertEqual(self.db.get_transaction_stats()["total_spent"], Decimal(0))
        self.assertEqual(get_reviews()["count"], 0)

    def test_exclusions_have_evidence_but_no_review_or_retry_noise(self):
        self.sql("UPDATE raw_emails SET raw_body='Your OTP is 000000 for a purchase.'")
        manifest = self.manifest("exclude")
        preview = run_repair(manifest)
        run_repair(manifest, expected_digest=preview["digest"])
        self.assertEqual(get_reviews()["count"], 0)
        self.assertEqual(self.db.get_unprocessed_email_ids(), [])
        self.assertEqual(
            self.sql("SELECT outcome FROM email_processing_outcomes"), [("rejected",)]
        )
        self.assertEqual(get_pipeline_health()["counts"]["extraction_backlog"], 0)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM extraction_reviews"), [(1,)])

    def test_existing_event_cannot_be_silently_excluded(self):
        self.db.insert_transactions([self.event()])
        event = extract_transaction({"id": "fixture", "snippet": "Your OTP is 000000."})
        self.assertEqual(store_extraction(event), "review")
        self.assertEqual(get_reviews()["count"], 1)
        self.assertEqual(self.sql("SELECT status FROM transactions"), [("success",)])

    def test_nearby_duplicate_blocks_insert(self):
        self.purchase()
        manifest = self.manifest("insert")
        self.sql(
            "INSERT INTO raw_emails(gmail_message_id, snippet) VALUES ('other', 'Synthetic')"
        )
        self.db.insert_transactions(
            [dict(self.event(), raw_email_id="other", date="2026-09-03")]
        )
        with self.assertRaisesRegex(ValueError, "duplicate candidate"):
            run_repair(manifest)

    def test_audit_failure_rolls_back_insert_and_outcomes(self):
        self.purchase()
        manifest = self.manifest("insert")
        preview = run_repair(manifest)
        self.sql(
            "ALTER TABLE historical_correction_audit ADD CONSTRAINT force_failure CHECK (action != 'insert')"
        )
        try:
            with self.assertRaises(Exception):
                run_repair(manifest, expected_digest=preview["digest"])
            self.assertEqual(self.sql("SELECT COUNT(*) FROM transactions"), [(0,)])
            self.assertEqual(
                self.sql("SELECT COUNT(*) FROM email_processing_outcomes"), [(0,)]
            )
        finally:
            self.sql(
                "ALTER TABLE historical_correction_audit DROP CONSTRAINT force_failure"
            )

    def test_confirm_preserves_every_transaction_field(self):
        self.purchase()
        event = extract_transaction(dict(self.state()["raw_emails"][0], id="fixture"))
        store_extraction(event)
        self.sql(
            "UPDATE transactions SET category='Personal', classification_source='manual', notes='Keep', source='unknown'"
        )
        old = copy.deepcopy(self.state()["transactions"])
        manifest = self.manifest("confirm")
        preview = run_repair(manifest)
        run_repair(manifest, expected_digest=preview["digest"])
        self.assertEqual(self.state()["transactions"], old)

    def test_later_admission_failure_rolls_back_earlier_insert(self):
        self.purchase()
        manifest = self.manifest("insert")
        self.sql(
            "INSERT INTO raw_emails(gmail_message_id, snippet) VALUES ('otp', 'Your OTP is 000000.')"
        )
        manifest["items"].extend(self.manifest("exclude", "otp")["items"])
        preview = run_repair(manifest)
        real_store = store_extraction

        def fail_second(event, **kwargs):
            if event["raw_email_id"] == "otp":
                raise RuntimeError("Synthetic failure")
            return real_store(event, **kwargs)

        with patch("app.reviewed_repair.store_extraction", side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                run_repair(manifest, expected_digest=preview["digest"])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM transactions"), [(0,)])
        self.assertEqual(
            self.sql("SELECT COUNT(*) FROM transaction_notifications"), [(0,)]
        )
