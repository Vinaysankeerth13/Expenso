"""Atomic manual classification edits and guarded undo, including optional rules."""

import json
from psycopg2.extras import Json, RealDictCursor
from app.db_connection import get_connection
from app.config import CATEGORIES

FIELDS = ("category", "classification_source", "classification_confidence", "notes")
RULE_FIELDS = (
    "merchant_pattern",
    "category",
    "subcategory",
    "confidence",
    "source",
    "match_mode",
)


def values(row, fields=FIELDS):
    return {key: row[key] for key in fields} if row else None


def json_value(value):
    return Json(value, dumps=lambda obj: json.dumps(obj, default=str))


def edit_category(row_id, category, *, learn_merchant=False):
    if category not in CATEGORIES:
        raise ValueError("Unknown category")
    conn = get_connection()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            # All user rule edits/undo use a common lock, including new patterns.
            cur.execute("SELECT pg_advisory_xact_lock(73501922)")
            cur.execute("SELECT * FROM transactions WHERE id=%s FOR UPDATE", (row_id,))
            old = cur.fetchone()
            if not old:
                raise ValueError("Transaction not found")
            before = values(old)
            pattern, rule_before, rule_after = None, None, None
            if learn_merchant:
                from app.classify_transactions import normalize_merchant

                pattern = normalize_merchant(old["merchant"] or "")
                if not pattern or old["direction"] != "out":
                    raise ValueError(
                        "A merchant rule requires an outgoing payment with a merchant"
                    )
                cur.execute(
                    "SELECT * FROM merchant_categories WHERE merchant_pattern=%s FOR UPDATE",
                    (pattern,),
                )
                rule_before = values(cur.fetchone(), RULE_FIELDS)
                cur.execute(
                    """INSERT INTO merchant_categories(merchant_pattern, category, confidence, source, match_mode)
                    VALUES (%s,%s,1,'manual','exact') ON CONFLICT(merchant_pattern) DO UPDATE SET
                    category=EXCLUDED.category, confidence=1, source='manual', match_mode='exact', updated_at=NOW()
                    RETURNING *""",
                    (pattern, category),
                )
                rule_after = values(cur.fetchone(), RULE_FIELDS)
            cur.execute(
                """UPDATE transactions SET category=%s, classification_source='manual',
                classification_confidence=1, updated_at=NOW() WHERE id=%s RETURNING *""",
                (category, row_id),
            )
            row = dict(cur.fetchone())
            cur.execute(
                """INSERT INTO classification_edits(transaction_id, before_state, after_state,
                rule_pattern, rule_before, rule_after) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                (
                    row["transaction_id"],
                    json_value(before),
                    json_value(values(row)),
                    pattern,
                    json_value(rule_before),
                    json_value(rule_after),
                ),
            )
            row["edit_id"] = cur.fetchone()["id"]
            row["batch_count"] = (
                _apply_to_other_payments(cur, pattern, category, row_id, row["edit_id"])
                if learn_merchant
                else 0
            )
            return row
    finally:
        conn.close()


def _automated_outgoing(cur, exclude_id=None, lock=False):
    cur.execute(
        """SELECT * FROM transactions WHERE direction='out' AND status='success'
        AND merchant IS NOT NULL AND classification_source IS DISTINCT FROM 'manual'
        AND id IS DISTINCT FROM %s""" + (" FOR UPDATE" if lock else ""),
        (exclude_id,),
    )
    return cur.fetchall()


def _apply_to_other_payments(cur, pattern, category, row_id, edit_id):
    """Confirm the merchant's other automated payments; manual choices are never touched."""
    from app.classify_transactions import normalize_merchant

    count = 0
    for old in _automated_outgoing(cur, row_id, lock=True):
        if normalize_merchant(old["merchant"]) != pattern:
            continue
        cur.execute(
            """UPDATE transactions SET category=%s, classification_source='manual',
            classification_confidence=1, updated_at=NOW() WHERE id=%s RETURNING *""",
            (category, old["id"]),
        )
        new = cur.fetchone()
        cur.execute(
            """INSERT INTO classification_edits(transaction_id, before_state, after_state, batch_id)
            VALUES (%s,%s,%s,%s)""",
            (new["transaction_id"], json_value(values(old)), json_value(values(new)), edit_id),
        )
        count += 1
    return count


def other_payment_counts(rows):
    """Per row: how many other payments 'Use for all payments to this merchant' would update."""
    from app.classify_transactions import normalize_merchant

    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            counts = {}
            for row in _automated_outgoing(cur):
                key = normalize_merchant(row["merchant"])
                counts[key] = counts.get(key, 0) + 1
    finally:
        conn.close()
    return [
        counts.get(normalize_merchant(row.get("merchant") or ""), 0)
        - (row.get("direction") == "out" and row.get("classification_source") != "manual"
           and bool(row.get("merchant")))
        for row in rows
    ]


def undo_category(edit_id):
    conn = get_connection()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT pg_advisory_xact_lock(73501922)")
            cur.execute(
                "SELECT * FROM classification_edits WHERE id=%s FOR UPDATE", (edit_id,)
            )
            edit = cur.fetchone()
            if not edit or edit["undone_at"]:
                raise ValueError("This edit is no longer available to undo")
            cur.execute(
                "SELECT * FROM transactions WHERE transaction_id=%s FOR UPDATE",
                (edit["transaction_id"],),
            )
            row = cur.fetchone()
            cur.execute(
                """SELECT 1 FROM classification_edits WHERE transaction_id=%s
                AND id>%s AND undone_at IS NULL LIMIT 1""",
                (edit["transaction_id"], edit_id),
            )
            if cur.fetchone() or values(row) != edit["after_state"]:
                raise ValueError("The transaction has changed since this edit")
            if edit["rule_pattern"]:
                cur.execute(
                    "SELECT * FROM merchant_categories WHERE merchant_pattern=%s FOR UPDATE",
                    (edit["rule_pattern"],),
                )
                current = values(cur.fetchone(), RULE_FIELDS)
                cur.execute(
                    """SELECT 1 FROM classification_edits WHERE rule_pattern=%s
                    AND id>%s AND undone_at IS NULL LIMIT 1""",
                    (edit["rule_pattern"], edit_id),
                )
                if cur.fetchone() or current != edit["rule_after"]:
                    raise ValueError("The merchant rule has changed since this edit")
                old = edit["rule_before"]
                if old is None:
                    cur.execute(
                        "DELETE FROM merchant_categories WHERE merchant_pattern=%s",
                        (edit["rule_pattern"],),
                    )
                else:
                    cur.execute(
                        """UPDATE merchant_categories SET category=%(category)s, subcategory=%(subcategory)s,
                        confidence=%(confidence)s, source=%(source)s, match_mode=%(match_mode)s, updated_at=NOW()
                        WHERE merchant_pattern=%(merchant_pattern)s""",
                        old,
                    )
            row = _restore(cur, edit)
            row["batch_reverted"] = _undo_batch(cur, edit_id)
            return row
    finally:
        conn.close()


def _restore(cur, edit):
    old = edit["before_state"]
    cur.execute(
        """UPDATE transactions SET category=%s, classification_source=%s,
        classification_confidence=%s, notes=%s, updated_at=NOW() WHERE transaction_id=%s RETURNING *""",
        (
            old["category"],
            old["classification_source"],
            old["classification_confidence"],
            old["notes"],
            edit["transaction_id"],
        ),
    )
    row = dict(cur.fetchone())
    cur.execute("UPDATE classification_edits SET undone_at=NOW() WHERE id=%s", (edit["id"],))
    return row


def _undo_batch(cur, edit_id):
    """Revert companion edits, skipping any payment that has been changed since."""
    cur.execute(
        "SELECT * FROM classification_edits WHERE batch_id=%s AND undone_at IS NULL ORDER BY id FOR UPDATE",
        (edit_id,),
    )
    reverted = 0
    for edit in cur.fetchall():
        cur.execute(
            "SELECT * FROM transactions WHERE transaction_id=%s FOR UPDATE",
            (edit["transaction_id"],),
        )
        current = cur.fetchone()
        cur.execute(
            """SELECT 1 FROM classification_edits WHERE transaction_id=%s
            AND id>%s AND undone_at IS NULL LIMIT 1""",
            (edit["transaction_id"], edit["id"]),
        )
        if cur.fetchone() or values(current) != edit["after_state"]:
            continue
        _restore(cur, edit)
        reverted += 1
    return reverted
