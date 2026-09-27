"""
Presentation layer for the dashboard.
Transforms raw DB results into clean, frontend-safe data structures.
No DB column names, no SQL artifacts leak past this layer.
"""

import re
import logging
from collections import Counter
from datetime import date, datetime
from typing import Dict, Any, List, Optional

from app import db_dashboard, db_transactions
from app.anomaly_detector import detect_anomalies
from app.config import MERCHANT_PREFIX_PATTERN
from app.reporting_periods import read_evidence, calendar_values, coverage_label
from app.db_pipeline import pipeline_today
from app.services.formatting import format_currency

log = logging.getLogger(__name__)

_MERCHANT_PREFIXES = re.compile(MERCHANT_PREFIX_PATTERN, re.IGNORECASE)

MONTH_NAMES = [
    "", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
]

MONTH_FULL_NAMES = [
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
]

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

CATEGORY_COLORS = {
    "Food & Dining": "#C97A3D",
    "Groceries": "#9E8A2E",
    "Entertainment": "#A34D74",
    "Subscriptions": "#7A5C9E",
    "Shopping": "#3D5C99",
    "Transport & Fuel": "#5C8A99",
    "Health & Personal": "#4C7A3F",
    "Transfers": "#6B6B6B",
    "Income": "#2E6B4A",
    "Other": "#8A6B4D",
}
# Not an assignable category, so it stays out of CATEGORY_COLORS (which feeds the edit menu).
UNCLASSIFIED_COLOR = "#BDB6A8"

CATEGORY_ICONS = {
    "Food & Dining": "utensils",
    "Groceries": "shopping-basket",
    "Entertainment": "film",
    "Subscriptions": "repeat",
    "Shopping": "shopping-bag",
    "Transport & Fuel": "car",
    "Health & Personal": "heart",
    "Transfers": "arrow-right-left",
    "Income": "trending-up",
    "Other": "circle",
}


def _category_name(category: Optional[str]) -> str:
    """Keep pending classification distinct from the assigned Other category."""
    return category or "Unclassified"


def _slug(text: str) -> str:
    """Convert category name to a CSS-safe slug."""
    return text.lower().replace(" & ", "-").replace(" ", "-")


def _format_date(d) -> str:
    """Format a date object to '20 Feb, 2026'."""
    if isinstance(d, date):
        return d.strftime("%-d %b, %Y")
    return str(d) if d else ""


def _clean_merchant(merchant: str) -> str:
    """Strip payment prefixes and UPI VPAs for display."""
    if not merchant:
        return "Unknown"
    text = merchant.strip()
    text = _MERCHANT_PREFIXES.sub("", text)
    if "@" in text:
        parts = text.split(None, 1)
        if len(parts) == 2:
            text = parts[1]
        else:
            text = parts[0].split("@")[0]
    return text.strip() or "Unknown"


def get_navigation_data() -> Dict[str, Any]:
    """Data needed by the nav bar on every page."""
    months = db_dashboard.get_available_months()
    return {
        "availableMonths": [
            {"year": m["year"], "month": m["month"], "label": f"{MONTH_NAMES[m['month']]} {m['year']}"}
            for m in months
        ]
    }


def get_year_page_data(year: int) -> Dict[str, Any]:
    summary = db_dashboard.get_year_summary(year)
    categories = db_dashboard.get_category_totals_for_year(year)

    evidence = read_evidence(date(year,1,1), min(date(year,12,31),pipeline_today()))
    monthly_values = calendar_values(evidence,year)
    incoming_values = calendar_values(evidence,year,direction="in")
    monthly = [{'month':m, 'total_out':monthly_values[m-1], 'total_in':incoming_values[m-1]}
               for m in range(1,13)]
    cat_total = sum(float(c['total']) for c in categories) or 1

    return {
        "year": year,
        "coverageLabel": coverage_label(evidence),
        "summary": {
            "totalSpent": format_currency(summary.get('total_spent', 0)),
            "income": format_currency(summary.get('total_income', 0)),
            "savings": format_currency(summary.get('savings', 0)),
            "transactionCount": summary.get('txn_count', 0),
        },
        "monthlyTrend": {
            "labels": [MONTH_NAMES[m['month']] for m in monthly],
            "spent": [float(m['total_out']) if m['total_out'] is not None else None for m in monthly],
            "income": [float(m['total_in']) if m['total_in'] is not None else None for m in monthly],
        },
        "categories": [
            {
                "name": _category_name(c['category']),
                "amount": format_currency(c['total']),
                "rawAmount": float(c['total']),
                "count": c['count'],
                "color": CATEGORY_COLORS.get(c['category'], UNCLASSIFIED_COLOR),
                "slug": _slug(_category_name(c['category'])),
                "percentage": round(float(c['total']) / cat_total * 100, 1),
            }
            for c in categories
        ],
        "heatmap": _build_spend_map(monthly),
        **get_navigation_data(),
    }


