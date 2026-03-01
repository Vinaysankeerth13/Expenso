"""
Database operations for transactions table.
Handles insertion, querying, and updates for extracted transaction events.
"""

import os
import logging
import psycopg2
from psycopg2.extras import execute_batch, RealDictCursor
from typing import List, Dict, Any, Optional
from datetime import datetime, date

log = logging.getLogger("db_transactions")

# Database config (same as db_raw_emails.py)
DB_NAME = os.getenv("DB_NAME", "expenso")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASS = os.getenv("DB_PASS", "postgres")
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")


def get_connection():
    """Establishes and returns a connection to the PostgreSQL database."""
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
        host=DB_HOST,
        port=DB_PORT
    )


def insert_transactions(transactions: List[Dict[str, Any]]) -> int:
    """
    Batch inserts transactions into the database.
    Uses ON CONFLICT to handle duplicates (idempotent).

    Args:
        transactions: List of transaction dicts with required fields

    Returns:
        Number of transactions inserted (excludes duplicates)
    """
    if not transactions:
        log.info("No transactions to insert.")
        return 0

    conn = get_connection()
    inserted_count = 0

    try:
        with conn.cursor() as cur:
            query = """
                INSERT INTO transactions (
                    transaction_id, raw_email_id, date, amount, currency,
                    direction, source, status, merchant, reference_id,
                    category, notes, parser_version, text_source, signals
                ) VALUES (
                    %(transaction_id)s, %(raw_email_id)s, %(date)s, %(amount)s, %(currency)s,
                    %(direction)s, %(source)s, %(status)s, %(merchant)s, %(reference_id)s,
                    %(category)s, %(notes)s, %(parser_version)s, %(text_source)s, %(signals)s
                )
                ON CONFLICT (transaction_id)
                DO UPDATE SET
                    updated_at = NOW(),
                    amount = EXCLUDED.amount,
                    date = EXCLUDED.date,
                    merchant = EXCLUDED.merchant,
                    category = EXCLUDED.category
                RETURNING id;
            """

            # Prepare data with defaults
            prepared_data = []
            for txn in transactions:
                prepared_data.append({
                    'transaction_id': txn['transaction_id'],
                    'raw_email_id': txn['raw_email_id'],
                    'date': txn.get('date'),
                    'amount': txn.get('amount'),
                    'currency': txn.get('currency', 'INR'),
                    'direction': txn.get('direction', 'unknown'),
                    'source': txn.get('source', 'unknown'),
                    'status': txn.get('status', 'unknown'),
                    'merchant': txn.get('merchant'),
                    'reference_id': txn.get('reference_id'),
                    'category': txn.get('category'),
                    'notes': txn.get('notes'),
                    'parser_version': txn.get('parser_version', 'hdfc_v1'),
                    'text_source': txn.get('text_source', 'snippet'),
                    'signals': psycopg2.extras.Json(txn.get('signals', []))
                })

            # Execute batch insert
            execute_batch(cur, query, prepared_data, page_size=100)
            inserted_count = cur.rowcount
            conn.commit()

            log.info(f"Inserted/Updated {inserted_count} transactions into database.")

    except Exception as e:
        log.error(f"Failed to insert transactions: {e}")
        conn.rollback()
        raise
    finally:
        conn.close()

    return inserted_count


def get_unprocessed_email_ids() -> List[str]:
    """
    Returns list of raw_email IDs that haven't been converted to transactions yet.
    Only considers emails from HDFC Bank.

    Returns:
        List of gmail_message_id strings
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            query = """
                SELECT gmail_message_id
                FROM raw_emails
                WHERE gmail_message_id NOT IN (
                    SELECT raw_email_id FROM transactions
                )
                AND (bank = 'hdfc' OR bank IS NULL)
                ORDER BY received_at ASC;
            """
            cur.execute(query)
            rows = cur.fetchall()
            return [row[0] for row in rows]
    except Exception as e:
        log.error(f"Failed to get unprocessed emails: {e}")
        return []
    finally:
        conn.close()


def get_raw_emails_by_ids(email_ids: List[str]) -> List[Dict[str, Any]]:
    """
    Retrieves raw email data for given message IDs.

    Args:
        email_ids: List of gmail_message_id strings

    Returns:
        List of email dicts with id, snippet, from, subject, etc.
    """
    if not email_ids:
        return []

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = """
                SELECT gmail_message_id as id, snippet, sender as "from",
                       subject, raw_body, received_at, bank
                FROM raw_emails
                WHERE gmail_message_id = ANY(%s)
                ORDER BY received_at ASC;
            """
            cur.execute(query, (email_ids,))
            rows = cur.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        log.error(f"Failed to fetch raw emails: {e}")
        return []
    finally:
        conn.close()


def get_transactions_by_date_range(start_date: date, end_date: date,
                                   status: str = 'success') -> List[Dict[str, Any]]:
    """
    Retrieves transactions within a date range.

    Args:
        start_date: Start date (inclusive)
        end_date: End date (inclusive)
        status: Filter by status (default: 'success')

    Returns:
        List of transaction dicts
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = """
                SELECT * FROM transactions
                WHERE date >= %s AND date <= %s
                AND status = %s
                ORDER BY date DESC, created_at DESC;
            """
            cur.execute(query, (start_date, end_date, status))
            rows = cur.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        log.error(f"Failed to fetch transactions: {e}")
        return []
    finally:
        conn.close()


