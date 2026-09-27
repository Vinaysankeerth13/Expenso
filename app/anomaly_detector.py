"""
Spend Velocity Anomaly Detector

Analyses per-category spending over a rolling window, computes velocity
(% change vs. average), and flags anomalies where spend is significantly
above the historical average.

Usage:
    python -m app.anomaly_detector [--months 3] [--threshold 50] [--min-deviation 500] [--json]
"""

import argparse
import json
import logging
from collections import defaultdict
from datetime import date
from typing import Dict, Any

from app import db_anomaly
from app.services.formatting import format_currency

log = logging.getLogger(__name__)


def detect_anomalies(
    lookback_months: int = 3,
    threshold_pct: float = 50.0,
    min_abs_deviation: float = 500.0,
) -> Dict[str, Any]:
    from app.reporting_periods import (
        read_evidence,
        windows,
        covered,
        total,
        month_start,
    )
    from app.db_pipeline import pipeline_today

    if not 1 <= lookback_months <= 24:
        raise ValueError("Lookback must be between 1 and 24 months")
    today = pipeline_today()
    evidence = read_evidence(
        month_start(today.year, today.month, -lookback_months), today
    )
    periods = windows(
        today.year,
        today.month,
        evidence["coverage"],
        today=today,
        lookback=lookback_months,
    )
    result = {
        "current_month": {"year": today.year, "month": today.month},
        "lookback_months": lookback_months,
        "threshold_pct": threshold_pct,
        "min_abs_deviation": min_abs_deviation,
        "categories": [],
        "anomalies": [],
        "status": "insufficient_history",
        "periods": periods,
        "message": "Insufficient recorded coverage or unresolved messages; no anomaly percentage calculated.",
    }
    if not periods or not all(covered(evidence, a, b) for a, b in periods):
        return result
    for row in evidence["rows"]:
        row["category"] = row["category"] or "Unclassified"
    categories = []
    for category in sorted(
        {r["category"] for r in evidence["rows"] if r["direction"] == "out"}
    ):
        amounts = [total(evidence["rows"], a, b, category) for a, b in periods]
        current = amounts[0]
        average = sum(amounts[1:]) / lookback_months
        pct = float((current - average) / average * 100) if average else None
        deviation = float(current - average)
        categories.append(
            {
                "category": category,
                "current_amount": float(current),
                "avg_amount": float(average),
                "velocity_pct": pct,
                "abs_deviation": deviation,
                "status": "comparable" if average else "new" if current else "zero",
                "direction": (
                    "up" if deviation > 0 else "down" if deviation < 0 else "flat"
                ),
                "is_anomaly": pct is not None
                and pct > threshold_pct
                and deviation > min_abs_deviation,
            }
        )
    categories.sort(key=lambda c: (not c["is_anomaly"], -(c["velocity_pct"] or 0)))
    result.update(
        status="comparable",
        categories=categories,
        anomalies=[c for c in categories if c["is_anomaly"]],
        message=f"First {periods[0][1].day} completed days compared across {lookback_months} prior months; heuristic, not a budget.",
    )
    return result


# ── Pretty printing ──────────────────────────────────────────────

MONTH_ABBR = [
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]


def print_report(result: Dict[str, Any]) -> None:
    cm = result["current_month"]
    month_label = f"{MONTH_ABBR[cm['month']]} {cm['year']}"
    lookback = result["lookback_months"]

    print()
    print(f"  Spend Velocity Report — {month_label}")
    print(f"  Comparing against {lookback}-month rolling average")
    print(
        f"  Threshold: >{result['threshold_pct']}% AND >{format_currency(result['min_abs_deviation'])}"
    )
    print()

    print(result["message"])
    if result["status"] != "comparable":
        return

    # Header
    cat_w = 22
    amt_w = 14
    vel_w = 10
    flag_w = 12
    header = (
        f"  {'Category':<{cat_w}}"
        f"{'3mo Avg':>{amt_w}}"
        f"{'This Month':>{amt_w}}"
        f"{'Velocity':>{vel_w}}"
        f"{'':>{flag_w}}"
    )
    print(header)
    print("  " + "─" * (cat_w + amt_w * 2 + vel_w + flag_w))

    for c in result["categories"]:
        cat_name = c["category"][:cat_w]
        avg_str = f"{format_currency(c['avg_amount'])}"
        cur_str = f"{format_currency(c['current_amount'])}"
        vel = c["velocity_pct"]
        vel_str = f"{vel:+.1f}%" if vel is not None else c["status"]

        if c["is_anomaly"]:
            flag = "!! ANOMALY"
        elif vel is not None and vel > 0:
            flag = ""
        else:
            flag = ""

        print(
            f"  {cat_name:<{cat_w}}"
            f"{avg_str:>{amt_w}}"
            f"{cur_str:>{amt_w}}"
            f"{vel_str:>{vel_w}}"
            f"{flag:>{flag_w}}"
        )

    print()
    anomaly_count = len(result["anomalies"])
    total_cats = len(result["categories"])
    if anomaly_count:
        print(
            f"  {anomaly_count} anomal{'y' if anomaly_count == 1 else 'ies'} detected across {total_cats} categories"
        )
        total_excess = sum(a["abs_deviation"] for a in result["anomalies"])
        print(f"  Total excess spend: {format_currency(total_excess)}")
    else:
        print(
            f"  No anomalies detected across {total_cats} categories. No category exceeded the stated heuristic thresholds."
        )
    print()


# ── CLI ───────────────────────────────────────────────────────────


def main():
    parser = argparse.ArgumentParser(
        description="Detect spending velocity anomalies by category"
    )
    parser.add_argument(
        "--months",
        type=int,
        default=3,
        help="Number of lookback months for rolling average (default: 3)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=50.0,
        help="Velocity %% above average to flag as anomaly (default: 50)",
    )
    parser.add_argument(
        "--min-deviation",
        type=float,
        default=500.0,
        help="Minimum absolute ₹ deviation to flag (default: 500)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output raw JSON instead of formatted table"
    )

    args = parser.parse_args()

    result = detect_anomalies(
        lookback_months=args.months,
        threshold_pct=args.threshold,
        min_abs_deviation=args.min_deviation,
    )

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print_report(result)


if __name__ == "__main__":
    main()
