"""Atomic admission of parsed notifications into the ledger or review queue."""

import json
from contextlib import nullcontext
from decimal import Decimal

from psycopg2.extras import Json, RealDictCursor

from app.db_connection import get_connection
from app.db_transactions import insert_transactions
from app.db_pipeline import record_outcome
from app.transaction_event import (
    PARSER_VERSION,
    generate_transaction_id,
    validate_event,
)


def _json(value):
    return Json(value, dumps=lambda obj: json.dumps(obj, default=str))


def _same_event(row, event):
    return (
        row["direction"] == event["direction"]
        and row["status"] == event["status"]
        and row["currency"] == event["currency"]
        and str(row["date"]) == event["date"]
        and row["amount"] == Decimal(str(event["amount"]))
        and row["source"] == event["source"]
        and row["event_kind"] in (None, event["event_kind"])
        and row["reference_id"] == event["reference_id"]
        and row["account_last4"] in (None, event["account_last4"])
    )


def store_extraction(event, *, connection=None):
    """Persist one notification atomically; return inserted, duplicate, or review.

    A transaction-level lock serializes admission, including duplicate-candidate
    checks. No existing financial amounts are changed by reprocessing here.
    """
    event = dict(event)
    reasons = list(
        dict.fromkeys(event.get("review_reasons", []) + validate_event(event))
    )
    conn = connection if connection is not None else get_connection()
    try:
        with (
            conn if connection is None else nullcontext(),
            conn.cursor(cursor_factory=RealDictCursor) as cur,
        ):
            cur.execute("SELECT pg_advisory_xact_lock(73501921)")
            existing = None
            if not reasons:
                # Retain legacy transaction IDs on replay of the same email.
                cur.execute(
                    """SELECT t.* FROM transactions t WHERE t.raw_email_id = %s
                    OR t.transaction_id IN (SELECT transaction_id FROM transaction_notifications
                                           WHERE raw_email_id = %s)""",
                    (event["raw_email_id"], event["raw_email_id"]),
                )
                rows = cur.fetchall()
                if rows:
                    if len(rows) == 1 and _same_event(rows[0], event):
                        existing = rows[0]
                    else:
                        reasons.append(
                            "Reprocessing differs from the stored event; historical review required"
                        )
                event["transaction_id"] = (
                    existing["transaction_id"]
                    if existing
                    else generate_transaction_id(event)
                )
            if not reasons and not existing:
                cur.execute(
                    "SELECT * FROM transactions WHERE transaction_id = %s",
                    (event["transaction_id"],),
                )
                existing = cur.fetchone()
                if existing and not _same_event(existing, event):
                    reasons.append("Bank reference conflicts with an existing event")
            if not reasons and not existing:
                # Similarity is evidence for review, never permission to merge.
                cur.execute(
                    """SELECT transaction_id FROM transactions
                    WHERE source = %s AND direction = %s AND status = 'success'
                      AND (account_last4 = %s OR account_last4 IS NULL OR %s IS NULL)
                      AND ((reference_id = %s AND %s IS NOT NULL)
                           OR (date = %s AND amount = %s AND currency = %s
                               AND LOWER(merchant) IS NOT DISTINCT FROM LOWER(%s)
                               AND (reference_id IS NULL OR %s IS NULL)))
                    LIMIT 1""",
                    (
                        event["source"],
                        event["direction"],
                        event["account_last4"],
                        event["account_last4"],
                        event["reference_id"],
                        event["reference_id"],
                        event["date"],
                        event["amount"],
                        event["currency"],
                        event["merchant"],
                        event["reference_id"],
                    ),
                )
                if cur.fetchone():
                    reasons.append(
                        "Possible duplicate notification; confirm whether this is a separate purchase or credit"
                    )
            if reasons:
                rejected = bool(event.get("exclusion_reason"))
                if rejected:
                    cur.execute(
                        """SELECT 1 FROM transactions WHERE raw_email_id=%s
                        UNION ALL SELECT 1 FROM transaction_notifications WHERE raw_email_id=%s""",
                        (event["raw_email_id"], event["raw_email_id"]),
                    )
                    if cur.fetchone():
                        rejected = False
                        reasons.append(
                            "Excluded notification has a stored event; historical review required"
                        )
                cur.execute(
                    """INSERT INTO extraction_reviews(raw_email_id, parser_version, reasons, candidate)
                    VALUES (%s, %s, %s, %s) ON CONFLICT (raw_email_id) DO UPDATE SET
                    parser_version=EXCLUDED.parser_version, reasons=EXCLUDED.reasons,
                    candidate=EXCLUDED.candidate, updated_at=NOW()""",
                    (
                        event["raw_email_id"],
                        PARSER_VERSION,
                        _json(reasons),
                        _json(event),
                    ),
                )
                record_outcome(conn, event, "rejected" if rejected else "review")
                return "rejected" if rejected else "review"
            if not existing:
                if (
                    event["direction"] == "in"
                    and event["event_kind"] in ("refund", "reversal")
                    and event["reference_id"]
                    and event["account_last4"]
                ):
                    cur.execute(
                        """SELECT transaction_id FROM transactions WHERE source=%s
                        AND account_last4=%s AND reference_id=%s AND direction='out'
                        AND status='success' AND currency=%s AND amount >= %s AND date <= %s""",
                        (
                            event["source"],
                            event["account_last4"],
                            event["reference_id"],
                            event["currency"],
                            event["amount"],
                            event["date"],
                        ),
                    )
                    originals = cur.fetchall()
                    if len(originals) == 1:
                        event["original_transaction_id"] = originals[0][
                            "transaction_id"
                        ]
                insert_transactions([event], connection=conn)
            else:
                # Enrich legacy provenance only after exact financial matching;
                # original amounts, IDs, categories and confidence stay intact.
                cur.execute(
                    """UPDATE transactions SET
                    account_last4=COALESCE(account_last4, %s),
                    event_kind=COALESCE(event_kind, %s), parser_version=%s,
                    text_source=%s, signals=%s WHERE transaction_id=%s""",
                    (
                        event["account_last4"],
                        event["event_kind"],
                        PARSER_VERSION,
                        event["text_source"],
                        _json(event["signals"]),
                        event["transaction_id"],
                    ),
                )
            # Link earlier-arriving reversals when their original arrives.
            if (
                event["direction"] == "out"
                and event["account_last4"]
                and event["reference_id"]
            ):
                cur.execute(
                    """UPDATE transactions SET original_transaction_id=%s
                        WHERE source=%s AND account_last4=%s AND reference_id=%s
                          AND direction='in' AND event_kind IN ('refund', 'reversal')
                          AND currency=%s AND amount <= %s AND date >= %s
                          AND original_transaction_id IS NULL""",
                    (
                        event["transaction_id"],
                        event["source"],
                        event["account_last4"],
                        event["reference_id"],
                        event["currency"],
                        event["amount"],
                        event["date"],
                    ),
                )
            cur.execute(
                """INSERT INTO transaction_notifications(raw_email_id, transaction_id, parser_version)
                VALUES (%s, %s, %s) ON CONFLICT (raw_email_id) DO UPDATE SET
                parser_version=EXCLUDED.parser_version""",
                (event["raw_email_id"], event["transaction_id"], PARSER_VERSION),
            )
            cur.execute(
                "DELETE FROM extraction_reviews WHERE raw_email_id=%s",
                (event["raw_email_id"],),
            )
            record_outcome(conn, event, "duplicate" if existing else "accepted")
            return "duplicate" if existing else "inserted"
    finally:
        if connection is None:
            conn.close()


def get_reviews(limit=50, offset=0, raw_email_id=None):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            visible = """NOT EXISTS (SELECT 1 FROM email_processing_outcomes o
                WHERE o.raw_email_id=v.raw_email_id AND o.outcome='rejected'
                AND o.parser_version=v.parser_version)"""
            cur.execute(
                "SELECT COUNT(*) AS count FROM extraction_reviews v WHERE " + visible
            )
            count = cur.fetchone()["count"]
            only = " AND v.raw_email_id=%s" if raw_email_id else ""
            cur.execute(
                """SELECT raw_email_id, reasons, candidate, updated_at
                FROM extraction_reviews v WHERE """
                + visible
                + only
                + """ ORDER BY updated_at DESC, raw_email_id
                LIMIT %s OFFSET %s""",
                ((raw_email_id,) if raw_email_id else ()) + (limit, offset),
            )
            return {"count": count, "reviews": [dict(row) for row in cur.fetchall()]}
    finally:
        conn.close()
