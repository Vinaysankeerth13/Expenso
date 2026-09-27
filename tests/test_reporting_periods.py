"""Matched periods must not turn missing history into zero spending."""

from datetime import date, datetime, timezone
from decimal import Decimal
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo
from app.reporting_periods import compare, windows, covered
from app.anomaly_detector import detect_anomalies, print_report


def evidence(rows=(), start=date(2026, 1, 1), end=date(2026, 9, 10), unresolved=()):
    return {
        "rows": list(rows),
        "coverage": {"coverage_start": start, "covered_through": end},
        "unresolved": list(unresolved),
    }


def row(day, amount, category="Shopping"):
    return {
        "date": day,
        "amount": Decimal(amount),
        "direction": "out",
        "category": category,
    }


class ReportingPeriodTests(unittest.TestCase):
    def test_equal_elapsed_days_not_full_previous_month(self):
        data = evidence(
            [
                row(date(2026, 9, 10), "2000"),
                row(date(2026, 8, 10), "1000"),
                row(date(2026, 8, 31), "2100"),
            ]
        )
        result = compare(2026, 9, data, today=date(2026, 9, 11))
        self.assertEqual(result["pct_change"], 100)
        self.assertEqual(result["prev_total"], 1000)

    def test_stale_checkpoint_and_short_month_clamp(self):
        periods = windows(
            2026, 3, {"covered_through": date(2026, 3, 30)}, today=date(2026, 3, 31)
        )
        self.assertEqual([end.day for _, end in periods], [28, 28])
        periods = windows(
            2024, 3, {"covered_through": date(2024, 3, 30)}, today=date(2024, 3, 31)
        )
        self.assertEqual([end.day for _, end in periods], [29, 29])
        periods = windows(
            2026, 9, {"covered_through": date(2026, 9, 5)}, today=date(2026, 9, 27)
        )
        self.assertEqual([end.day for _, end in periods], [5, 5])

    def test_completed_months_and_year_rollover(self):
        data = evidence(
            [row(date(2025, 12, 31), "100"), row(date(2026, 1, 31), "150")],
            start=date(2025, 12, 1),
        )
        result = compare(2026, 1, data, today=date(2026, 9, 27))
        self.assertEqual(result["pct_change"], 50)
        self.assertEqual(result["periods"][1], (date(2025, 12, 1), date(2025, 12, 31)))

    def test_unknown_history_or_unresolved_messages_suppress_percentage(self):
        data = evidence([row(date(2026, 9, 10), "900")], start=date(2026, 9, 1))
        self.assertIsNone(compare(2026, 9, data, today=date(2026, 9, 11))["pct_change"])
        data["coverage"] = None
        self.assertEqual(
            compare(2026, 9, data, today=date(2026, 9, 11))["status"], "incomplete"
        )
        data = evidence(unresolved=[None])
        self.assertFalse(covered(data, date(2026, 8, 1), date(2026, 8, 10)))

    def test_unresolved_message_only_blocks_nearby_periods(self):
        ist = ZoneInfo("Asia/Kolkata")
        data = evidence(
            unresolved=[datetime(2026, 9, 6, 21, 5, tzinfo=ist)], end=date(2026, 9, 26)
        )
        self.assertTrue(covered(data, date(2026, 3, 1), date(2026, 3, 31)))
        self.assertTrue(covered(data, date(2026, 8, 1), date(2026, 8, 29)))
        self.assertFalse(covered(data, date(2026, 8, 1), date(2026, 8, 30)))
        self.assertFalse(covered(data, date(2026, 9, 7), date(2026, 9, 26)))
        self.assertTrue(covered(data, date(2026, 9, 8), date(2026, 9, 26)))
        # 20:00 UTC on 6 Sep is 7 Sep in India, so the window shifts a day.
        data = evidence(
            unresolved=[datetime(2026, 9, 6, 20, 0, tzinfo=timezone.utc)],
            end=date(2026, 9, 26),
        )
        self.assertTrue(covered(data, date(2026, 8, 1), date(2026, 8, 30)))
        self.assertFalse(covered(data, date(2026, 9, 8), date(2026, 9, 26)))

    def test_zero_baseline_has_no_invented_percentage(self):
        data = evidence([row(date(2026, 9, 10), "900")])
        result = compare(2026, 9, data, today=date(2026, 9, 11))
        self.assertEqual(result["status"], "new")
        self.assertIsNone(result["pct_change"])
        self.assertEqual(
            compare(2026, 9, evidence(), today=date(2026, 9, 11))["status"], "zero"
        )

    def test_future_and_first_day_have_no_completed_comparison(self):
        for today in (date(2026, 8, 31), date(2026, 9, 1)):
            self.assertEqual(compare(2026, 9, evidence(), today=today)["periods"], [])

    def detect(self, data):
        with (
            patch("app.reporting_periods.read_evidence", return_value=data),
            patch("app.db_pipeline.pipeline_today", return_value=date(2026, 9, 11)),
        ):
            return detect_anomalies()

    def test_anomaly_compares_three_matched_windows(self):
        data = evidence(
            [row(date(2026, m, 10), "1000") for m in (6, 7, 8)]
            + [row(date(2026, 9, 10), "2000"), row(date(2026, 8, 31), "5000")]
        )
        result = self.detect(data)
        self.assertEqual(result["anomalies"][0]["velocity_pct"], 100)
        self.assertEqual(result["anomalies"][0]["avg_amount"], 1000)

    def test_missing_months_not_zero_and_new_category_not_100_percent(self):
        data = evidence(
            [row(date(2026, 8, 10), "900"), row(date(2026, 9, 10), "900")],
            start=date(2026, 8, 1),
        )
        self.assertEqual(self.detect(data)["status"], "insufficient_history")
        result = self.detect(evidence([row(date(2026, 9, 10), "900")]))
        self.assertEqual(result["anomalies"], [])
        self.assertIsNone(result["categories"][0]["velocity_pct"])
        with patch("builtins.print"):
            print_report(result)
            print_report(self.detect(data))


