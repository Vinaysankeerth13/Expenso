"""
Database queries for spend velocity anomaly detection.
Returns monthly category totals for rolling-window analysis.
"""

import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from typing import List, Dict, Any

log = logging.getLogger("db_anomaly")

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


def get_monthly_category_totals(num_months: int = 4) -> List[Dict[str, Any]]:
    """
    Get total outgoing spend per category per month for the last `num_months` months
    (including the current/partial month).

    Returns list of dicts: {year, month, category, total_amount, txn_count}
    Ordered by year, month, category.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    EXTRACT(YEAR FROM date)::int   AS year,
                    EXTRACT(MONTH FROM date)::int  AS month,
                    category,
                    SUM(amount)                    AS total_amount,
                    COUNT(*)                       AS txn_count
                FROM transactions
                WHERE direction = 'out'
                  AND status = 'success'
                  AND category IS NOT NULL
                  AND date >= (DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '%s months')::date
                GROUP BY 1, 2, 3
                ORDER BY 1, 2, 3
            """, (num_months,))
            return [dict(row) for row in cur.fetchall()]
    except Exception:
        log.exception("Failed to fetch monthly category totals")
        return []
    finally:
        conn.close()
