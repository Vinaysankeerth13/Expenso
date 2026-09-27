"""Synthetic bank-template and adversarial event parsing regressions."""

from decimal import Decimal
from datetime import datetime
import unittest

from app.transaction_event import (
    extract_transaction,
    generate_transaction_id,
    parse_date,
)


class ParserTests(unittest.TestCase):
    def parse(self, snippet, **fields):
        return extract_transaction(dict(id="notice", snippet=snippet, **fields))

    def test_separate_credit_details_date(self):
        text = (
            "Rs.600.00 has been successfully credited to your HDFC Bank account ending in 1234. "
            "Transaction Details: a. Date: 07-05-26 b. Sender: Example (VPA: shop@bank) "
            "c. UPI Reference No.: 123456789012"
        )
        event = self.parse(text)
        self.assertEqual(event["review_reasons"], [])
        self.assertEqual(event["date"], "2026-05-07")
        self.assertEqual(event["direction"], "in")

    def test_card_boilerplate_and_may_month(self):
        text = (
            "We would like to inform you that Rs.20.00 has been debited from your "
            "HDFC Bank Credit Card ending 1234 towards Test Shop on 17 May, 2026 at 09:44:05. "
            "To check your available balance, you may use Mycards."
        )
        self.assertEqual(self.parse(text)["review_reasons"], [])
        for uncertainty in (
            "This movement is pending confirmation.",
            "This movement may be reversed.",
        ):
            self.assertTrue(self.parse(text + uncertainty)["review_reasons"])

    def test_truncated_reference_is_not_a_reference(self):
        snippet = (
            "Rs.20 debited from account 1234 via UPI on 07-05-26. Reference number is"
        )
        self.assertIsNone(self.parse(snippet)["reference_id"])
        event = self.parse(snippet, raw_body=snippet + " 123456789012.")
        self.assertEqual(event["review_reasons"], [])
        self.assertEqual(event["reference_id"], "123456789012")

    def test_completed_card_transaction_template(self):
        text = (
            "Thank you for using your HDFC Bank Credit Card ending in 1234. "
            "You made a transaction of Rs. 316.00 at Test Shop on 22-06-2026 20:26:00. "
            "Authorization code: 123456"
        )
        event = self.parse(text)
        self.assertEqual(event["review_reasons"], [])
        self.assertEqual(event["amount"], Decimal("316"))
        self.assertEqual(event["merchant"], "Test Shop")
        self.assertEqual(event["date"], "2026-06-22")
        self.assertEqual(event["direction"], "out")
        for variant in (
            text + " Payment declined.",
            text + " Pending confirmation.",
            text.replace("made", "will make"),
            text.replace("Credit Card", "account"),
            text + " Rs.316 refunded.",
        ):
            self.assertTrue(self.parse(variant)["review_reasons"])

    def test_known_non_movements_and_conflicting_body(self):
        for text in (
            "Your OTP is 000000 for a purchase of Rs.100.",
            "Your card transaction was declined.",
            "Your Login PIN has been successfully set.",
        ):
            self.assertTrue(self.parse(text).get("exclusion_reason"))
            event = self.parse("Rs.100 debited via UPI on 07-05-26.", raw_body=text)
            self.assertTrue(event["review_reasons"])
            self.assertNotIn("exclusion_reason", event)

    def test_known_upi_and_card_templates(self):
        templates = [
            "Rs.20.00 has been debited from account 3947 to VPA shop@bank Test Shop on 28-11-25. Your UPI transaction reference number is 114833820377. If you",
            "INR 20.00 debited from account 3947 towards VPA shop@bank (Test Shop) on 28-11-2025. UPI transaction reference no.: 114833820377",
            "Rs.20.00 is debited from your HDFC Bank Credit Card ending 1540 towards Test Shop on 28 Nov, 2025 at 20:19:42.",
            "Rs.20.00 is debited from your HDFC Bank Debit Card ending 1540 towards Test Shop on 28 Nov, 2025.",
        ]
        for text in templates:
            with self.subTest(text=text):
                event = self.parse(text)
                self.assertEqual(event["review_reasons"], [])
                self.assertEqual(event["amount"], Decimal("20"))
                self.assertEqual(event["date"], "2025-11-28")
                self.assertIn("Test Shop", event["merchant"])
                self.assertIn(event["account_last4"], ("3947", "1540"))

    def test_failed_negated_and_future_movements_are_not_success(self):
        for text in (
            "Your UPI transaction of Rs.500 failed. Your account was not debited on 26-09-26.",
            "Transaction of Rs.500 failed. The amount will be credited on 26-09-26.",
            "Rs.500 will be credited via UPI on 26-09-26.",
            "Rs.500 has not been debited via UPI on 26-09-26.",
            "Rs.500 debited via UPI on 26-09-26 is pending confirmation.",
        ):
            with self.subTest(text=text):
                event = self.parse(text)
                self.assertNotEqual(event["status"], "success")
                self.assertTrue(event["review_reasons"])

    def test_amount_belongs_to_movement_not_balance(self):
        event = self.parse(
            "Available balance Rs.9000.00. ₹500.00 debited via UPI towards VPA shop@bank (Test Shop) on 26-09-2026."
        )
        self.assertEqual(event["amount"], Decimal("500.00"))
        self.assertEqual(event["date"], "2026-09-26")
        self.assertEqual(event["review_reasons"], [])

    def test_four_digit_dates_and_boundaries(self):
        self.assertEqual(parse_date("26-09-2026"), "2026-09-26")
        self.assertEqual(parse_date("26-09-26"), "2026-09-26")
        for token in (
            "26-09-20260",
            "126-09-26",
            "31-02-2026",
            "26-09-2026 and 27-09-2026",
        ):
            self.assertIsNone(parse_date(token))

    def test_dates_must_belong_to_movement_and_precede_notification(self):
        event = self.parse(
            "Rs.500 debited via UPI. Available balance updated on 26-09-26."
        )
        self.assertIsNone(event["date"])
        event = self.parse(
            "Rs.500 debited via UPI on 26-09-26.", received_at=datetime(2025, 9, 26)
        )
        self.assertIn(
            "Transaction date is after the notification date", event["review_reasons"]
        )

    def test_incomplete_foreign_and_invalid_amounts_need_review(self):
        cases = (
            "Rs.500 debited via UPI to VPA shop@bank Test Shop.",
            "USD 20.00 debited from your credit card towards Test Shop on 26 Sep, 2026.",
            "Rs.0 debited via UPI on 26-09-26.",
            "Rs.-500 debited via UPI on 26-09-26.",
            "Rs.500.001 debited via UPI on 26-09-26.",
            "Rs.50,0 debited via UPI on 26-09-26.",
            "Rs.500 debited via UPI on 26-09-2099.",
        )
        for text in cases:
            with self.subTest(text=text):
                self.assertTrue(self.parse(text)["review_reasons"])
        foreign = self.parse(cases[1])
        self.assertEqual(foreign["currency"], "USD")
        self.assertEqual(foreign["amount"], Decimal("20"))

    def test_plain_and_html_body_fallback(self):
        for body in (
            "₹500 debited via UPI to VPA shop@bank Test Shop on 26-09-2026.",
            "<p>₹500 <b>debited</b> via UPI to VPA shop@bank Test Shop on 26-09-2026.</p><script>Rs.900 credited</script>",
        ):
            event = self.parse(
                "HDFC notification. Read your message for details.", raw_body=body
            )
            self.assertEqual(event["text_source"], "raw_body")
            self.assertEqual(event["review_reasons"], [])

    def test_conflicting_body_is_not_silently_overridden(self):
        snippet = "Rs.500 debited via UPI on 26-09-26."
        for body in (
            "Rs.600 debited via UPI on 26-09-26.",
            "Transaction failed. Rs.500 was not debited via UPI on 26-09-26.",
            "Rs.500 debited and Rs.500 credited via UPI on 26-09-26.",
        ):
            self.assertTrue(self.parse(snippet, raw_body=body)["review_reasons"])

    def test_reversal_has_distinct_identity(self):
        debit = self.parse(
            "Rs.500 debited from account 3947 via UPI on 26-09-26. Reference number is 12345."
        )
        credit = self.parse(
            "Rs.500 credited to account 3947 via UPI on 26-09-26. Reversal reference number is 12345."
        )
        self.assertEqual(credit["event_kind"], "reversal")
        self.assertEqual(credit["review_reasons"], [])
        self.assertNotEqual(
            generate_transaction_id(debit), generate_transaction_id(credit)
        )
        self.assertEqual(
            generate_transaction_id(debit),
            generate_transaction_id(dict(debit, raw_email_id="another")),
        )
        self.assertNotEqual(
            generate_transaction_id(debit),
            generate_transaction_id(dict(debit, account_last4="1111")),
        )


if __name__ == "__main__":
    unittest.main()
