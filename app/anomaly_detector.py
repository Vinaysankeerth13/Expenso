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
from typing import Dict, Any, List

from app import db_anomaly
from app.services.dashboard_service import format_currency

log = logging.getLogger("anomaly_detector")


def detect_anomalies(
    lookback_months: int = 3,
    threshold_pct: float = 50.0,
    min_abs_deviation: float = 500.0,
) -> Dict[str, Any]:
    """
    Detect spending anomalies by comparing current month spend per category
    against the rolling average of the previous `lookback_months` months.

    A category is flagged as an anomaly when BOTH:
      - velocity_pct > threshold_pct  (e.g. >50% above average)
      - abs_deviation > min_abs_deviation  (e.g. >₹500 above average)

    Returns a structured dict with per-category analysis and flagged anomalies.
    """
    today = date.today()
    current_year, current_month = today.year, today.month

    rows = db_anomaly.get_monthly_category_totals(lookback_months)

    # Pivot: {category -> {(year, month) -> amount}}
    cat_monthly: Dict[str, Dict[tuple, float]] = defaultdict(dict)
    for row in rows:
        key = (row["year"], row["month"])
        cat_monthly[row["category"]][key] = float(row["total_amount"])

    # Build list of lookback month keys (complete months, excluding current)
    lookback_keys = []
    y, m = current_year, current_month
    for _ in range(lookback_months):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
        lookback_keys.insert(0, (y, m))

    current_key = (current_year, current_month)

    categories = []
    for category in sorted(cat_monthly.keys()):
        monthly_data = cat_monthly[category]

        # Collect lookback amounts (0 if category had no spend that month)
        lookback_amounts = [monthly_data.get(k, 0.0) for k in lookback_keys]
        current_amount = monthly_data.get(current_key, 0.0)

        # Only consider categories that have some historical data
        non_zero_months = [a for a in lookback_amounts if a > 0]
        if not non_zero_months:
            avg_amount = 0.0
        else:
            avg_amount = sum(lookback_amounts) / len(lookback_amounts)

        if avg_amount > 0:
            velocity_pct = ((current_amount - avg_amount) / avg_amount) * 100
        elif current_amount > 0:
            velocity_pct = 100.0  # new spend with no history
        else:
            velocity_pct = 0.0

        abs_deviation = current_amount - avg_amount
        is_anomaly = velocity_pct > threshold_pct and abs_deviation > min_abs_deviation

        monthly_totals = [
            {"year": k[0], "month": k[1], "amount": monthly_data.get(k, 0.0)}
            for k in lookback_keys
        ]

        categories.append({
            "category": category,
            "monthly_totals": monthly_totals,
            "avg_amount": round(avg_amount, 2),
            "current_amount": round(current_amount, 2),
            "velocity_pct": round(velocity_pct, 1),
            "abs_deviation": round(abs_deviation, 2),
            "is_anomaly": is_anomaly,
            "direction": "up" if velocity_pct > 0 else "down" if velocity_pct < 0 else "flat",
        })

    # Sort: anomalies first, then by velocity descending
    categories.sort(key=lambda c: (not c["is_anomaly"], -c["velocity_pct"]))

    anomalies = [c for c in categories if c["is_anomaly"]]

    return {
        "current_month": {"year": current_year, "month": current_month},
        "lookback_months": lookback_months,
        "threshold_pct": threshold_pct,
        "min_abs_deviation": min_abs_deviation,
        "categories": categories,
        "anomalies": anomalies,
    }


# ── Pretty printing ──────────────────────────────────────────────

MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def print_report(result: Dict[str, Any]) -> None:
    cm = result["current_month"]
    month_label = f"{MONTH_ABBR[cm['month']]} {cm['year']}"
    lookback = result["lookback_months"]

    print()
    print(f"  Spend Velocity Report — {month_label}")
    print(f"  Comparing against {lookback}-month rolling average")
    print(f"  Threshold: >{result['threshold_pct']}% AND >{format_currency(result['min_abs_deviation'])}")
    print()

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
        vel_str = f"{'+' if vel >= 0 else ''}{vel:.1f}%"

        if c["is_anomaly"]:
            flag = "!! ANOMALY"
        elif vel > 0:
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
        print(f"  {anomaly_count} anomal{'y' if anomaly_count == 1 else 'ies'} detected across {total_cats} categories")
        total_excess = sum(a["abs_deviation"] for a in result["anomalies"])
        print(f"  Total excess spend: {format_currency(total_excess)}")
    else:
        print(f"  No anomalies detected across {total_cats} categories. Spending is within normal range.")
    print()


# ── CLI ───────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Detect spending velocity anomalies by category"
    )
    parser.add_argument(
        "--months", type=int, default=3,
        help="Number of lookback months for rolling average (default: 3)"
    )
    parser.add_argument(
        "--threshold", type=float, default=50.0,
        help="Velocity %% above average to flag as anomaly (default: 50)"
    )
    parser.add_argument(
        "--min-deviation", type=float, default=500.0,
        help="Minimum absolute ₹ deviation to flag (default: 500)"
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Output raw JSON instead of formatted table"
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
