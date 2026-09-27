"""Apply an explicitly reviewed manifest with snapshot guards and durable receipts.

This is deliberately separate from unattended extraction. Preview is read-only;
apply holds admission and table locks and commits the entire batch atomically.
"""

from collections import Counter
from datetime import date
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from psycopg2.extras import RealDictCursor

from app.db_connection import get_connection
from app.db_extraction import store_extraction, _json
from app.db_pipeline import record_outcome
from app.historical_repair import totals
from app.transaction_event import PARSER_VERSION, extract_transaction


def canonical(value):
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def snapshot(cur):
    result = {}
    for table, order in (
        ("raw_emails", "gmail_message_id"),
        ("transactions", "transaction_id"),
        ("transaction_notifications", "raw_email_id"),
        ("extraction_reviews", "raw_email_id"),
        ("email_processing_outcomes", "raw_email_id"),
    ):
        cur.execute(f"SELECT * FROM {table} ORDER BY {order}")
        result[table] = [dict(row) for row in cur.fetchall()]
    return result


def message_state(state, raw_id):
    linked = {
        n["transaction_id"]
        for n in state["transaction_notifications"]
        if n["raw_email_id"] == raw_id
    }
    return {
        "raw": next(
            (r for r in state["raw_emails"] if r["gmail_message_id"] == raw_id), None
        ),
        "transactions": [
            r
            for r in state["transactions"]
            if r["raw_email_id"] == raw_id or r["transaction_id"] in linked
        ],
        **{
            table: [r for r in state[table] if r["raw_email_id"] == raw_id]
            for table in (
                "transaction_notifications",
                "extraction_reviews",
                "email_processing_outcomes",
            )
        },
    }


def prepare(manifest, state):
    items = manifest["items"]
    if not items or len({i["raw"]["gmail_message_id"] for i in items}) != len(items):
        raise ValueError("Manifest must contain unique reviewed messages")
    plan = []
    for item in items:
        raw_id = item["raw"]["gmail_message_id"]
        before = message_state(state, raw_id)
        if canonical(before["raw"]) != canonical(item["raw"]) or canonical(
            before["transactions"]
        ) != canonical(item["transactions"]):
            raise ValueError("Reviewed source or transaction has changed")
        event = extract_transaction(dict(before["raw"], id=raw_id))
        action = item["action"]
        if not item.get("reason"):
            raise ValueError("A reviewed reason is required")
        if action in ("confirm", "insert"):
            if event["review_reasons"]:
                raise ValueError("Reviewed movement still requires parser review")
            expected = item["expected_event"]
            for key in ("date", "direction", "currency", "status"):
                if str(event[key]) != str(expected[key]):
                    raise ValueError("Parsed movement differs from reviewed evidence")
            if Decimal(str(event["amount"])) != Decimal(str(expected["amount"])):
                raise ValueError("Parsed amount differs from reviewed evidence")
        if action == "confirm":
            if len(before["transactions"]) != 1:
                raise ValueError("Confirmation requires one existing transaction")
            old = before["transactions"][0]
            if (
                any(
                    str(old[k]) != str(event[k])
                for k in ("date", "direction", "currency", "status")
                )
                or Decimal(str(old["amount"])) != event["amount"]
            ):
                raise ValueError("Confirmation cannot change an existing movement")
        elif action == "insert":
            if before["transactions"]:
                raise ValueError("Missing movement is already stored")
            # Deliberately broader than normal admission: any successful debit
            # with the same amount within three days requires another review.
            for row in state["transactions"]:
                if (
                    row["status"] == "success"
                    and row["direction"] == event["direction"]
                    and row["currency"] == event["currency"]
                    and row.get("date")
                    and Decimal(str(row["amount"])) == event["amount"]
                    and abs(
                        (
                            date.fromisoformat(str(row["date"]))
                            - date.fromisoformat(event["date"])
                        ).days
                    )
                    <= 3
                ):
                    raise ValueError(
                        "Nearby amount/date duplicate candidate requires review"
                    )
        elif action == "exclude":
            if before["transactions"] or not event.get("exclusion_reason"):
                raise ValueError(
                    "Exclusion requires a recognized non-movement without ledger rows"
                )
        elif action == "quarantine":
            if len(before["transactions"]) != 1 or not event["review_reasons"]:
                raise ValueError(
                    "Quarantine requires one unsupported reviewed transaction"
                )
        else:
            raise ValueError("Unsupported reviewed action")
        plan.append(
            {
                "raw_email_id": raw_id,
                "action": action,
                "reason": item["reason"],
                "event": event,
                "before": before,
            }
        )
    return plan