from postgres_support import TemporaryPostgres
from app.reporting_periods import read_evidence, calendar_values, month_comparison
from app.db_pipeline import ingestion_scope
from app.ingest import HDFC_SENDERS, get_keywords
from app import db_dashboard
from app.services.dashboard_service import get_year_page_data, get_month_page_data


class ReportingStorageTests(TemporaryPostgres):
    def checkpoint(self, start, end):
        scope = ingestion_scope(HDFC_SENDERS, get_keywords())
        ident = self.sql(
            "INSERT INTO pipeline_runs(stage,scope,status) VALUES ('ingestion',%s,'success') RETURNING id",
            (scope,),
        )[0][0]
        self.sql(
            "INSERT INTO ingestion_checkpoints(scope,coverage_start,covered_through,run_id) VALUES (%s,%s,%s,%s)",
            (scope, start, end, ident),
        )

    def test_empty_covered_calendar_differs_from_unknown(self):
        self.sql("DELETE FROM raw_emails")
        data = read_evidence(date(2026, 8, 1), date(2026, 9, 26))
        self.assertEqual(calendar_values(data, 2026, 8), [None] * 31)
        self.checkpoint(date(2026, 8, 1), date(2026, 9, 26))
        data = read_evidence(date(2026, 8, 1), date(2026, 9, 26))
        self.assertEqual(calendar_values(data, 2026, 8), [0] * 31)
        self.assertEqual(calendar_values(data, 2026, 9)[26:], [None] * 4)

    def test_database_comparison_uses_coverage_and_unresolved_outcomes(self):
        self.db.insert_transactions([self.event("a")])
        self.sql("UPDATE transactions SET date='2026-08-10' WHERE transaction_id='a'")
        self.db.insert_transactions(
            [dict(self.event("b"), date="2026-09-10", amount="200")]
        )
        self.checkpoint(date(2026, 8, 1), date(2026, 9, 10))
        with patch(
            "app.reporting_periods.pipeline_today", return_value=date(2026, 9, 11)
        ):
            result = month_comparison(2026, 9)
        self.assertEqual(result["pct_change"], 100)
        self.sql(
            "INSERT INTO email_processing_outcomes(raw_email_id,parser_version,outcome) VALUES ('fixture','test','review')"
        )
        with patch(
            "app.reporting_periods.pipeline_today", return_value=date(2026, 9, 11)
        ):
            self.assertIsNone(month_comparison(2026, 9)["pct_change"])

    def test_dashboard_labels_and_complete_axes(self):
        from fastapi.testclient import TestClient
        from app.main import app

        self.db.insert_transactions([self.event()])
        with patch(
            "app.services.dashboard_service._get_anomaly_callouts", return_value=[]
        ):
            year = get_year_page_data(2026)
            month = get_month_page_data(2026, 9)
            self.assertEqual(len(year["monthlyTrend"]["labels"]), 12)
            self.assertEqual(len(month["dailyTrend"]["labels"]), 30)
            self.assertIsNone(month["summary"]["change"])
            self.assertEqual(month["dailyTrend"]["amounts"][0], 100)
            self.assertIsNone(month["dailyTrend"]["amounts"][1])
            with TestClient(app) as client:
                y = client.get("/year/2026")
                m = client.get("/month/2026/9")
            self.assertEqual(y.status_code, 200)
            self.assertEqual(m.status_code, 200)
            self.assertIn("Net captured cash flow", y.text)
            self.assertIn("No comparable percentage", m.text)
            self.assertNotIn("over budget", m.text)

    def test_reconciled_sum_invariants_still_hold(self):
        self.db.insert_transactions(
            [
                dict(self.event("a"), amount="0.10"),
                dict(self.event("b"), amount="0.20"),
                dict(self.event("c"), amount="100"),
            ]
        )
        summary = db_dashboard.get_year_summary(2026)
        monthly = db_dashboard.get_monthly_totals(2026)
        categories = db_dashboard.get_category_totals_for_year(2026)
        daily = db_dashboard.get_daily_totals_for_month(2026, 9)
        self.assertEqual(summary["total_spent"], Decimal("100.30"))
        self.assertEqual(sum(r["total_out"] for r in monthly), summary["total_spent"])
        self.assertEqual(sum(r["total"] for r in categories), summary["total_spent"])
        self.assertEqual(sum(r["total"] for r in daily), summary["total_spent"])
