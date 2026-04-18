"""
Presentation layer for the dashboard.
Transforms raw DB results into clean, frontend-safe data structures.
No DB column names, no SQL artifacts leak past this layer.
"""

from datetime import date, datetime
from typing import Dict, Any, List, Optional
from app import db_dashboard, db_transactions
import logging

log = logging.getLogger("dashboard_service")

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
    "Food & Dining": "#DC2626",
    "Groceries": "#EA580C",
    "Entertainment": "#D97706",
    "Subscriptions": "#7C3AED",
    "Shopping": "#2563EB",
    "Transport & Fuel": "#0891B2",
    "Health & Personal": "#059669",
    "Transfers": "#6B7280",
    "Income": "#16A34A",
    "Other": "#9CA3AF",
}

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


def format_currency(amount, show_sign=False) -> str:
    """Format amount in Indian numbering system (1,23,456.00)."""
    if amount is None:
        return "0.00"
    val = float(amount)
    negative = val < 0
    val = abs(val)

    integer_part = int(val)
    decimal_part = f"{val - integer_part:.2f}"[1:]  # .XX

    s = str(integer_part)
    if len(s) > 3:
        last3 = s[-3:]
        rest = s[:-3]
        groups = []
        while rest:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        result = ",".join(groups) + "," + last3
    else:
        result = s

    formatted = result + decimal_part

    if show_sign:
        if negative:
            return f"-₹{formatted}"
        else:
            return f"+₹{formatted}"
    elif negative:
        return f"-₹{formatted}"
    return f"₹{formatted}"


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
    import re
    text = merchant.strip()
    text = re.sub(r"^(PYU\*|CAS\*|RSP\*|RAZ\*|Razorpay\*)", "", text, flags=re.IGNORECASE)
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
    monthly = db_dashboard.get_monthly_totals(year)
    categories = db_dashboard.get_category_totals_for_year(year)

    total_spent = float(summary.get('total_spent', 0))
    cat_total = sum(float(c['total']) for c in categories) or 1

    return {
        "year": year,
        "summary": {
            "totalSpent": format_currency(summary.get('total_spent', 0)),
            "income": format_currency(summary.get('total_income', 0)),
            "savings": format_currency(summary.get('savings', 0)),
            "transactionCount": summary.get('txn_count', 0),
        },
        "monthlyTrend": {
            "labels": [MONTH_NAMES[m['month']] for m in monthly],
            "spent": [float(m['total_out']) for m in monthly],
            "income": [float(m['total_in']) for m in monthly],
        },
        "categories": [
            {
                "name": c['category'],
                "amount": format_currency(c['total']),
                "rawAmount": float(c['total']),
                "count": c['count'],
                "color": CATEGORY_COLORS.get(c['category'], "#9CA3AF"),
                "slug": _slug(c['category']),
                "percentage": round(float(c['total']) / cat_total * 100, 1),
            }
            for c in categories
        ],
        "heatmap": _build_spend_map(monthly),
        **get_navigation_data(),
    }


def _build_spend_map(monthly_data: list) -> Dict[str, Any]:
    """Build a treemap-style spend map: one block per month, sized + colored by spend."""
    max_spend = max((float(m['total_out']) for m in monthly_data), default=0) or 1

    blocks = []
    for m in monthly_data:
        spent = float(m['total_out'])
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
            "amount": format_currency(spent),
            "ratio": round(ratio, 3),
            "level": level,
        })

    return {"blocks": blocks}