def get_transaction_stats() -> Dict[str, Any]:
    """
    Returns summary statistics about transactions.

    Returns:
        Dict with counts, totals, date ranges, etc.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = """
                SELECT
                    COUNT(*) as total_transactions,
                    COUNT(DISTINCT raw_email_id) as total_emails_processed,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END) as success_count,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) as failed_count,
                    SUM(CASE WHEN direction = 'out' AND status = 'success' THEN amount ELSE 0 END) as total_spent,
                    SUM(CASE WHEN direction = 'in' AND status = 'success' THEN amount ELSE 0 END) as total_received,
                    MIN(date) as earliest_transaction,
                    MAX(date) as latest_transaction,
                    COUNT(DISTINCT source) as distinct_sources,
                    COUNT(DISTINCT merchant) as distinct_merchants
                FROM transactions;
            """
            cur.execute(query)
            row = cur.fetchone()
            return dict(row) if row else {}
    except Exception as e:
        log.error(f"Failed to get transaction stats: {e}")
        return {}
    finally:
        conn.close()


def update_transaction_category(transaction_id: str, category: str,
                                confidence: float = 1.0,
                                classification_source: str = 'manual',
                                notes: Optional[str] = None) -> bool:
    """
    Updates category for a specific transaction.

    Args:
        transaction_id: Unique transaction ID
        category: Category name
        confidence: Classification confidence (1.0 for rules/manual, 0.0-1.0 for LLM)
        classification_source: How category was assigned ('rule', 'llm', 'manual')
        notes: Optional notes (e.g., LLM rationale)

    Returns:
        True if updated, False otherwise
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            query = """
                UPDATE transactions
                SET category = %s,
                    classification_source = %s,
                    classification_confidence = %s,
                    notes = COALESCE(%s, notes),
                    updated_at = NOW()
                WHERE transaction_id = %s;
            """
            cur.execute(query, (category, classification_source, confidence,
                                notes, transaction_id))
            conn.commit()
            updated = cur.rowcount > 0

            if updated:
                log.info(f"Updated category for {transaction_id}: '{category}' "
                         f"(source={classification_source}, confidence={confidence})")
            else:
                log.warning(f"Transaction {transaction_id} not found")

            return updated
    except Exception as e:
        log.error(f"Failed to update transaction category: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def update_transaction_category_by_id(
    row_id: int,
    category: str,
    classification_source: str = 'manual',
    confidence: float = 1.0,
) -> Optional[Dict[str, Any]]:
    """
    Updates category for a transaction by its integer PK.
    Returns the updated row fields needed for re-rendering, or None on failure.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                UPDATE transactions
                SET category = %s,
                    classification_source = %s,
                    classification_confidence = %s,
                    updated_at = NOW()
                WHERE id = %s
                RETURNING id, date, amount, currency, direction, merchant, category
            """, (category, classification_source, confidence, row_id))
            conn.commit()
            row = cur.fetchone()
            if row:
                log.info(f"Updated category for id={row_id}: '{category}' (source={classification_source})")
                return dict(row)
            log.warning(f"Transaction id={row_id} not found")
            return None
    except Exception as e:
        log.error(f"Failed to update transaction category by id: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()


def get_uncategorized_transactions(limit: int = 100) -> List[Dict[str, Any]]:
    """
    Returns transactions that don't have a category assigned.

    Args:
        limit: Max number of transactions to return

    Returns:
        List of transaction dicts
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = """
                SELECT * FROM transactions
                WHERE category IS NULL
                AND status = 'success'
                ORDER BY date DESC
                LIMIT %s;
            """
            cur.execute(query, (limit,))
            rows = cur.fetchall()
            return [dict(row) for row in rows]
    except Exception as e:
        log.error(f"Failed to fetch uncategorized transactions: {e}")
        return []
    finally:
        conn.close()


if __name__ == "__main__":
    # Quick test
    logging.basicConfig(level=logging.INFO)

    # Test connection
    try:
        conn = get_connection()
        print("✅ Database connection successful")
        conn.close()

        # Get stats
        stats = get_transaction_stats()
        print(f"\n📊 Transaction Stats:")
        for key, value in stats.items():
            print(f"  {key}: {value}")

        # Get unprocessed count
        unprocessed = get_unprocessed_email_ids()
        print(f"\n📧 Unprocessed emails: {len(unprocessed)}")

    except Exception as e:
        print(f"❌ Database error: {e}")
