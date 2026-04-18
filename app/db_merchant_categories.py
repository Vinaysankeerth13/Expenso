"""Database operations for the merchant_categories table."""

import logging
from typing import Any, Dict, List, Optional

from psycopg2.extras import RealDictCursor

from app.db_connection import get_connection

log = logging.getLogger(__name__)


def lookup_merchant_category(normalized_merchant: str) -> Optional[Dict[str, Any]]:
    """Finds the best matching category for a merchant name using exact then substring match."""
    if not normalized_merchant:
        return None

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM merchant_categories WHERE merchant_pattern = %s",
                (normalized_merchant,),
            )
            row = cur.fetchone()
            if row:
                return dict(row)

            cur.execute(
                """SELECT * FROM merchant_categories
                   WHERE %s ILIKE '%%' || merchant_pattern || '%%'
                   ORDER BY LENGTH(merchant_pattern) DESC
                   LIMIT 1""",
                (normalized_merchant,),
            )
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as e:
        log.error(f"Failed to look up merchant category: {e}")
        return None
    finally:
        conn.close()


def insert_merchant_category(
    pattern: str,
    category: str,
    subcategory: Optional[str] = None,
    confidence: float = 1.0,
    source: str = "manual",
) -> bool:
    """Inserts or updates a merchant pattern → category mapping (upsert)."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO merchant_categories
                       (merchant_pattern, category, subcategory, confidence, source)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (merchant_pattern) DO UPDATE SET
                       category = EXCLUDED.category,
                       subcategory = EXCLUDED.subcategory,
                       confidence = EXCLUDED.confidence,
                       source = EXCLUDED.source,
                       updated_at = NOW()""",
                (pattern, category, subcategory, confidence, source),
            )
            conn.commit()
            log.info(f"Upserted merchant category: '{pattern}' -> '{category}'")
            return True
    except Exception as e:
        log.error(f"Failed to insert merchant category: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def get_all_categories() -> List[Dict[str, Any]]:
    """Returns every merchant → category mapping, ordered by category then pattern."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT * FROM merchant_categories ORDER BY category, merchant_pattern"
            )
            return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        log.error(f"Failed to fetch merchant categories: {e}")
        return []
    finally:
        conn.close()


def get_classification_stats() -> Dict[str, Any]:
    """Returns classification coverage stats: total, classified, unclassified, breakdown by category and source."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    COUNT(*) as total_transactions,
                    COUNT(category) as classified,
                    COUNT(*) - COUNT(category) as unclassified,
                    COUNT(DISTINCT category) as distinct_categories
                FROM transactions
                WHERE status = 'success'
            """)
            stats = dict(cur.fetchone())

            cur.execute("""
                SELECT
                    COALESCE(category, 'UNCLASSIFIED') as category,
                    COUNT(*) as count,
                    SUM(CASE WHEN direction = 'out' THEN amount ELSE 0 END) as total_spent
                FROM transactions
                WHERE status = 'success'
                GROUP BY category
                ORDER BY count DESC
            """)
            stats["by_category"] = [dict(row) for row in cur.fetchall()]

            cur.execute("""
                SELECT
                    COALESCE(classification_source, 'none') as source,
                    COUNT(*) as count
                FROM transactions
                WHERE status = 'success'
                GROUP BY classification_source
                ORDER BY count DESC
            """)
            stats["by_source"] = [dict(row) for row in cur.fetchall()]
            return stats
    except Exception as e:
        log.error(f"Failed to get classification stats: {e}")
        return {}
    finally:
        conn.close()