def _get_anomaly_callouts(year: int, month: int) -> List[Dict[str, Any]]:
    today = date.today()
    if (year, month) != (today.year, today.month):
        return []
    try:
        from app.anomaly_detector import detect_anomalies
        result = detect_anomalies(lookback_months=3, threshold_pct=50.0, min_abs_deviation=500.0)
        callouts = []
        for a in result.get("anomalies", []):
            callouts.append({
                "category": a["category"],
                "color": CATEGORY_COLORS.get(a["category"], "#9CA3AF"),
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
    daily = db_dashboard.get_daily_totals_for_month(year, month)
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
        "prevMonth": {"year": prev_year, "month": prev_month},
        "nextMonth": {"year": next_year, "month": next_month},
        "summary": {
            "totalSpent": format_currency(total_spent),
            "change": round(comparison['pct_change'], 1),
            "changePositive": comparison['pct_change'] >= 0,
            "topCategory": top_cat['category'] if top_cat else "N/A",
            "topCategoryShare": round(float(top_cat['total']) / cat_total * 100) if top_cat else 0,
            "topCategoryColor": CATEGORY_COLORS.get(top_cat['category'], "#9CA3AF") if top_cat else "#9CA3AF",
        },
        "dailyTrend": {
            "labels": [str(d['day'].day) for d in daily],
            "amounts": [float(d['total']) for d in daily],
        },
        "categories": [
            {
                "name": c['category'],
                "amount": format_currency(c['total']),
                "rawAmount": float(c['total']),
                "color": CATEGORY_COLORS.get(c['category'], "#9CA3AF"),
                "slug": _slug(c['category']),
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
                "category": t.get('category', 'Other'),
                "categoryColor": CATEGORY_COLORS.get(t.get('category', 'Other'), "#9CA3AF"),
                "categorySlug": _slug(t.get('category', 'Other')),
                "isIncome": t.get('direction') == 'in',
            }
            for t in recent
        ],
        "anomalies": _get_anomaly_callouts(year, month),
        **get_navigation_data(),
    }


def get_transactions_page_data(
    query: Optional[str] = None,
    category: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    page: int = 1,
    per_page: int = 20,
) -> Dict[str, Any]:
    offset = (page - 1) * per_page
    result = db_dashboard.search_transactions(
        query=query, category=category, year=year, month=month,
        offset=offset, limit=per_page,
    )

    total_pages = (result['total_count'] + per_page - 1) // per_page if result['total_count'] > 0 else 1

    return {
        "summary": {
            "totalSpent": format_currency(result['total_spent']),
            "count": result['total_count'],
            "average": format_currency(result['avg_amount']),
        },
        "transactions": [
            {
                "ref": t.get('id'),
                "merchant": _clean_merchant(t.get('merchant')),
                "amount": format_currency(t.get('amount'), show_sign=True) if t.get('direction') == 'in'
                          else f"-{format_currency(t.get('amount'))}",
                "date": _format_date(t.get('date')),
                "category": t.get('category', 'Other'),
                "categoryColor": CATEGORY_COLORS.get(t.get('category', 'Other'), "#9CA3AF"),
                "categorySlug": _slug(t.get('category', 'Other')),
                "isIncome": t.get('direction') == 'in',
            }
            for t in result['transactions']
        ],
        "categoryOptions": get_category_options(),
        "filters": {
            "query": query or "",
            "category": category or "",
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


def update_category(ref: int, new_category: str) -> Dict[str, Any]:
    """
    Update a transaction's category by its ref (integer PK).
    Auto-learns by upserting the merchant pattern into merchant_categories.
    Returns a presentation-ready transaction dict on success.
    """
    if new_category not in CATEGORY_COLORS:
        return {"success": False}

    row = db_transactions.update_transaction_category_by_id(ref, new_category)
    if not row:
        return {"success": False}

    # Auto-learn: save the manual correction for future rule-based matching
    raw_merchant = row.get('merchant')
    if raw_merchant:
        from app.classify_transactions import normalize_merchant
        from app.db_merchant_categories import insert_merchant_category
        normalized = normalize_merchant(raw_merchant)
        if normalized:
            insert_merchant_category(
                pattern=normalized,
                category=new_category,
                confidence=1.0,
                source='manual',
            )

    return {
        "success": True,
        "ref": row['id'],
        "merchant": _clean_merchant(raw_merchant),
        "amount": format_currency(row.get('amount'), show_sign=True) if row.get('direction') == 'in'
                  else f"-{format_currency(row.get('amount'))}",
        "date": _format_date(row.get('date')),
        "category": new_category,
        "categoryColor": CATEGORY_COLORS.get(new_category, "#9CA3AF"),
        "categorySlug": _slug(new_category),
        "isIncome": row.get('direction') == 'in',
    }
