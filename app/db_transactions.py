"""Database operations for the transactions table."""

import logging
from datetime import date
from typing import Any, Dict, List, Optional

import psycopg2.extras
from psycopg2.extras import execute_batch, RealDictCursor

from app.db_connection import get_connection

log = logging.getLogger(__name__)


def insert_transactions(transactions: List[Dict[str, Any]], *, connection=None) -> int:
    """Insert events or refresh event fields, preserving classifications.

    Classification changes belong to the category-update operations, not replay.
    Return the number of committed input records (inserts and updates combined),
    including repeated IDs; this is not a count of newly created database rows.
    With an explicit connection, the caller owns commit/rollback and closure.
    """
    if not transactions:
        return 0

    from app.transaction_event import validate_event

    for transaction in transactions:
        if transaction.get("status") == "success":
            reasons = transaction.get('review_reasons', []) + validate_event(transaction)
            if reasons:
                raise ValueError("Invalid financial event: " + "; ".join(reasons))

    conn = connection or get_connection()
    try:
        with conn.cursor() as cur:
            query = """
                INSERT INTO transactions (
                    transaction_id, raw_email_id, date, amount, currency,
                    direction, source, status, merchant, reference_id,
                    category, notes, parser_version, text_source, signals,
                    classification_source, classification_confidence,
                    account_last4, event_kind, original_transaction_id
                ) VALUES (
                    %(transaction_id)s, %(raw_email_id)s, %(date)s, %(amount)s, %(currency)s,
                    %(direction)s, %(source)s, %(status)s, %(merchant)s, %(reference_id)s,
                    %(category)s, %(notes)s, %(parser_version)s, %(text_source)s, %(signals)s,
                    %(classification_source)s, %(classification_confidence)s,
                    %(account_last4)s, %(event_kind)s, %(original_transaction_id)s
                )
                ON CONFLICT (transaction_id) DO UPDATE SET
                    updated_at = NOW(),
                    amount     = EXCLUDED.amount,
                    date       = EXCLUDED.date,
                    merchant   = EXCLUDED.merchant,
                    currency   = EXCLUDED.currency,
                    direction  = EXCLUDED.direction,
                    source     = EXCLUDED.source,
                    status     = EXCLUDED.status,
                    reference_id = EXCLUDED.reference_id,
                    account_last4 = EXCLUDED.account_last4,
                    event_kind = EXCLUDED.event_kind,
                    original_transaction_id = COALESCE(EXCLUDED.original_transaction_id, transactions.original_transaction_id),
                    parser_version = EXCLUDED.parser_version,
                    text_source = EXCLUDED.text_source,
                    signals = EXCLUDED.signals
            """
            prepared = [
                {
                    "transaction_id":  t["transaction_id"],
                    "raw_email_id":    t["raw_email_id"],
                    "date":            t.get("date"),
                    "amount":          t.get("amount"),
                    "currency":        t.get("currency", "INR"),
                    "direction":       t.get("direction", "unknown"),
                    "source":          t.get("source", "unknown"),
                    "status":          t.get("status", "unknown"),
                    "merchant":        t.get("merchant"),
                    "reference_id":    t.get("reference_id"),
                    "category":        t.get("category"),
                    "classification_source": t.get("classification_source") if t.get("category") else None,
                    "classification_confidence": t.get("classification_confidence") if t.get("category") else None,
                    "account_last4": t.get("account_last4"),
                    "event_kind": t.get("event_kind") or ("debit" if t.get("direction") == "out" else "credit"),
                    "original_transaction_id": t.get("original_transaction_id"),
                    "notes":           t.get("notes"),
                    "parser_version":  t.get("parser_version", "hdfc_v1"),
                    "text_source":     t.get("text_source", "snippet"),
                    "signals":         psycopg2.extras.Json(t.get("signals", [])),
                }
                for t in transactions
            ]
            execute_batch(cur, query, prepared, page_size=100)
            if connection is None:
                conn.commit()
            count = len(prepared)
            log.info(f"Inserted/updated {count} transactions.")
            return count
    except Exception as e:
        log.error(f"Failed to insert transactions: {e}")
        if connection is None:
            conn.rollback()
        raise
    finally:
        if connection is None:
            conn.close()