def _build_spend_map(monthly_data: list) -> Dict[str, Any]:
    """Build a treemap-style spend map: one block per month, sized + colored by spend."""
    max_spend = max((float(m['total_out'] or 0) for m in monthly_data), default=0) or 1

    blocks = []
    for m in monthly_data:
        spent = float(m['total_out'] or 0)
        ratio = spent / max_spend
        # Color level 0-4 based on relative spend
        if spent == 0:
            level = 0
        elif ratio < 0.25:
            level = 1
        elif ratio < 0.5:
            level = 2
        elif ratio < 0.75:
            level = 3
        else:
            level = 4
        blocks.append({
            "month": MONTH_NAMES[m['month']],
            "amount": format_currency(spent) if m['total_out'] is not None else "Unknown",
            "ratio": round(ratio, 3),
            "level": level,
        })

    return {"blocks": blocks}


def _get_anomaly_callouts(year: int, month: int) -> List[Dict[str, Any]]:
    today = date.today()
    if (year, month) != (today.year, today.month):
        return []
    try:
        result = detect_anomalies(lookback_months=3, threshold_pct=50.0, min_abs_deviation=500.0)
        callouts = []
        for a in result.get("anomalies", []):
            callouts.append({
                "category": _category_name(a["category"]),
                "color": CATEGORY_COLORS.get(a["category"], UNCLASSIFIED_COLOR),
                "velocity_pct": a["velocity_pct"],
                "current_amount": format_currency(a["current_amount"]),
                "avg_amount": format_currency(a["avg_amount"]),
                "abs_deviation": format_currency(a["abs_deviation"]),
                "direction": a["direction"],
            })
        return callouts
    except Exception:
        log.exception("Anomaly detection failed — skipping callouts")
        return []


def get_month_page_data(year: int, month: int) -> Dict[str, Any]:
    comparison = db_dashboard.get_month_comparison(year, month)
    categories = db_dashboard.get_category_totals_for_month(year, month)
    recent = db_dashboard.get_recent_transactions(year, month, limit=10)

    total_spent = comparison['current_total']
    cat_total = sum(float(c['total']) for c in categories) or 1
    top_cat = categories[0] if categories else None

    # Prev/next month
    if month == 1:
        prev_year, prev_month = year - 1, 12
    else:
        prev_year, prev_month = year, month - 1
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    return {
        "year": year,
        "month": month,
        "monthName": MONTH_FULL_NAMES[month],
        "coverageLabel": comparison["coverage_label"],
        "prevMonth": {"year": prev_year, "month": prev_month},
        "nextMonth": {"year": next_year, "month": next_month},
        "summary": {
            "totalSpent": format_currency(total_spent),
            "change": round(comparison['pct_change'], 1) if comparison['pct_change'] is not None else None,
            "comparisonLabel": comparison["period_label"],
            "comparisonMessage": comparison["message"],
            "changePositive": comparison['pct_change'] is not None and comparison['pct_change'] > 0,
            "topCategory": _category_name(top_cat['category']) if top_cat else "N/A",
            "topCategoryShare": round(float(top_cat['total']) / cat_total * 100) if top_cat else 0,
            "topCategoryColor": CATEGORY_COLORS.get(top_cat['category'], UNCLASSIFIED_COLOR) if top_cat else UNCLASSIFIED_COLOR,
        },
        "dailyTrend": {
            "labels": [str(day) for day in range(1,len(comparison["daily_values"])+1)],
            "amounts": comparison["daily_values"],
        },
        "categories": [
            {
                "name": _category_name(c['category']),
                "amount": format_currency(c['total']),
                "rawAmount": float(c['total']),
                "color": CATEGORY_COLORS.get(c['category'], UNCLASSIFIED_COLOR),
                "slug": _slug(_category_name(c['category'])),
                "percentage": round(float(c['total']) / cat_total * 100, 1),
            }
            for c in categories
        ],
        "recentTransactions": [
            {
                "merchant": _clean_merchant(t.get('merchant')),
                "amount": format_currency(t.get('amount'), show_sign=True) if t.get('direction') == 'in'
                          else f"-{format_currency(t.get('amount'))}",
                "date": _format_date(t.get('date')),
                "category": _category_name(t.get('category')),
                "categoryColor": CATEGORY_COLORS.get(_category_name(t.get('category')), UNCLASSIFIED_COLOR),
                "categorySlug": _slug(_category_name(t.get('category'))),
                "isIncome": t.get('direction') == 'in',
            }
            for t in recent
        ],
        "anomalies": _get_anomaly_callouts(year, month),
        "needsReviewCount": db_dashboard.get_low_confidence_count(),
        **get_navigation_data(),
    }


