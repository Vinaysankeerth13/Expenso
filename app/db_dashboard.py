"""Dashboard aggregate queries — read-only, never called from scripts."""

import logging
import os
from typing import Any, Dict, List, Optional

from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from app.db_connection import get_connection as _connect

log = logging.getLogger(__name__)


def get_connection():
    """Use the same reporting day/timezone as pipeline checkpoints."""
    conn = _connect()
    try:
        conn.set_session(readonly=True)
        with conn.cursor() as cur:
            cur.execute("SELECT set_config('TimeZone', %s, true)",
                        (os.environ.get('EXPENSO_TIMEZONE', 'Asia/Kolkata'),))
        return conn
    except Exception:
        conn.close()
        raise



def get_available_months() -> List[Dict[str, int]]:
    """Returns every year/month pair that has at least one successful transaction, newest first."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT EXTRACT(YEAR  FROM date)::int AS year,
                       EXTRACT(MONTH FROM date)::int AS month
                FROM transactions
                WHERE status = 'success' AND currency = 'INR' AND date <= CURRENT_DATE AND date IS NOT NULL
                GROUP BY 1, 2
                ORDER BY 1 DESC, 2 DESC
            """)
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"get_available_months failed: {e}")
        return []
    finally:
        conn.close()


def get_all_category_names() -> List[str]:
    """Returns the distinct category names present in successful transactions."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT category FROM transactions
                WHERE category IS NOT NULL AND status = 'success' AND currency = 'INR' AND date <= CURRENT_DATE
                ORDER BY category
            """)
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.error(f"get_all_category_names failed: {e}")
        return []
    finally:
        conn.close()


def get_year_summary(year: int) -> Dict[str, Any]:
    """Returns total spent, total income, savings, and transaction count for the given year."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN direction = 'out' THEN amount ELSE 0 END), 0) AS total_spent,
                    COALESCE(SUM(CASE WHEN direction = 'in'  THEN amount ELSE 0 END), 0) AS total_income,
                    COUNT(*) AS txn_count
                FROM transactions
                WHERE status = 'success' AND currency = 'INR' AND date <= CURRENT_DATE
                  AND EXTRACT(YEAR FROM date) = %s
            """, (year,))
            row = dict(cur.fetchone())
            row["savings"] = row["total_income"] - row["total_spent"]
            return row
    except Exception as e:
        log.error(f"get_year_summary failed: {e}")
        return {"total_spent": 0, "total_income": 0, "savings": 0, "txn_count": 0}
    finally:
        conn.close()


def get_monthly_totals(year: int) -> List[Dict[str, Any]]:
    """Returns monthly spend-in and spend-out totals for every month in the given year."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    EXTRACT(MONTH FROM date)::int AS month,
                    COALESCE(SUM(CASE WHEN direction = 'out' THEN amount ELSE 0 END), 0) AS total_out,
                    COALESCE(SUM(CASE WHEN direction = 'in'  THEN amount ELSE 0 END), 0) AS total_in,
                    COUNT(*) AS txn_count
                FROM transactions
                WHERE status = 'success' AND currency = 'INR' AND date <= CURRENT_DATE
                  AND EXTRACT(YEAR FROM date) = %s
                GROUP BY 1
                ORDER BY 1
            """, (year,))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"get_monthly_totals failed: {e}")
        return []
    finally:
        conn.close()


def get_category_totals_for_year(year: int) -> List[Dict[str, Any]]:
    """Returns total amount and transaction count per category for outgoing spend in the given year."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT category,
                       COALESCE(SUM(amount), 0) AS total,
                       COUNT(*) AS count
                FROM transactions
                WHERE status = 'success' AND currency = 'INR' AND date <= CURRENT_DATE
                  AND direction = 'out'
                  AND EXTRACT(YEAR FROM date) = %s
                GROUP BY category
                ORDER BY total DESC
            """, (year,))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"get_category_totals_for_year failed: {e}")
        return []
    finally:
        conn.close()


def get_category_totals_for_month(year: int, month: int) -> List[Dict[str, Any]]:
    """Returns total amount and transaction count per category for outgoing spend in the given month."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT category,
                       COALESCE(SUM(amount), 0) AS total,
                       COUNT(*) AS count
                FROM transactions
                WHERE status = 'success' AND currency = 'INR' AND date <= CURRENT_DATE
                  AND direction = 'out'
                  AND EXTRACT(YEAR  FROM date) = %s
                  AND EXTRACT(MONTH FROM date) = %s
                GROUP BY category
                ORDER BY total DESC
            """, (year, month))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"get_category_totals_for_month failed: {e}")
        return []
    finally:
        conn.close()


def get_daily_totals_for_month(year: int, month: int) -> List[Dict[str, Any]]:
    """Returns the total outgoing spend for each day in the given month."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT date AS day,
                       COALESCE(SUM(CASE WHEN direction = 'out' THEN amount ELSE 0 END), 0) AS total
                FROM transactions
                WHERE status = 'success' AND currency = 'INR' AND date <= CURRENT_DATE
                  AND EXTRACT(YEAR  FROM date) = %s
                  AND EXTRACT(MONTH FROM date) = %s
                GROUP BY date
                ORDER BY date
            """, (year, month))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"get_daily_totals_for_month failed: {e}")
        return []
    finally:
        conn.close()