def get_unprocessed_email_ids() -> List[str]:
    """Returns IDs of raw emails that have not yet been converted to transactions."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            from app.transaction_event import PARSER_VERSION
            cur.execute("""
                SELECT gmail_message_id
                FROM raw_emails
                WHERE gmail_message_id NOT IN (SELECT raw_email_id FROM transactions)
                  AND gmail_message_id NOT IN (SELECT raw_email_id FROM transaction_notifications)
                  AND gmail_message_id NOT IN (
                      SELECT raw_email_id FROM extraction_reviews v WHERE parser_version = %s
                        AND NOT EXISTS (SELECT 1 FROM email_processing_outcomes o
                                        WHERE o.raw_email_id=v.raw_email_id AND o.outcome='failed')
                  )
                  AND (bank = 'hdfc' OR bank IS NULL)
                ORDER BY received_at ASC
            """, (PARSER_VERSION,))
            return [row[0] for row in cur.fetchall()]
    except Exception as e:
        log.error(f"Failed to get unprocessed email IDs: {e}")
        raise
    finally:
        conn.close()


def get_raw_emails_by_ids(email_ids: List[str]) -> List[Dict[str, Any]]:
    """Fetches raw email rows for the given list of gmail message IDs."""
    if not email_ids:
        return []

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT gmail_message_id AS id, snippet, sender AS "from",
                          subject, raw_body, received_at, bank, account_last4
                   FROM raw_emails
                   WHERE gmail_message_id = ANY(%s)
                   ORDER BY received_at ASC""",
                (email_ids,),
            )
            return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        log.error(f"Failed to fetch raw emails by IDs: {e}")
        raise
    finally:
        conn.close()


def get_transactions_by_date_range(
    start_date: date, end_date: date, status: str = "success"
) -> List[Dict[str, Any]]:
    """Returns all transactions within the given date range, filtered by status."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM transactions
                   WHERE date >= %s AND date <= %s AND status = %s
                   ORDER BY date DESC, created_at DESC""",
                (start_date, end_date, status),
            )
            return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        log.error(f"Failed to fetch transactions by date range: {e}")
        return []
    finally:
        conn.close()


def get_transaction_stats() -> Dict[str, Any]:
    """Returns aggregate statistics across all transactions: counts, totals, date range, unique merchants."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    COUNT(*)                                                            AS total_transactions,
                    COUNT(DISTINCT raw_email_id)                                        AS total_emails_processed,
                    SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END)                AS success_count,
                    SUM(CASE WHEN status = 'failed'  THEN 1 ELSE 0 END)                AS failed_count,
                    SUM(CASE WHEN direction = 'out' AND status = 'success'
                             THEN amount ELSE 0 END)                                   AS total_spent,
                    SUM(CASE WHEN direction = 'in'  AND status = 'success'
                             THEN amount ELSE 0 END)                                   AS total_received,
                    MIN(date)                                                           AS earliest_transaction,
                    MAX(date)                                                           AS latest_transaction,
                    COUNT(DISTINCT source)                                              AS distinct_sources,
                    COUNT(DISTINCT merchant)                                            AS distinct_merchants
                FROM transactions
            """)
            row = cur.fetchone()
            return dict(row) if row else {}
    except Exception as e:
        log.error(f"Failed to get transaction stats: {e}")
        return {}
    finally:
        conn.close()


