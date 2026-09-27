from datetime import datetime, timezone
from decimal import Decimal
import unittest

from app.historical_repair import build_report


def raw(ident, text):
    return {
        "gmail_message_id": ident,
        "snippet": text,
        "received_at": datetime(2026, 9, 26, tzinfo=timezone.utc),
    }


def row(ident, email, **changes):
    return {
        **{
            "transaction_id": ident,
            "raw_email_id": email,
            "amount": Decimal("500"),
            "date": "2026-09-26",
            "currency": "INR",
            "direction": "out",
            "status": "success",
            "source": "hdfc_upi",
            "merchant": None,
            "reference_id": None,
            "category": "Shopping",
            "classification_source": "manual",
            "classification_confidence": 1,
            "notes": "Keep this correction",
        },
        **changes,
    }


class HistoricalRepairTests(unittest.TestCase):
    def test_wrong_amount_and_year_are_proposed_with_classification_preserved(self):
        old = row("legacy", "email", amount=Decimal("9000"), date="2020-09-26")
        report = build_report(
            [
                raw(
                    "email",
                    "Available balance Rs.9000. Rs.500 debited via UPI on 26-09-2026.",
                )
            ],
            [old],
        )
        entry = report["entries"][0]
        self.assertEqual(entry["action"], "propose_update")
        self.assertEqual(
            entry["preserved_classification"]["notes"], "Keep this correction"
        )
        self.assertEqual(report["delta"]["INR/all"]["out"], Decimal("-8500"))
        self.assertEqual(old["amount"], Decimal("9000"))

    def test_uncertain_existing_records_are_held_not_deleted(self):
        report = build_report(
            [raw("email", "UPI failed. Rs.500 will be credited on 26-09-26.")],
            [row("legacy", "email")],
        )
        self.assertEqual(report["entries"][0]["action"], "hold_existing")
        self.assertEqual(report["delta"]["INR/all"]["out"], 0)

    def test_new_reversal_retained_and_linked_without_double_counting_notice(self):
        emails = [
            raw(
                "debit",
                "Rs.500 debited from account 3947 via UPI on 26-09-26. Reference number is 12345.",
            ),
            raw(
                "credit",
                "Rs.500 credited to account 3947 via UPI on 26-09-26. Reversal reference number is 12345.",
            ),
            raw(
                "copy",
                "Rs.500 debited from account 3947 via UPI on 26-09-26. Reference number is 12345.",
            ),
        ]
        report = build_report(emails, [row("old", "debit", reference_id="12345")])
        self.assertEqual(report["actions"]["propose_insert"], 1)
        self.assertEqual(report["actions"]["duplicate_notice"], 1)
        self.assertEqual(report["staged"]["INR/all"]["net"], 0)
        self.assertEqual(len(report["proposed_links"]), 1)

    def test_identical_unreferenced_card_notices_require_review(self):
        text = (
            "Rs.500 debited from credit card ending 1540 towards Test Shop on 26-09-26."
        )
        report = build_report([raw("one", text), raw("two", text)], [])
        self.assertEqual(report["actions"]["propose_insert"], 1)
        self.assertEqual(report["actions"]["review_unrecorded"], 1)

    def test_currencies_and_missing_amounts_are_not_combined(self):
        report = build_report(
            [],
            [
                row("inr", "absent"),
                row("usd", "absent", currency="USD"),
                row("unknown", "absent", amount=None),
            ],
        )
        self.assertEqual(report["before"]["INR/all"]["out"], 500)
        self.assertEqual(report["before"]["USD/all"]["out"], 500)
        self.assertEqual(report["before"]["INR/all"]["missing_amount"], 1)
        self.assertEqual(len(report["transactions_without_raw_evidence"]), 3)