def get_transactions_page_data(
    query: Optional[str] = None,
    category: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    page: int = 1,
    per_page: int = 20,
    needs_review: bool = False,
) -> Dict[str, Any]:
    from app.db_classification import other_payment_counts

    def search(page):
        # The review list is sorted by merchant in Python, so it loads every match.
        return db_dashboard.search_transactions(
            query=query, category=category, year=year, month=month,
            offset=0 if needs_review else (page - 1) * per_page,
            limit=None if needs_review else per_page,
            needs_review=needs_review,
        )

    result = search(page)
    total_pages = (result['total_count'] + per_page - 1) // per_page if result['total_count'] > 0 else 1
    # Fixing reviewed rows can empty the last page of the review list.
    if page > total_pages:
        page = total_pages
        if not needs_review:
            result = search(page)
    rows = result['transactions']
    if needs_review:
        rows = _by_merchant_frequency(rows)[(page - 1) * per_page : page * per_page]

    return {
        "summary": {
            "totalSpent": format_currency(result['total_spent']),
            "count": result['total_count'],
            "average": format_currency(result['avg_amount']),
        },
        "transactions": [
            {
                **_transaction_row(t, similar),
                "groupSize": t.get("group_size", 0),
                "groupStart": needs_review and t.get("group_size", 0) > 1
                and (i == 0 or rows[i - 1].get("group_key") != t.get("group_key")),
            }
            for i, (t, similar) in enumerate(zip(rows, other_payment_counts(rows)))
        ],
        "needsReviewCount": db_dashboard.get_low_confidence_count(),
        "categoryOptions": get_category_options(),
        "filters": {
            "query": query or "",
            "category": category or "",
            "needsReview": needs_review,
            "categories": db_dashboard.get_all_category_names(),
        },
        "pagination": {
            "page": page,
            "totalPages": total_pages,
            "hasNext": page < total_pages,
            "hasPrev": page > 1,
        },
        **get_navigation_data(),
    }


def get_category_options() -> List[Dict[str, str]]:
    """Category list for the inline-edit dropdown."""
    return [
        {"name": k, "color": v, "slug": _slug(k)}
        for k, v in CATEGORY_COLORS.items()
    ]


def _by_merchant_frequency(rows):
    """Largest groups of outgoing payments to one merchant first; incoming last.

    Groups use the same merchant key as 'Use for all payments to this merchant',
    so one edit at the top of the list clears the most rows.
    """
    from app.classify_transactions import normalize_merchant

    keyed = []
    for row in rows:
        outgoing = row.get("direction") == "out" and bool(row.get("merchant"))
        key = normalize_merchant(row["merchant"]) if outgoing else None
        keyed.append({**row, "group_key": key})
    sizes = Counter(r["group_key"] for r in keyed if r["group_key"])
    for r in keyed:
        r["group_size"] = sizes.get(r["group_key"], 0)
    # Stable sort keeps newest-first order inside each group.
    return sorted(keyed, key=lambda r: (r["group_key"] is None, -r["group_size"], r["group_key"] or ""))


def _transaction_row(row, similar_count=0):
    category = _category_name(row.get('category'))
    return {
        "ref": row['id'],
        "merchant": _clean_merchant(row.get('merchant')),
        "amount": format_currency(row.get('amount'), show_sign=True) if row.get('direction') == 'in'
                  else f"-{format_currency(row.get('amount'))}",
        "date": _format_date(row.get('date')),
        "category": category,
        "categoryColor": CATEGORY_COLORS.get(category, UNCLASSIFIED_COLOR),
        "categorySlug": _slug(category),
        "isIncome": row.get('direction') == 'in',
        "needsReview": not row.get('category') or (row.get('classification_source') != 'manual'
            and (row.get('classification_confidence') is None or not 0.9 <= row['classification_confidence'] <= 1)),
        "similarCount": similar_count,
    }


def _edited_transaction(row):
    from app.db_classification import other_payment_counts

    return {
        **_transaction_row(row, other_payment_counts([row])[0]),
        "success": True,
        "editId": row.get('edit_id'),
        "batchCount": row.get('batch_count', 0),
        "batchReverted": row.get('batch_reverted', 0),
    }


def update_category(ref: int, new_category: str, *, learn_merchant=False) -> Dict[str, Any]:
    from app.db_classification import edit_category
    return _edited_transaction(edit_category(ref, new_category, learn_merchant=learn_merchant))


def undo_category(edit_id: int) -> Dict[str, Any]:
    from app.db_classification import undo_category as undo
    return _edited_transaction(undo(edit_id))
