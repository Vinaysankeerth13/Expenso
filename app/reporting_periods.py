"""Comparable captured-INR periods. Mailbox coverage is not bank reconciliation."""

from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal
from psycopg2.extras import RealDictCursor
from app.db_connection import get_connection
from app.db_pipeline import ingestion_scope, pipeline_timezone, pipeline_today

# Bank alerts arrive the same day as the movement; a week of slack matches the
# incremental scan overlap, and the parser rejects dates after receipt + 1 day.
UNRESOLVED_LOOKBACK_DAYS = 7


def month_start(year, month, offset=0):
    number = year * 12 + month - 1 + offset
    return date(number // 12, number % 12 + 1, 1)


def month_end(start):
    return start.replace(day=monthrange(start.year, start.month)[1])


def read_evidence(start, end):
    from app.ingest import HDFC_SENDERS, get_keywords

    scope = ingestion_scope(HDFC_SENDERS, get_keywords())
    conn = get_connection()
    conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT coverage_start, covered_through FROM ingestion_checkpoints WHERE scope=%s",
                (scope,),
            )
            coverage = cur.fetchone()
            cur.execute(
                """SELECT date, amount, direction, category FROM transactions
                WHERE status='success' AND currency='INR' AND date BETWEEN %s AND %s ORDER BY date""",
                (start, end),
            )
            rows = [dict(r) for r in cur.fetchall()]
            # A reviewed conflict remains unresolved even if an old row exists.
            cur.execute(
                """SELECT r.received_at FROM raw_emails r
                LEFT JOIN email_processing_outcomes o ON o.raw_email_id=r.gmail_message_id
                WHERE (r.bank='hdfc' OR r.bank IS NULL) AND (
                  o.outcome IN ('review','failed') OR
                  (o.raw_email_id IS NULL AND NOT EXISTS
                    (SELECT 1 FROM transactions t WHERE t.raw_email_id=r.gmail_message_id)
                   AND NOT EXISTS (SELECT 1 FROM transaction_notifications n WHERE n.raw_email_id=r.gmail_message_id)))"""
            )
            unresolved = [r["received_at"] for r in cur.fetchall()]
            return {
                "coverage": dict(coverage) if coverage else None,
                "rows": rows,
                "unresolved": unresolved,
            }
    finally:
        conn.rollback()
        conn.close()


def covered(evidence, start, end):
    coverage = evidence["coverage"]
    if (
        not coverage
        or not coverage["coverage_start"] <= start <= end <= coverage["covered_through"]
    ):
        return False
    return not any(
        _may_affect(received, start, end) for received in evidence.get("unresolved", [])
    )


def _may_affect(received, start, end):
    """An unparsed message can only describe a movement shortly before its receipt."""
    if received is None:
        return True
    day = (
        received.astimezone(pipeline_timezone()).date()
        if isinstance(received, datetime)
        else received
    )
    return (
        day - timedelta(days=UNRESOLVED_LOOKBACK_DAYS) <= end
        and start <= day + timedelta(days=1)
    )


def windows(year, month, coverage, *, today=None, lookback=1):
    today = today or pipeline_today()
    current = month_start(year, month)
    previous = [month_start(year, month, -i) for i in range(1, lookback + 1)]
    if current > today:
        return []
    if (year, month) < (today.year, today.month):
        return [(s, month_end(s)) for s in [current] + previous]
    cutoff = (
        min(today - timedelta(days=1), coverage["covered_through"])
        if coverage
        else today - timedelta(days=1)
    )
    if cutoff < current:
        return []
    days = min(cutoff.day, *(month_end(s).day for s in previous))
    return [(s, s.replace(day=days)) for s in [current] + previous]


def total(rows, start, end, category=None):
    return sum(
        (
            Decimal(str(r["amount"]))
            for r in rows
            if r["direction"] == "out"
            and start <= r["date"] <= end
            and (category is None or r["category"] == category)
        ),
        Decimal(0),
    )


def compare(year, month, evidence, *, today=None):
    today = today or pipeline_today()
    start = month_start(year, month)
    periods = windows(year, month, evidence["coverage"], today=today)
    result = {
        "current_total": total(evidence["rows"], start, min(month_end(start), today)),
        "prev_total": None,
        "comparison_total": None,
        "pct_change": None,
        "status": "incomplete",
        "period_label": "Comparison unavailable",
        "message": "Insufficient recorded coverage or unresolved messages.",
        "coverage": evidence["coverage"],
        "daily_values": calendar_values(evidence, year, month),
        "coverage_label": coverage_label(evidence),
        "periods": periods,
    }
    if not periods:
        result["message"] = "No completed days available for comparison."
        return result
    (a, b), (c, d) = periods
    result["period_label"] = f"{a:%d %b %Y}–{b:%d %b %Y} vs {c:%d %b %Y}–{d:%d %b %Y}"
    result["comparison_total"], result["prev_total"] = total(
        evidence["rows"], a, b
    ), total(evidence["rows"], c, d)
    if not all(covered(evidence, x, y) for x, y in periods):
        return result
    current, previous = result["comparison_total"], result["prev_total"]
    if previous == 0:
        result.update(
            status="new" if current else "zero",
            message=(
                "New outflow; previous period had none."
                if current
                else "No captured outflow in either period."
            ),
        )
    else:
        result.update(
            status="comparable",
            pct_change=float((current - previous) / previous * 100),
            message="Compared using completed days and recorded mailbox coverage.",
        )
    return result


def month_comparison(year, month):
    today = pipeline_today()
    return compare(
        year,
        month,
        read_evidence(
            month_start(year, month, -1),
            min(month_end(month_start(year, month)), today),
        ),
        today=today,
    )


def calendar_values(evidence, year, month=None, direction="out"):
    """Keep captured values visible; only verified empty periods become zero."""
    periods = (
        [
            (date(year, month, day), date(year, month, day))
            for day in range(1, monthrange(year, month)[1] + 1)
        ]
        if month
        else [(date(year, m, 1), month_end(date(year, m, 1))) for m in range(1, 13)]
    )
    values = []
    for start, end in periods:
        amount = sum((Decimal(str(row["amount"])) for row in evidence["rows"]
                      if row["direction"] == direction and start <= row["date"] <= end), Decimal(0))
        observed = amount > 0
        values.append(
            float(amount)
            if observed or covered(evidence, start, end)
            else None
        )
    return values


def coverage_label(evidence):
    coverage = evidence["coverage"]
    if not coverage:
        return "Coverage unverified: captured totals may be partial."
    suffix = (
        " Unresolved messages may affect totals." if evidence.get("unresolved") else ""
    )
    return (
        f"Mailbox scan coverage: {coverage['coverage_start']:%d %b %Y}–{coverage['covered_through']:%d %b %Y}. Captured values outside this range may be partial."
        + suffix
    )