def update_transaction_merchant(
    row_id: int,
    merchant: str,
    reference_id: Optional[str] = None,
) -> bool:
    """Updates the merchant/reference_id of a transaction by its integer primary key.

    Used for targeted backfills (e.g. re-parsing emails after a bank template
    change) — updates only these two fields on an existing row rather than
    regenerating transaction_id, so it can't create a duplicate row.
    """
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE transactions
                   SET merchant = %s,
                       reference_id = COALESCE(%s, reference_id),
                       updated_at = NOW()
                   WHERE id = %s""",
                (merchant, reference_id, row_id),
            )
            conn.commit()
            updated = cur.rowcount > 0
            if not updated:
                log.warning(f"Transaction id={row_id} not found")
            return updated
    except Exception as e:
        log.error(f"Failed to update merchant for id={row_id}: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def update_transaction_category(
    transaction_id: str,
    category: str,
    confidence: float = 1.0,
    classification_source: str = "manual",
    notes: Optional[str] = None,
) -> bool:
    """Updates the category of a transaction identified by its transaction_id string."""
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE transactions
                   SET category = %s,
                       classification_source = %s,
                       classification_confidence = %s,
                       notes = COALESCE(%s, notes),
                       updated_at = NOW()
                   WHERE transaction_id = %s
                     AND (%s = 'manual' OR classification_source IS DISTINCT FROM 'manual')""",
                (category, classification_source, confidence, notes, transaction_id, classification_source),
            )
            conn.commit()
            updated = cur.rowcount > 0
            if updated:
                log.info(f"Updated category for {transaction_id}: '{category}' ({classification_source})")
            else:
                log.warning(f"Transaction {transaction_id} not found")
            return updated
    except Exception as e:
        log.error(f"Failed to update category: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def update_transaction_category_by_id(
    row_id: int,
    category: str,
    classification_source: str = "manual",
    confidence: float = 1.0,
) -> Optional[Dict[str, Any]]:
    """Updates the category of a transaction by its integer primary key and returns the updated row."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """UPDATE transactions
                   SET category = %s,
                       classification_source = %s,
                       classification_confidence = %s,
                       updated_at = NOW()
                   WHERE id = %s
                   RETURNING id, date, amount, currency, direction, merchant, category""",
                (category, classification_source, confidence, row_id),
            )
            conn.commit()
            row = cur.fetchone()
            if row:
                log.info(f"Updated category for id={row_id}: '{category}' ({classification_source})")
                return dict(row)
            log.warning(f"Transaction id={row_id} not found")
            return None
    except Exception as e:
        log.error(f"Failed to update category by id: {e}")
        conn.rollback()
        return None
    finally:
        conn.close()


def get_uncategorized_transactions(limit: int = 100) -> List[Dict[str, Any]]:
    """Returns successful transactions that have no category assigned yet."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM transactions
                   WHERE status = 'success' AND (category IS NULL
                       OR (classification_confidence = 0 AND classification_source IS DISTINCT FROM 'manual'))
                   ORDER BY date DESC
                   LIMIT %s""",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        log.error(f"Failed to fetch uncategorized transactions: {e}")
        raise
    finally:
        conn.close()


def get_transactions_with_blank_merchant(source: Optional[str] = None) -> List[Dict[str, Any]]:
    """Returns successful transactions with no merchant captured, optionally filtered by source.

    Used to find transactions affected by a parser regression (e.g. a bank
    template change breaking merchant extraction) so they can be backfilled.
    """
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            query = """SELECT * FROM transactions
                       WHERE status = 'success'
                         AND (merchant IS NULL OR merchant = '')"""
            params: list = []
            if source:
                query += " AND source = %s"
                params.append(source)
            query += " ORDER BY date ASC"
            cur.execute(query, params)
            return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        log.error(f"Failed to fetch transactions with blank merchant: {e}")
        return []
    finally:
        conn.close()


def get_all_success_transactions(limit: int = 1000) -> List[Dict[str, Any]]:
    """Returns all successful transactions regardless of category, for reclassification."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT * FROM transactions
                   WHERE status = 'success'
                   ORDER BY date DESC
                   LIMIT %s""",
                (limit,),
            )
            return [dict(row) for row in cur.fetchall()]
    except Exception as e:
        log.error(f"Failed to fetch all success transactions: {e}")
        raise
    finally:
        conn.close()