def get_month_comparison(year: int, month: int) -> Dict[str, Any]:
    from app.reporting_periods import month_comparison
    return month_comparison(year, month)


def get_recent_transactions(year: int, month: int, limit: int = 5) -> List[Dict[str, Any]]:
    """Returns the most recent successful transactions for the given month, newest first."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, date, amount, currency, direction, merchant, category
                FROM transactions
                WHERE status = 'success' AND currency = 'INR' AND date <= CURRENT_DATE
                  AND EXTRACT(YEAR  FROM date) = %s
                  AND EXTRACT(MONTH FROM date) = %s
                ORDER BY date DESC, created_at DESC
                LIMIT %s
            """, (year, month, limit))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"get_recent_transactions failed: {e}")
        return []
    finally:
        conn.close()


# ── search_transactions helpers ──────────────────────────────────────────────

def _build_where(
    query: Optional[str],
    category: Optional[str],
    year: Optional[int],
    month: Optional[int],
    needs_review: bool = False,
):
    """Builds a psycopg2 SQL WHERE clause and matching params list from optional filter values."""
    clauses = [sql.SQL("status = 'success' AND currency = 'INR' AND date <= CURRENT_DATE")]
    params: list = []

    if needs_review:
        clauses.append(sql.SQL(NEEDS_REVIEW_SQL))
    if query:
        clauses.append(sql.SQL("(merchant ILIKE %s OR notes ILIKE %s)"))
        params.extend([f"%{query}%", f"%{query}%"])
    if category:
        clauses.append(sql.SQL("category = %s"))
        params.append(category)
    if year:
        clauses.append(sql.SQL("EXTRACT(YEAR FROM date) = %s"))
        params.append(year)
    if month:
        clauses.append(sql.SQL("EXTRACT(MONTH FROM date) = %s"))
        params.append(month)

    where = sql.SQL(" AND ").join(clauses)
    return where, params


def _fetch_count(cur, where, params: list) -> int:
    """Runs a COUNT(*) query with the given WHERE clause and returns the integer result."""
    cur.execute(
        sql.SQL("SELECT COUNT(*) AS cnt FROM transactions WHERE {}").format(where),
        params,
    )
    return cur.fetchone()["cnt"]


def _fetch_aggregates(cur, where, params: list) -> Dict[str, float]:
    """Runs SUM and AVG aggregates over outgoing transactions matching the WHERE clause."""
    cur.execute(
        sql.SQL("""
            SELECT
                COALESCE(SUM(CASE WHEN direction='out' THEN amount ELSE 0 END), 0) AS total_spent,
                COALESCE(AVG(CASE WHEN direction='out' THEN amount END), 0)         AS avg_amount
            FROM transactions WHERE {}
        """).format(where),
        params,
    )
    row = cur.fetchone()
    return {"total_spent": float(row["total_spent"]), "avg_amount": float(row["avg_amount"])}


def _fetch_rows(cur, where, params: list, offset: int, limit: int) -> List[Dict[str, Any]]:
    """Fetches a paginated slice of transactions matching the WHERE clause."""
    cur.execute(
        sql.SQL("""
            SELECT id, date, amount, currency, direction, merchant, category,
                   classification_confidence, classification_source
            FROM transactions
            WHERE {}
            ORDER BY date DESC, created_at DESC
            OFFSET %s LIMIT %s
        """).format(where),
        params + [offset, limit],
    )
    return [dict(r) for r in cur.fetchall()]


NEEDS_REVIEW_SQL = """(category IS NULL OR (classification_source IS DISTINCT FROM 'manual'
    AND (classification_confidence IS NULL OR classification_confidence < 0.9
         OR classification_confidence > 1)))"""


def get_low_confidence_count() -> int:
    """Returns unclassified or uncertain automatic decisions across all sources."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM transactions WHERE status = 'success' AND currency = 'INR'"
                " AND date <= CURRENT_DATE AND " + NEEDS_REVIEW_SQL
            )
            return cur.fetchone()[0]
    except Exception as e:
        log.error(f"get_low_confidence_count failed: {e}")
        return 0
    finally:
        conn.close()


def search_transactions(
    query: Optional[str] = None,
    category: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    offset: int = 0,
    limit: int = 50,
    needs_review: bool = False,
) -> Dict[str, Any]:
    """Searches transactions by optional filters and returns paginated rows plus count and aggregate totals."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            where, params = _build_where(query, category, year, month, needs_review)
            total_count = _fetch_count(cur, where, params)
            agg         = _fetch_aggregates(cur, where, params)
            rows        = _fetch_rows(cur, where, params, offset, limit)
            return {
                "transactions": rows,
                "total_count":  total_count,
                **agg,
            }
    except Exception as e:
        log.error(f"search_transactions failed: {e}")
        return {"transactions": [], "total_count": 0, "total_spent": 0, "avg_amount": 0}
    finally:
        conn.close()
