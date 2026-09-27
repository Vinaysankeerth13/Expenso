"""Configuration and storage regressions using a disposable local cluster.

Run: poetry run python -m unittest discover -s tests -v
Set PG_BIN if initdb/pg_ctl are not on PATH (Postgres.app is also detected).
No existing database, Gmail account, or AI service is accessed.
"""

import importlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import psycopg2

ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    def test_entry_points_load_settings_before_consumers(self):
        modules = (
            "app.main",
            "app.ingest",
            "app.extract_transactions",
            "app.classify_transactions",
            "scripts.check_parsing_health",
            "scripts.send_alert",
            "app.pipeline",
            "scripts.check_pipeline_health",
            "scripts.stage_historical_repair",
        )
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "DB_NAME=phase1_config_test\nDB_USER=phase1_user\n"
                "DB_PASS=synthetic\nDB_HOST=config.invalid\nDB_PORT=12345\n"
                "GMAIL_CREDENTIALS=synthetic_credentials.json\n"
                "TOKEN_PATH=synthetic_token.json\n"
            )
            env = {k: v for k, v in os.environ.items() if not k.startswith("DB_")}
            env.pop("GMAIL_CREDENTIALS", None)
            env.pop("TOKEN_PATH", None)
            env["EXPENSO_ENV_FILE"] = str(env_file)
            for module in modules:
                for exported in (False, True):
                    with self.subTest(module=module, exported=exported):
                        case_env = dict(env)
                        expected = "exported_name" if exported else "phase1_config_test"
                        if exported:
                            case_env["DB_NAME"] = expected
                        code = f"""
import importlib
importlib.import_module({module!r})
from app import db_connection as db, gmail_auth as gmail
assert db.DB_NAME == {expected!r}
assert (db.DB_USER, db.DB_PASS, db.DB_HOST, db.DB_PORT) == (
    'phase1_user', 'synthetic', 'config.invalid', '12345')
assert gmail.CREDENTIALS_PATH == 'synthetic_credentials.json'
assert gmail.TOKEN_PATH == 'synthetic_token.json'
"""
                        subprocess.run(
                            [sys.executable, "-c", code],
                            cwd=ROOT,
                            env=case_env,
                            check=True,
                            capture_output=True,
                            text=True,
                        )


from postgres_support import TemporaryPostgres


