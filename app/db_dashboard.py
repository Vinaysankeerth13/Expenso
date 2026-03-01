"""
Dashboard-specific aggregate queries.
Internal only — never imported by routes or templates.
"""

import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from typing import List, Dict, Any, Optional
from datetime import date
from decimal import Decimal

log = logging.getLogger("db_dashboard")

DB_NAME = os.getenv("DB_NAME", "expenso")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "postgres")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")


def get_connection():
    return psycopg2.connect(
        dbname=DB_NAME, user=DB_USER, password=DB_PASS,
        host=DB_HOST, port=DB_PORT
    )


def get_available_months() -> List[Dict[str, int]]:
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT EXTRACT(YEAR FROM date)::int AS year,
                       EXTRACT(MONTH FROM date)::int AS month
                FROM transactions
                WHERE status = 'success' AND date IS NOT NULL
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
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT category FROM transactions
                WHERE category IS NOT NULL AND status = 'success'
                ORDER BY category
            """)
            return [r[0] for r in cur.fetchall()]
    except Exception as e:
        log.error(f"get_all_category_names failed: {e}")
        return []
    finally:
        conn.close()


def get_year_summary(year: int) -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    COALESCE(SUM(CASE WHEN direction = 'out' THEN amount ELSE 0 END), 0) AS total_spent,
                    COALESCE(SUM(CASE WHEN direction = 'in' THEN amount ELSE 0 END), 0) AS total_income,
                    COUNT(*) AS txn_count
                FROM transactions
                WHERE status = 'success'
                  AND EXTRACT(YEAR FROM date) = %s
            """, (year,))
            row = cur.fetchone()
            result = dict(row)
            result['savings'] = result['total_income'] - result['total_spent']
            return result
    except Exception as e:
        log.error(f"get_year_summary failed: {e}")
        return {'total_spent': 0, 'total_income': 0, 'savings': 0, 'txn_count': 0}
    finally:
        conn.close()


def get_monthly_totals(year: int) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    EXTRACT(MONTH FROM date)::int AS month,
                    COALESCE(SUM(CASE WHEN direction = 'out' THEN amount ELSE 0 END), 0) AS total_out,
                    COALESCE(SUM(CASE WHEN direction = 'in' THEN amount ELSE 0 END), 0) AS total_in,
                    COUNT(*) AS txn_count
                FROM transactions
                WHERE status = 'success'
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
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    category,
                    COALESCE(SUM(amount), 0) AS total,
                    COUNT(*) AS count
                FROM transactions
                WHERE status = 'success'
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
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    category,
                    COALESCE(SUM(amount), 0) AS total,
                    COUNT(*) AS count
                FROM transactions
                WHERE status = 'success'
                  AND direction = 'out'
                  AND EXTRACT(YEAR FROM date) = %s
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
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    date AS day,
                    COALESCE(SUM(CASE WHEN direction = 'out' THEN amount ELSE 0 END), 0) AS total
                FROM transactions
                WHERE status = 'success'
                  AND EXTRACT(YEAR FROM date) = %s
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


