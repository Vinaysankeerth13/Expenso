"""Classification decisions, manual exceptions, rule scope and undo regressions."""

import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app import classify_transactions as engine
from app import db_dashboard
from app.db_classification import edit_category, undo_category
from app.db_merchant_categories import (
    lookup_merchant_category,
    insert_merchant_category,
)
from app.services.dashboard_service import get_transactions_page_data
from postgres_support import TemporaryPostgres


class ClassificationDecisionTests(unittest.TestCase):
    def test_names_do_not_prove_transfers(self):
        with (
            patch.object(engine, "lookup_merchant_category", return_value=None),
            patch.object(engine, "classify_with_llm", return_value=None),
        ):
            for merchant in ("CORNER CAFE", "CITY HOSPITAL", "SOME PERSON"):
                decision = engine.classify_transaction(
                    {"merchant": merchant, "source": "hdfc_upi"}
                )
                self.assertEqual(decision["category"], "Other")
                self.assertEqual(decision["confidence"], 0)

    def test_incoming_is_not_automatically_income(self):
        with patch.object(engine, "classify_with_llm") as llm:
            result = engine.classify_transaction(
                {"direction": "in", "merchant": "Store refund"}
            )
        self.assertEqual(result["category"], "Other")
        self.assertLess(result["confidence"], 0.9)
        llm.assert_not_called()

    def test_model_rule_retains_confidence_and_origin(self):
        with patch.object(
            engine,
            "lookup_merchant_category",
            return_value={
                "merchant_pattern": "store",
                "category": "Shopping",
                "confidence": 0.72,
                "source": "llm",
            },
        ):
            result = engine.classify_transaction({"merchant": "Store"})
        self.assertEqual((result["source"], result["confidence"]), ("llm", 0.72))

    def test_model_transfer_guess_is_reviewable_unknown(self):
        with (
            patch.object(engine, "lookup_merchant_category", return_value=None),
            patch.object(
                engine,
                "classify_with_llm",
                return_value={"category": "Transfers", "confidence": 0.99},
            ),
        ):
            result = engine.classify_transaction({"merchant": "A Person"})
        self.assertEqual(result["category"], "Other")
        self.assertLess(result["confidence"], 0.9)

    def test_model_schema_is_validated(self):
        invalid = [
            [],
            None,
            {"category": "Shopping", "confidence": True},
            {"category": "Shopping", "confidence": float("nan")},
            {"category": "Shopping", "confidence": 1.1},
            {"category": "Shopping", "confidence": -1},
            {"category": "Shopping", "confidence": "0.9"},
            {"category": "Shopping", "confidence": 0.9, "rationale": []},
            {"category": "Invalid", "confidence": 0.9},
        ]
        for value in invalid + [
            {"category": "Shopping", "confidence": 0.8, "rationale": "Synthetic"}
        ]:
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"ANTHROPIC_API_KEY": "synthetic"}),
                patch("anthropic.Anthropic") as factory,
            ):
                factory.return_value.__enter__.return_value.messages.create.return_value = SimpleNamespace(
                    content=[SimpleNamespace(text=json.dumps(value))]
                )
                result = engine.classify_with_llm("Store", "store", 100, "hdfc_upi")
                if value in invalid:
                    self.assertIsNone(result)
                else:
                    self.assertEqual(result["confidence"], 0.8)
                factory.return_value.__exit__.assert_called_once()