class DatabaseTests(TemporaryPostgres):
    def test_baseline_and_raw_email_round_trip(self):
        self.execute_migrations()
        self.raw.insert_raw_emails([{"id": "fixture", "snippet": "duplicate"}])
        rows = self.db.get_raw_emails_by_ids(["fixture"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["raw_body"], "Synthetic body")
        self.assertEqual(rows[0]["bank"], "hdfc")
        self.assertEqual(self.db.get_unprocessed_email_ids(), ["fixture"])

    def test_manual_classification_survives_null_and_conflicting_replays(self):
        event = self.event()
        self.db.insert_transactions([event])
        self.assertTrue(
            self.db.update_transaction_category("event", "Shopping", notes="Keep me")
        )
        for category in (None, "Other"):
            self.assertEqual(
                self.db.insert_transactions(
                    [
                        dict(
                            event,
                            category=category,
                            classification_source="llm",
                            classification_confidence=0.2,
                        )
                    ]
                ),
                1,
            )
            self.assertEqual(
                self.sql(
                    "SELECT category, classification_source, "
                    "classification_confidence, notes FROM transactions"
                ),
                [("Shopping", "manual", 1.0, "Keep me")],
            )
            self.assertEqual(
                self.sql("SELECT COUNT(*), SUM(amount) FROM transactions"), [(1, 100)]
            )

    def test_replay_preserves_automated_classification_too(self):
        self.db.insert_transactions(
            [
                self.event(
                    category="Groceries",
                    classification_source="llm",
                    classification_confidence=0.75,
                )
            ]
        )
        self.db.insert_transactions([self.event()])
        self.assertEqual(
            self.sql(
                "SELECT category, classification_source, "
                "classification_confidence FROM transactions"
            ),
            [("Groceries", "llm", 0.75)],
        )

    def test_counts_across_batch_boundaries_and_repeated_ids(self):
        self.assertEqual(self.db.insert_transactions([]), 0)
        events = [self.event(str(i)) for i in range(205)]
        self.assertEqual(self.db.insert_transactions(events), 205)
        self.assertEqual(self.db.insert_transactions(events[:3]), 3)
        self.assertEqual(self.db.insert_transactions([events[0], events[0]]), 2)
        self.assertEqual(
            self.sql("SELECT COUNT(*), SUM(amount) FROM transactions"), [(205, 20500)]
        )

    def test_failed_batch_rolls_back_all_pages(self):
        events = [self.event(str(i)) for i in range(101)]
        events[-1]["raw_email_id"] = "missing"
        with self.assertRaises(psycopg2.IntegrityError):
            self.db.insert_transactions(events)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM transactions"), [(0,)])

    def test_null_categories_render_on_all_affected_pages(self):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.services import dashboard_service as service

        self.db.insert_transactions(
            [self.event(), self.event("other", category="Other")]
        )
        with patch.object(service, "_get_anomaly_callouts", return_value=[]):
            with TestClient(app) as client:
                for url in ("/year/2026", "/month/2026/9", "/transactions"):
                    with self.subTest(url=url):
                        response = client.get(url)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn("Unclassified", response.text)
                        self.assertIn("Other", response.text)
            self.assertEqual(
                service.get_month_page_data(2026, 9)["summary"]["totalSpent"], "₹200.00"
            )
        self.assertEqual(
            self.sql(
                "SELECT category, classification_source, classification_confidence "
                "FROM transactions WHERE transaction_id='event'"
            ),
            [(None, None, None)],
        )

    def notice(self, ident, text):
        from app.transaction_event import extract_transaction

        email = {
            "id": ident,
            "snippet": text,
            "raw_body": text,
            "date": "26 Sep 2026 12:00:00 +0000",
        }
        self.raw.insert_raw_emails([email])
        return extract_transaction(email)

    def test_extraction_reversal_pair_and_duplicate_notices(self):
        from app.db_extraction import store_extraction

        debit = self.notice(
            "debit",
            "Rs.500 debited from account 3947 via UPI to VPA shop@bank Test Shop on 26-09-26. Reference number is 12345.",
        )
        reversal = self.notice(
            "reversal",
            "Rs.500 credited to account 3947 via UPI on 26-09-26. Reversal reference number is 12345.",
        )
        self.assertEqual(store_extraction(debit), "inserted")
        self.assertEqual(store_extraction(reversal), "inserted")
        self.assertEqual(store_extraction(debit), "duplicate")
        self.assertEqual(store_extraction(reversal), "duplicate")
        duplicate = self.notice(
            "debit-copy",
            "Rs.500 debited from account 3947 via UPI to VPA shop@bank Test Shop on 26-09-26. Reference number is 12345.",
        )
        self.assertEqual(store_extraction(duplicate), "duplicate")
        self.assertEqual(
            self.sql(
                "SELECT COUNT(*), SUM(CASE WHEN direction='out' THEN -amount ELSE amount END) FROM transactions"
            ),
            [(2, 0)],
        )
        self.assertEqual(
            self.sql(
                "SELECT COUNT(*) FROM transactions WHERE original_transaction_id IS NOT NULL"
            ),
            [(1,)],
        )
        self.assertEqual(
            self.sql("SELECT COUNT(*) FROM transaction_notifications"), [(3,)]
        )
        self.assertNotIn("debit-copy", self.db.get_unprocessed_email_ids())

    def test_original_can_arrive_after_reversal(self):
        from app.db_extraction import store_extraction

        reversal = self.notice(
            "reversal",
            "Rs.500 credited to account 3947 via UPI on 26-09-26. Reversal reference number is 12345.",
        )
        debit = self.notice(
            "debit",
            "Rs.500 debited from account 3947 via UPI on 26-09-26. Reference number is 12345.",
        )
        self.assertEqual(store_extraction(reversal), "inserted")
        self.assertEqual(store_extraction(debit), "inserted")
        self.assertEqual(
            self.sql(
                "SELECT COUNT(*) FROM transactions WHERE original_transaction_id IS NOT NULL"
            ),
            [(1,)],
        )

    def test_card_duplicate_candidates_are_reviewed_not_merged(self):
        from app.db_extraction import store_extraction

        text = (
            "Rs.500 debited from credit card ending 1540 towards Test Shop on 26-09-26."
        )
        self.assertEqual(store_extraction(self.notice("card-1", text)), "inserted")
        self.assertEqual(store_extraction(self.notice("card-2", text)), "review")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM transactions"), [(1,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM extraction_reviews"), [(1,)])
        # A different instrument is a distinct event, not a duplicate candidate.
        self.assertEqual(
            store_extraction(self.notice("card-3", text.replace("1540", "2222"))),
            "inserted",
        )

    def test_multiple_same_reference_credits_require_review(self):
        from app.db_extraction import store_extraction

        text = "Rs.200 credited to account 3947 via UPI on 26-09-26. Refund reference number is 12345."
        self.assertEqual(store_extraction(self.notice("credit-1", text)), "inserted")
        self.assertEqual(store_extraction(self.notice("credit-2", text)), "review")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM transactions"), [(1,)])

    def test_quarantine_pipeline_and_review_page(self):
        from app.extract_transactions import extract_and_insert_transactions
        from fastapi.testclient import TestClient
        from app.main import app

        cases = [
            "UPI transaction of Rs.500 failed. Your account was not debited on 26-09-26.",
            "Rs.500 debited via UPI.",
            "USD 20 debited from credit card ending 1540 towards Test Shop on 26-09-26.",
        ]
        for i, text in enumerate(cases):
            self.notice(f"invalid-{i}", text)
        emails = self.db.get_raw_emails_by_ids([f"invalid-{i}" for i in range(3)])
        stats = extract_and_insert_transactions(emails, filter_success_only=False)
        self.assertEqual(stats["inserted"], 0)
        self.assertEqual(stats["review"], 3)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM transactions"), [(0,)])
        self.assertEqual(self.db.get_unprocessed_email_ids(), ["fixture"])
        with TestClient(app) as client:
            response = client.get("/review")
            self.assertEqual(response.status_code, 200)
            self.assertIn("Unsupported or unknown currency", response.text)
            self.assertEqual(client.get("/review?page=0").status_code, 422)

    def test_needs_review_filter_and_month_banner(self):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.services import dashboard_service as service

        self.db.insert_transactions([self.event("pending"), self.event("done")])
        self.sql(
            "UPDATE transactions SET category='Shopping', classification_source='manual' "
            "WHERE transaction_id='done'"
        )
        data = service.get_transactions_page_data(needs_review=True)
        self.assertEqual(data["summary"]["count"], 1)
        self.assertTrue(data["transactions"][0]["needsReview"])
        self.assertEqual(service.get_transactions_page_data()["summary"]["count"], 2)
        with patch.object(service, "_get_anomaly_callouts", return_value=[]):
            with TestClient(app) as client:
                page = client.get("/transactions?review=1").text
                self.assertIn('name="review" value="1"', page)
                self.assertEqual(page.count('class="txn-row"'), 1)
                rows = client.get("/htmx/filtered-transactions?review=1&q=Synthetic").text
                self.assertEqual(rows.count('class="txn-row"'), 1)
                # The stats row refreshes with the list, so counts never go stale.
                self.assertIn('id="txn-stats" hx-swap-oob="true"', rows)
                month = client.get("/month/2026/9").text
                self.assertIn("1 transaction needs", month)
                self.assertIn('href="/transactions?review=1"', month)
                landing = client.get("/", follow_redirects=False)
                self.assertEqual(landing.status_code, 302)
                self.assertTrue(landing.headers["location"].startswith("/month/"))

    def test_review_list_puts_biggest_merchant_groups_first(self):
        from app.services import dashboard_service as service

        self.db.insert_transactions(
            [
                dict(self.event("solo"), merchant="Solo Cafe", date="2026-09-20"),
                self.event("s1"),
                dict(self.event("s2"), date="2026-09-10"),
                dict(self.event("s3"), merchant="shop@okaxis Synthetic Shop", date="2026-09-05"),
                dict(self.event("in"), direction="in", merchant=None, date="2026-09-25"),
            ]
        )
        page = service.get_transactions_page_data(needs_review=True)["transactions"]
        self.assertEqual(
            [(r["merchant"], r["date"]) for r in page],
            [
                ("Synthetic Shop", "10 Sep, 2026"),
                ("Synthetic Shop", "5 Sep, 2026"),
                ("Synthetic Shop", "1 Sep, 2026"),
                ("Solo Cafe", "20 Sep, 2026"),
                ("Unknown", "25 Sep, 2026"),
            ],
        )
        self.assertEqual([r["groupStart"] for r in page], [True, False, False, False, False])
        self.assertEqual(page[0]["groupSize"], 3)
        second = service.get_transactions_page_data(needs_review=True, per_page=2, page=2)
        self.assertEqual([r["date"] for r in second["transactions"]], ["1 Sep, 2026", "20 Sep, 2026"])
        self.assertTrue(second["transactions"][0]["groupStart"])

    def test_review_retry_reparses_one_message(self):
        from app.extract_transactions import extract_and_insert_transactions
        from fastapi.testclient import TestClient
        from app.main import app

        self.notice("retry", "Rs.500 debited via UPI.")
        emails = self.db.get_raw_emails_by_ids(["retry"])
        self.assertEqual(extract_and_insert_transactions(emails)["review"], 1)
        with TestClient(app) as client:
            retry = lambda: client.post("/htmx/retry-review", data={"raw_email_id": "retry"})
            response = retry()
            self.assertEqual(response.status_code, 200)
            self.assertIn("Still needs review", response.text)
            self.assertIn('hx-swap-oob="true">1 message<', response.text)
            # A corrected body stands in for a parser fix; retry now records it.
            text = "Rs.500 debited from account 3947 via UPI on 26-09-26. Reference number is 12345."
            self.sql(
                "UPDATE raw_emails SET snippet=%s, raw_body=%s WHERE gmail_message_id='retry'",
                (text, text),
            )
            response = retry()
            self.assertIn("Recorded as a transaction", response.text)
            self.assertIn('hx-swap-oob="true">0 messages<', response.text)
            self.assertEqual(
                client.post("/htmx/retry-review", data={"raw_email_id": "missing"}).status_code,
                404,
            )
        self.assertEqual(self.sql("SELECT COUNT(*) FROM transactions"), [(1,)])

    def test_legacy_replay_keeps_id_classification_and_totals(self):
        from app.db_extraction import store_extraction

        parsed = self.notice(
            "legacy-email",
            "Rs.500 debited from account 3947 via UPI on 26-09-26. Reference number is 12345.",
        )
        self.db.insert_transactions(
            [dict(parsed, transaction_id="hdfc_upi_12345", account_last4=None)]
        )
        self.db.update_transaction_category("hdfc_upi_12345", "Shopping")
        self.assertEqual(store_extraction(parsed), "duplicate")
        self.assertEqual(
            self.sql(
                "SELECT transaction_id, category, classification_source FROM transactions"
            ),
            [("hdfc_upi_12345", "Shopping", "manual")],
        )
        # Parser changes are review candidates, not silent historical rewrites.
        self.assertEqual(store_extraction(dict(parsed, amount=600)), "review")
        self.assertEqual(
            self.sql("SELECT COUNT(*), SUM(amount) FROM transactions"), [(1, 500)]
        )

    def test_invalid_events_rejected_at_persistence_boundary(self):
        for changes in (
            {"amount": None},
            {"date": None},
            {"currency": "USD"},
            {"amount": "0.001"},
            {"direction": "unknown"},
            {"review_reasons": ["Conflicting message details"]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.db.insert_transactions([dict(self.event(), **changes)])
        with self.assertRaises(psycopg2.IntegrityError):
            self.sql(
                """INSERT INTO transactions(transaction_id,raw_email_id,status,amount,date,direction,currency)
                VALUES ('invalid','fixture','success',NULL,'2026-09-26','out','INR')"""
            )

    def test_upgrade_preserves_legacy_rows_and_enforces_new_writes(self):
        conn = self.connect()
        try:
            with conn.cursor() as cur:
                cur.execute("CREATE SCHEMA phase2_upgrade_test")
                cur.execute("SET LOCAL search_path TO phase2_upgrade_test")
                for name in (
                    "000_create_raw_emails.sql",
                    "001_create_transactions.sql",
                    "002_add_classification_fields.sql",
                ):
                    cur.execute((ROOT / "migrations" / name).read_text())
                cur.execute("INSERT INTO raw_emails(gmail_message_id) VALUES ('old')")
                cur.execute(
                    """INSERT INTO transactions(transaction_id,raw_email_id,status,category,
                    classification_source) VALUES ('old','old','success','Shopping','manual')"""
                )
                cur.execute(
                    (
                        ROOT / "migrations/003_event_validation_and_review.sql"
                    ).read_text()
                )
                cur.execute(
                    "SELECT amount, date, category, classification_source FROM transactions"
                )
                self.assertEqual(cur.fetchall(), [(None, None, "Shopping", "manual")])
                cur.execute("SAVEPOINT invalid_insert")
                with self.assertRaises(psycopg2.IntegrityError):
                    cur.execute(
                        """INSERT INTO transactions(transaction_id,raw_email_id,status)
                        VALUES ('new','old','success')"""
                    )
                cur.execute("ROLLBACK TO SAVEPOINT invalid_insert")
        finally:
            # Roll back the entire isolated upgrade fixture, including its schema.
            conn.rollback()
            conn.close()

    def test_extraction_failure_rolls_back_notification_and_event(self):
        import app.db_extraction as extraction

        event = self.notice("rollback", "Rs.500 debited via UPI on 26-09-26.")
        original = extraction.insert_transactions

        def failing_insert(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("injected failure before notification mapping")

        with patch.object(
            extraction, "insert_transactions", side_effect=failing_insert
        ):
            with self.assertRaises(RuntimeError):
                extraction.store_extraction(event)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM transactions"), [(0,)])
        self.assertEqual(
            self.sql("SELECT COUNT(*) FROM transaction_notifications"), [(0,)]
        )
        self.assertIn("rollback", self.db.get_unprocessed_email_ids())


if __name__ == "__main__":
    unittest.main()
