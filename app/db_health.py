"""Database queries used exclusively by scripts/check_parsing_health.py."""

import logging
from typing import Any, Dict, List

from psycopg2.extras import RealDictCursor

from app.db_connection import get_connection

log = logging.getLogger(__name__)


def get_blank_merchant_stats(window_days: int = 7) -> Dict[str, int]:
    """Returns count of blank-merchant vs. total successful outgoing transactions
    in the trailing window_days. A sustained nonzero rate (historically ~0%)
    signals that merchant-name extraction broke — e.g. a bank changed its
    email wording (see IMPLEMENTATION.md for the 2026-05-07 HDFC incident)."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE merchant IS NULL OR merchant = '') AS blank,
                    COUNT(*) AS total
                FROM transactions
                WHERE status = 'success' AND direction = 'out'
                  AND date >= (CURRENT_DATE - (%s || ' days')::interval)
                """,
                (window_days,),
            )
            row = cur.fetchone()
            return {"blank": row["blank"], "total": row["total"]}
    except Exception:
        log.exception("get_blank_merchant_stats failed")
        raise
    finally:
        conn.close()


def get_other_category_stats(window_days: int = 7) -> Dict[str, int]:
    """Returns count of category='Other' vs. total successful outgoing
    transactions in the trailing window_days. A sustained spike (historically
    ~0-2%) signals classification is degrading — from blank merchants, a new
    unrecognized merchant pattern, or the LLM classification path failing."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER (WHERE category = 'Other') AS other,
                    COUNT(*) AS total
                FROM transactions
                WHERE status = 'success' AND direction = 'out'
                  AND date >= (CURRENT_DATE - (%s || ' days')::interval)
                """,
                (window_days,),
            )
            row = cur.fetchone()
            return {"other": row["other"], "total": row["total"]}
    except Exception:
        log.exception("get_other_category_stats failed")
        raise
    finally:
        conn.close()


def get_blank_merchant_examples(window_days: int = 7, limit: int = 3) -> List[Dict[str, Any]]:
    """Returns sample raw email snippets for blank-merchant transactions in the
    trailing window, so an alert can show the actual wording that broke parsing."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT t.date, t.source, re.subject, re.snippet
                FROM transactions t
                JOIN raw_emails re ON re.gmail_message_id = t.raw_email_id
                WHERE t.status = 'success' AND t.direction = 'out'
                  AND (t.merchant IS NULL OR t.merchant = '')
                  AND t.date >= (CURRENT_DATE - (%s || ' days')::interval)
                ORDER BY t.date DESC
                LIMIT %s
                """,
                (window_days, limit),
            )
            return [dict(row) for row in cur.fetchall()]
    except Exception:
        log.exception("get_blank_merchant_examples failed")
        return []
    finally:
        conn.close()


def get_other_category_examples(window_days: int = 7, limit: int = 3) -> List[Dict[str, Any]]:
    """Returns sample merchant/snippet pairs for transactions classified as
    'Other' in the trailing window, so an alert can show what's not matching."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT t.date, t.merchant, re.subject, re.snippet
                FROM transactions t
                JOIN raw_emails re ON re.gmail_message_id = t.raw_email_id
                WHERE t.status = 'success' AND t.direction = 'out'
                  AND t.category = 'Other'
                  AND t.date >= (CURRENT_DATE - (%s || ' days')::interval)
                ORDER BY t.date DESC
                LIMIT %s
                """,
                (window_days, limit),
            )
            return [dict(row) for row in cur.fetchall()]
    except Exception:
        log.exception("get_other_category_examples failed")
        return []
    finally:
        conn.close()