def get_top_merchants_for_month(year: int, month: int, limit: int = 10) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    merchant,
                    COALESCE(SUM(amount), 0) AS total,
                    COUNT(*) AS count
                FROM transactions
                WHERE status = 'success'
                  AND direction = 'out'
                  AND merchant IS NOT NULL
                  AND EXTRACT(YEAR FROM date) = %s
                  AND EXTRACT(MONTH FROM date) = %s
                GROUP BY merchant
                ORDER BY total DESC
                LIMIT %s
            """, (year, month, limit))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"get_top_merchants_for_month failed: {e}")
        return []
    finally:
        conn.close()


def get_month_comparison(year: int, month: int) -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT COALESCE(SUM(amount), 0) AS total
                FROM transactions
                WHERE status = 'success' AND direction = 'out'
                  AND EXTRACT(YEAR FROM date) = %s
                  AND EXTRACT(MONTH FROM date) = %s
            """, (year, month))
            current = float(cur.fetchone()['total'])

            # Previous month
            if month == 1:
                prev_year, prev_month = year - 1, 12
            else:
                prev_year, prev_month = year, month - 1

            cur.execute("""
                SELECT COALESCE(SUM(amount), 0) AS total
                FROM transactions
                WHERE status = 'success' AND direction = 'out'
                  AND EXTRACT(YEAR FROM date) = %s
                  AND EXTRACT(MONTH FROM date) = %s
            """, (prev_year, prev_month))
            previous = float(cur.fetchone()['total'])

            pct_change = 0.0
            if previous > 0:
                pct_change = ((current - previous) / previous) * 100

            return {
                'current_total': current,
                'prev_total': previous,
                'pct_change': pct_change,
            }
    except Exception as e:
        log.error(f"get_month_comparison failed: {e}")
        return {'current_total': 0, 'prev_total': 0, 'pct_change': 0}
    finally:
        conn.close()


def get_spending_intensity(year: int) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    EXTRACT(MONTH FROM date)::int AS month,
                    EXTRACT(ISODOW FROM date)::int AS weekday,
                    COALESCE(SUM(CASE WHEN direction = 'out' THEN amount ELSE 0 END), 0) AS total
                FROM transactions
                WHERE status = 'success'
                  AND EXTRACT(YEAR FROM date) = %s
                  AND date IS NOT NULL
                GROUP BY 1, 2
                ORDER BY 1, 2
            """, (year,))
            return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"get_spending_intensity failed: {e}")
        return []
    finally:
        conn.close()


def get_recent_transactions(year: int, month: int, limit: int = 5) -> List[Dict[str, Any]]:
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT id, date, amount, currency, direction, merchant, category
                FROM transactions
                WHERE status = 'success'
                  AND EXTRACT(YEAR FROM date) = %s
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


def search_transactions(
    query: Optional[str] = None,
    category: Optional[str] = None,
    year: Optional[int] = None,
    month: Optional[int] = None,
    offset: int = 0,
    limit: int = 50,
) -> Dict[str, Any]:
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            conditions = ["status = 'success'"]
            params: list = []

            if query:
                conditions.append("(merchant ILIKE %s OR notes ILIKE %s)")
                params.extend([f"%{query}%", f"%{query}%"])
            if category:
                conditions.append("category = %s")
                params.append(category)
            if year:
                conditions.append("EXTRACT(YEAR FROM date) = %s")
                params.append(year)
            if month:
                conditions.append("EXTRACT(MONTH FROM date) = %s")
                params.append(month)

            where = " AND ".join(conditions)

            # Count
            cur.execute(f"SELECT COUNT(*) AS cnt FROM transactions WHERE {where}", params)
            total_count = cur.fetchone()['cnt']

            # Aggregates
            cur.execute(f"""
                SELECT
                    COALESCE(SUM(CASE WHEN direction='out' THEN amount ELSE 0 END), 0) AS total_spent,
                    COALESCE(AVG(CASE WHEN direction='out' THEN amount END), 0) AS avg_amount
                FROM transactions WHERE {where}
            """, params)
            agg = cur.fetchone()

            # Rows
            cur.execute(f"""
                SELECT id, date, amount, currency, direction, merchant, category
                FROM transactions
                WHERE {where}
                ORDER BY date DESC, created_at DESC
                OFFSET %s LIMIT %s
            """, params + [offset, limit])
            rows = [dict(r) for r in cur.fetchall()]

            return {
                'transactions': rows,
                'total_count': total_count,
                'total_spent': float(agg['total_spent']),
                'avg_amount': float(agg['avg_amount']),
            }
    except Exception as e:
        log.error(f"search_transactions failed: {e}")
        return {'transactions': [], 'total_count': 0, 'total_spent': 0, 'avg_amount': 0}
    finally:
        conn.close()
