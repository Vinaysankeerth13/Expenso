"""Database queries used exclusively by the anomaly detector."""

import logging
from typing import Any, Dict, List

from psycopg2.extras import RealDictCursor

from app.db_connection import get_connection

log = logging.getLogger(__name__)


def get_monthly_category_totals(num_months: int = 4) -> List[Dict[str, Any]]:
    """Returns per-category monthly spend totals for the last num_months months, including the current partial month."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    EXTRACT(YEAR FROM date)::int  AS year,
                    EXTRACT(MONTH FROM date)::int AS month,
                    category,
                    SUM(amount)                   AS total_amount,
                    COUNT(*)                      AS txn_count
                FROM transactions
                WHERE direction = 'out'
                  AND status = 'success'
                  AND category IS NOT NULL
                  AND date >= (DATE_TRUNC('month', CURRENT_DATE) - INTERVAL '%s months')::date
                GROUP BY 1, 2, 3
                ORDER BY 1, 2, 3
                """,
                (num_months,),
            )
            return [dict(row) for row in cur.fetchall()]
    except Exception:
        log.exception("Failed to fetch monthly category totals")
        return []
    finally:
        conn.close()
