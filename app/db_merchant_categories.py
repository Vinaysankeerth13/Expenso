"""
Database operations for merchant_categories table.
Handles lookup, insertion, and querying of merchant-to-category mappings.
"""

import os
import logging
import psycopg2
from psycopg2.extras import RealDictCursor
from typing import List, Dict, Any, Optional

log = logging.getLogger("db_merchant_categories")

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


def lookup_merchant_category(normalized_merchant: str) -> Optional[Dict[str, Any]]:
    """
    Look up a category for a normalized merchant name.
    Tries exact match first, then checks if any pattern is a substring of the merchant.

    Args:
        normalized_merchant: Lowercased, cleaned merchant name

    Returns:
        Dict with category, subcategory, confidence, source — or None
    """
    if not normalized_merchant:
        return None

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # First: exact match
            cur.execute(
                "SELECT * FROM merchant_categories WHERE merchant_pattern = %s",
                (normalized_merchant,)
            )
            row = cur.fetchone()
            if row:
                return dict(row)

            # Second: check if any stored pattern is a substring of the merchant
            cur.execute(
                """SELECT * FROM merchant_categories
                   WHERE %s ILIKE '%%' || merchant_pattern || '%%'
                   ORDER BY LENGTH(merchant_pattern) DESC
                   LIMIT 1""",
                (normalized_merchant,)
            )
            row = cur.fetchone()
            if row:
                return dict(row)

            return None
    except Exception as e:
        log.error(f"Failed to look up merchant category: {e}")
        return None
    finally:
        conn.close()


def insert_merchant_category(pattern: str, category: str,
                             subcategory: Optional[str] = None,
                             confidence: float = 1.0,
                             source: str = 'manual') -> bool:
    """
    Insert or update a merchant pattern → category mapping.
    Uses ON CONFLICT for idempotent upserts.
    """
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
                (pattern, category, subcategory, confidence, source)
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
    """Returns all merchant category mappings."""
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
    """
    Returns classification coverage statistics across transactions.
    """
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
                WHERE status = 'success';
            """)
            stats = dict(cur.fetchone())

            # Breakdown by category
            cur.execute("""
                SELECT
                    COALESCE(category, 'UNCLASSIFIED') as category,
                    COUNT(*) as count,
                    SUM(CASE WHEN direction = 'out' THEN amount ELSE 0 END) as total_spent
                FROM transactions
                WHERE status = 'success'
                GROUP BY category
                ORDER BY count DESC;
            """)
            stats['by_category'] = [dict(row) for row in cur.fetchall()]

            # Breakdown by classification source
            cur.execute("""
                SELECT
                    COALESCE(classification_source, 'none') as source,
                    COUNT(*) as count
                FROM transactions
                WHERE status = 'success'
                GROUP BY classification_source
                ORDER BY count DESC;
            """)
            stats['by_source'] = [dict(row) for row in cur.fetchall()]

            return stats
    except Exception as e:
        log.error(f"Failed to get classification stats: {e}")
        return {}
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("Merchant Categories:")
    for cat in get_all_categories():
        print(f"  {cat['merchant_pattern']:30s} -> {cat['category']}")

    print("\nClassification Stats:")
    stats = get_classification_stats()
    print(f"  Total: {stats.get('total_transactions', 0)}")
    print(f"  Classified: {stats.get('classified', 0)}")
    print(f"  Unclassified: {stats.get('unclassified', 0)}")