def run_repair(manifest, *, expected_digest=None):
    """Preview unless supplied the exact digest of a previously reviewed preview."""
    apply = expected_digest is not None
    conn = get_connection()
    conn.set_session(
        readonly=not apply,
        isolation_level="REPEATABLE READ" if not apply else "READ COMMITTED",
    )
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET LOCAL lock_timeout='10s'")
            cur.execute("SET LOCAL statement_timeout='30s'")
            if apply:
                cur.execute("SELECT pg_advisory_xact_lock(73501921)")
                cur.execute(
                    "LOCK TABLE raw_emails, transactions, transaction_notifications, extraction_reviews, email_processing_outcomes IN SHARE ROW EXCLUSIVE MODE"
                )
            cur.execute("SELECT to_regclass('historical_correction_audit') AS name")
            if cur.fetchone()["name"]:
                cur.execute(
                    "SELECT COUNT(*) AS count FROM historical_correction_audit WHERE batch_id=%s",
                    (manifest["batch_id"],),
                )
                if cur.fetchone()["count"]:
                    raise ValueError("This reviewed batch has already been applied")
            before = snapshot(cur)
            plan = prepare(manifest, before)
            token = digest(
                {
                    "manifest": manifest,
                    "snapshot": before,
                    "plan": plan,
                    "parser_sha256": hashlib.sha256(
                        Path(__file__).with_name("transaction_event.py").read_bytes()
                    ).hexdigest(),
                }
            )
            if apply and token != expected_digest:
                raise ValueError(
                    "Preview is stale; regenerate and review before applying"
                )
            receipt = {
                "batch_id": manifest["batch_id"],
                "digest": token,
                "applied": apply,
                "actions": dict(Counter(p["action"] for p in plan)),
                "before_totals": totals(before["transactions"]),
                "plan": plan,
            }
            if not apply:
                return receipt
            migration = (
                Path(__file__).resolve().parents[1]
                / "migrations/005_historical_correction_audit.sql"
            )
            cur.execute(migration.read_text())
            quarantined = set()
            for p in plan:
                event, raw_id = p["event"], p["raw_email_id"]
                if p["action"] in ("insert", "exclude"):
                    outcome = store_extraction(event, connection=conn)
                    if outcome != (
                        "inserted" if p["action"] == "insert" else "rejected"
                    ):
                        raise ValueError(
                            "Admission differs from the reviewed plan; rolling back batch"
                        )
                elif p["action"] == "confirm":
                    tid = p["before"]["transactions"][0]["transaction_id"]
                    cur.execute(
                        """INSERT INTO transaction_notifications(raw_email_id, transaction_id, parser_version)
                        VALUES (%s,%s,%s) ON CONFLICT (raw_email_id) DO NOTHING""",
                        (raw_id, tid, PARSER_VERSION),
                    )
                    cur.execute(
                        "DELETE FROM extraction_reviews WHERE raw_email_id=%s",
                        (raw_id,),
                    )
                    record_outcome(conn, event, "accepted")
                else:
                    tid = p["before"]["transactions"][0]["transaction_id"]
                    quarantined.add(tid)
                    cur.execute(
                        "UPDATE transactions SET status='unknown' WHERE transaction_id=%s",
                        (tid,),
                    )
                    cur.execute(
                        """INSERT INTO extraction_reviews(raw_email_id, parser_version, reasons, candidate)
                        VALUES (%s,%s,%s,%s) ON CONFLICT(raw_email_id) DO UPDATE SET
                        parser_version=EXCLUDED.parser_version, reasons=EXCLUDED.reasons, candidate=EXCLUDED.candidate, updated_at=NOW()""",
                        (raw_id, PARSER_VERSION, _json([p["reason"]]), _json(event)),
                    )
                    record_outcome(conn, event, "rejected")
            after = snapshot(cur)
            old_by_id = {r["transaction_id"]: r for r in before["transactions"]}
            new_by_id = {r["transaction_id"]: r for r in after["transactions"]}
            for tid, old in old_by_id.items():
                expected = dict(old, status="unknown") if tid in quarantined else old
                if canonical(new_by_id.get(tid)) != canonical(expected):
                    raise ValueError(
                        "Unexpected change to an existing transaction; rolling back"
                    )
            if len(new_by_id) != len(old_by_id) + receipt["actions"].get("insert", 0):
                raise ValueError("Unexpected ledger count; rolling back")
            if canonical(after["raw_emails"]) != canonical(before["raw_emails"]):
                raise ValueError("Raw evidence changed; rolling back")
            for p in plan:
                cur.execute(
                    """INSERT INTO historical_correction_audit
                    (batch_id, raw_email_id, action, reason, before_state, after_state)
                    VALUES (%s,%s,%s,%s,%s,%s)""",
                    (
                        manifest["batch_id"],
                        p["raw_email_id"],
                        p["action"],
                        p["reason"],
                        _json(p["before"]),
                        _json(message_state(after, p["raw_email_id"])),
                    ),
                )
            receipt.update(
                after_totals=totals(after["transactions"]),
                unchanged_existing=len(old_by_id) - len(quarantined),
                transaction_count=len(new_by_id),
                after_snapshot_sha256=digest(after),
            )
            return receipt
    finally:
        conn.close()