class ClassificationStorageTests(TemporaryPostgres):
    def setUp(self):
        super().setUp()
        self.sql("TRUNCATE merchant_categories RESTART IDENTITY")

    def row_id(self, **fields):
        self.db.insert_transactions([self.event(**fields)])
        return self.sql("SELECT id FROM transactions")[0][0]

    def test_literal_percent_underscore_and_longest_pattern(self):
        insert_merchant_category("audit%shop", "Shopping")
        insert_merchant_category("audit_shop", "Groceries")
        self.assertIsNone(lookup_merchant_category("audit grocery shop"))
        self.assertIsNone(lookup_merchant_category("auditXshop"))
        self.assertEqual(
            lookup_merchant_category("the audit%shop location")["category"], "Shopping"
        )
        insert_merchant_category("swiggy", "Food & Dining")
        insert_merchant_category("instamart", "Groceries")
        self.assertEqual(
            lookup_merchant_category("swiggy instamart")["category"], "Groceries"
        )

    def test_model_rules_exact_and_cannot_replace_manual_rules(self):
        insert_merchant_category("store", "Shopping", confidence=0.91, source="llm")
        self.assertIsNone(lookup_merchant_category("store pharmacy"))
        self.assertEqual(lookup_merchant_category("store")["confidence"], 0.91)
        insert_merchant_category("store", "Groceries", source="manual")
        self.assertFalse(insert_merchant_category("store", "Shopping", source="llm"))
        self.assertEqual(lookup_merchant_category("store")["category"], "Groceries")

    def test_dry_run_has_no_transaction_rule_or_edit_writes(self):
        self.row_id()
        before = self.sql(
            "SELECT category, classification_source, classification_confidence, updated_at FROM transactions"
        )
        with patch.object(
            engine,
            "classify_with_llm",
            return_value={
                "category": "Shopping",
                "confidence": 0.99,
                "rationale": "Synthetic",
            },
        ):
            engine.batch_classify(dry_run=True)
        self.assertEqual(
            self.sql(
                "SELECT category, classification_source, classification_confidence, updated_at FROM transactions"
            ),
            before,
        )
        self.assertEqual(self.sql("SELECT COUNT(*) FROM merchant_categories"), [(0,)])
        self.assertEqual(self.sql("SELECT COUNT(*) FROM classification_edits"), [(0,)])

    def test_normal_model_decision_does_not_learn_rule(self):
        self.row_id()
        with patch.object(
            engine,
            "classify_with_llm",
            return_value={
                "category": "Shopping",
                "confidence": 0.99,
                "rationale": "Synthetic",
            },
        ):
            result = engine.batch_classify()
        self.assertEqual(result["llm_classified"], 1)
        self.assertEqual(self.sql("SELECT COUNT(*) FROM merchant_categories"), [(0,)])

    def test_manual_survives_reclassify_all_and_concurrent_automatic_update(self):
        self.row_id(
            category="Shopping",
            classification_source="manual",
            classification_confidence=1,
            notes="Keep",
        )
        with patch.object(engine, "classify_transaction") as classify:
            result = engine.batch_classify(reclassify_all=True)
        classify.assert_not_called()
        self.assertEqual(result["skipped"], 1)
        self.assertFalse(
            self.db.update_transaction_category(
                "event", "Transfers", 0.85, "rule", "Overwritten"
            )
        )
        self.assertEqual(
            self.sql("SELECT category, notes FROM transactions"), [("Shopping", "Keep")]
        )

    def test_review_badge_and_count_agree_for_all_sources(self):
        for i, (source, confidence, category) in enumerate(
            [
                ("rule", 0, "Other"),
                ("rule", 0.85, "Transfers"),
                ("llm", 0.5, "Shopping"),
                ("rule", None, "Other"),
                (None, None, None),
                ("manual", 0.2, "Shopping"),
                ("rule", 1, "Shopping"),
            ]
        ):
            self.db.insert_transactions(
                [
                    self.event(
                        str(i),
                        category=category,
                        classification_source=source,
                        classification_confidence=confidence,
                    )
                ]
            )
        page = get_transactions_page_data()
        self.assertEqual(sum(r["needsReview"] for r in page["transactions"]), 5)
        self.assertEqual(db_dashboard.get_low_confidence_count(), 5)

    def test_default_edit_is_transaction_only_and_undo_restores_classification(self):
        ref = self.row_id(
            category="Other",
            classification_source="llm",
            classification_confidence=0.5,
            notes="Original",
        )
        before = self.sql(
            "SELECT category, classification_source, classification_confidence, notes, amount FROM transactions"
        )
        row = edit_category(ref, "Shopping")
        self.assertEqual(self.sql("SELECT COUNT(*) FROM merchant_categories"), [(0,)])
        undo_category(row["edit_id"])
        self.assertEqual(
            self.sql(
                "SELECT category, classification_source, classification_confidence, notes, amount FROM transactions"
            ),
            before,
        )
        with self.assertRaises(ValueError):
            undo_category(row["edit_id"])

    def test_opt_in_rule_is_exact_and_undo_restores_previous_rule(self):
        ref = self.row_id()
        insert_merchant_category(
            "synthetic shop", "Other", confidence=0.8, source="llm"
        )
        old = lookup_merchant_category("synthetic shop")
        row = edit_category(ref, "Shopping", learn_merchant=True)
        rule = lookup_merchant_category("synthetic shop")
        self.assertEqual(
            (rule["source"], rule["match_mode"], rule["category"]),
            ("manual", "exact", "Shopping"),
        )
        self.assertIsNone(lookup_merchant_category("synthetic shop pharmacy"))
        undo_category(row["edit_id"])
        rule = lookup_merchant_category("synthetic shop")
        for key in ("category", "confidence", "source", "match_mode"):
            self.assertEqual(rule[key], old[key])

    def test_undo_does_not_overwrite_later_edit_or_rule(self):
        ref = self.row_id()
        first = edit_category(ref, "Shopping", learn_merchant=True)
        second = edit_category(ref, "Groceries")
        with self.assertRaises(ValueError):
            undo_category(first["edit_id"])
        undo_category(second["edit_id"])
        insert_merchant_category("synthetic shop", "Subscriptions")
        with self.assertRaises(ValueError):
            undo_category(first["edit_id"])
        self.assertEqual(self.sql("SELECT category FROM transactions"), [("Shopping",)])

    def test_new_rule_undo_and_invalid_rule_request_are_atomic(self):
        ref = self.row_id()
        row = edit_category(ref, "Shopping", learn_merchant=True)
        undo_category(row["edit_id"])
        self.assertIsNone(lookup_merchant_category("synthetic shop"))
        self.sql("UPDATE transactions SET merchant=NULL")
        with self.assertRaises(ValueError):
            edit_category(ref, "Shopping", learn_merchant=True)
        self.assertEqual(self.sql("SELECT category FROM transactions"), [(None,)])

    def test_all_payments_option_updates_automated_rows_and_undoes_as_batch(self):
        auto = dict(category="Other", classification_source="llm", classification_confidence=0.5)
        self.db.insert_transactions(
            [
                self.event("a", **auto),
                self.event("b", **auto),
                dict(self.event("vpa", **auto), merchant="shop@okaxis Synthetic Shop"),
                dict(self.event("elsewhere", **auto), merchant="Another Shop"),
                self.event(
                    "mine",
                    category="Groceries",
                    classification_source="manual",
                    classification_confidence=1,
                ),
                dict(self.event("incoming", **auto), direction="in"),
            ]
        )
        ids = dict(self.sql("SELECT transaction_id, id FROM transactions"))
        state = lambda: dict(
            self.sql("SELECT transaction_id, category || '/' || classification_source FROM transactions")
        )
        page = get_transactions_page_data()["transactions"]
        self.assertEqual({r["ref"]: r["similarCount"] for r in page}[ids["a"]], 2)

        row = edit_category(ids["a"], "Shopping", learn_merchant=True)
        self.assertEqual(row["batch_count"], 2)
        self.assertEqual(
            state(),
            {
                "a": "Shopping/manual",
                "b": "Shopping/manual",
                "vpa": "Shopping/manual",
                "elsewhere": "Other/llm",
                "mine": "Groceries/manual",
                "incoming": "Other/llm",
            },
        )
        # A later choice on one batch row survives undoing the batch.
        edit_category(ids["b"], "Groceries")
        undone = undo_category(row["edit_id"])
        self.assertEqual(undone["batch_reverted"], 1)
        self.assertEqual(
            state(),
            {
                "a": "Other/llm",
                "b": "Groceries/manual",
                "vpa": "Other/llm",
                "elsewhere": "Other/llm",
                "mine": "Groceries/manual",
                "incoming": "Other/llm",
            },
        )
        self.assertIsNone(lookup_merchant_category("synthetic shop"))

    def test_all_payments_route_refreshes_list_and_notice_undo(self):
        from app.main import app

        auto = dict(category="Other", classification_source="llm", classification_confidence=0.5)
        self.db.insert_transactions([self.event("a", **auto), self.event("b", **auto)])
        ref = dict(self.sql("SELECT transaction_id, id FROM transactions"))["a"]
        with TestClient(app) as client:
            response = client.post(
                "/htmx/update-category",
                data={"ref": ref, "category": "Shopping", "learn_merchant": "true"},
            )
            self.assertEqual(response.headers["HX-Trigger-After-Settle"], "transactions-changed")
            self.assertIn("2 payments to Synthetic Shop", response.text)
            edit_id = self.sql("SELECT MIN(id) FROM classification_edits")[0][0]
            response = client.post(
                "/htmx/undo-category", data={"edit_id": edit_id, "from_notice": "true"}
            )
            self.assertIn("Undone. 2 payments restored", response.text)
            self.assertEqual(response.headers["HX-Trigger-After-Settle"], "transactions-changed")
        self.assertEqual(self.sql("SELECT DISTINCT category FROM transactions"), [("Other",)])

    def test_category_route_default_opt_in_and_undo(self):
        from app.main import app

        ref = self.row_id()
        with TestClient(app) as client:
            response = client.post(
                "/htmx/update-category", data={"ref": ref, "category": "Shopping"}
            )
            self.assertEqual(response.status_code, 200)
            self.assertIn("Undo", response.text)
            self.assertIn("Use for all payments to this merchant", response.text)
            self.assertEqual(
                self.sql("SELECT COUNT(*) FROM merchant_categories"), [(0,)]
            )
            edit_id = self.sql("SELECT MAX(id) FROM classification_edits")[0][0]
            response = client.post("/htmx/undo-category", data={"edit_id": edit_id})
            self.assertEqual(response.status_code, 200)
            self.assertIn("Needs review", response.text)
            self.assertEqual(
                client.post(
                    "/htmx/undo-category", data={"edit_id": edit_id}
                ).status_code,
                409,
            )
            self.assertEqual(
                client.post(
                    "/htmx/update-category",
                    data={"ref": ref, "category": "Shopping", "learn_merchant": "true"},
                ).status_code,
                200,
            )
            self.assertEqual(
                self.sql("SELECT match_mode FROM merchant_categories"), [("exact",)]
            )
