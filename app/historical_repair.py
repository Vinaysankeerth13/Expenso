"""Read-only, reproducible historical repair proposals. Never applies changes."""

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path

from psycopg2.extras import RealDictCursor

from app.db_connection import get_connection
from app.transaction_event import (
    PARSER_VERSION,
    extract_transaction,
    generate_transaction_id,
)

FIELDS = (
    "amount",
    "date",
    "currency",
    "direction",
    "status",
    "source",
    "merchant",
    "reference_id",
    "account_last4",
    "event_kind",
)
PRESERVED = ("category", "classification_source", "classification_confidence", "notes")


def _value(value):
    return str(value) if isinstance(value, (Decimal, datetime)) else value


def _equal(a, b):
    return str(a) == str(b) or (a is None and b is None)


def _money(value):
    try:
        amount = Decimal(str(value))
        return amount if amount.is_finite() else None
    except (InvalidOperation, ValueError):
        return None


def _same_money(a, b):
    return _money(a.get("amount")) == _money(b.get("amount")) and all(
        _equal(a.get(k), b.get(k))
        for k in ("date", "currency", "direction", "status", "source")
    )


def totals(rows):
    """Per stored currency and month; unknown fields never become invented money."""
    result = defaultdict(
        lambda: {
            "out": Decimal(0),
            "in": Decimal(0),
            "count": 0,
            "missing_amount": 0,
            "unknown_direction": 0,
        }
    )
    for row in rows:
        if row.get("status") != "success":
            continue
        currency = row.get("currency") or "unknown"
        month = str(row["date"])[:7] if row.get("date") else "undated"
        amount = _money(row.get("amount"))
        for key in (f"{currency}/all", f"{currency}/{month}"):
            bucket = result[key]
            bucket["count"] += 1
            if amount is None:
                bucket["missing_amount"] += 1
            elif row.get("direction") in ("out", "in"):
                bucket[row["direction"]] += amount
            else:
                bucket["unknown_direction"] += 1
    return {
        key: {**value, "net": value["in"] - value["out"]}
        for key, value in sorted(result.items())
    }


def _potential_duplicate(row, event):
    if (
        row.get("status") != "success"
        or row.get("source") != event["source"]
        or row.get("direction") != event["direction"]
    ):
        return False
    if (
        row.get("account_last4")
        and event["account_last4"]
        and row["account_last4"] != event["account_last4"]
    ):
        return False
    if event["reference_id"] and row.get("reference_id") == event["reference_id"]:
        return True
    return (
        (not row.get("reference_id") or not event["reference_id"])
        and _same_money(row, event)
        and (row.get("merchant") or "").lower() == (event.get("merchant") or "").lower()
    )


def build_report(raw_emails, transactions, notifications=(), coverage=()):
    baseline = {t["transaction_id"]: deepcopy(t) for t in transactions}
    staged = deepcopy(baseline)
    by_email = defaultdict(set)
    for row in transactions:
        by_email[row["raw_email_id"]].add(row["transaction_id"])
    for notification in notifications:
        by_email[notification["raw_email_id"]].add(notification["transaction_id"])
    parsed = []
    for raw in raw_emails:
        email = dict(raw, id=raw["gmail_message_id"])
        try:
            event = extract_transaction(email)
        except Exception as exc:
            event = {
                "raw_email_id": email["id"],
                "review_reasons": [f"Parser error: {type(exc).__name__}"],
            }
        parsed.append((raw, event))

    entries = []
    pending_new = []
    proposed_updates = {}
    for raw, event in parsed:
        raw_id = raw["gmail_message_id"]
        ids = sorted(by_email.get(raw_id, ()))
        entry = {
            "message_ref": hashlib.sha256(raw_id.encode()).hexdigest()[:12],
            "raw_email_id": raw_id,
            "existing_transaction_ids": ids,
            "candidate": {key: event.get(key) for key in FIELDS},
            "parser_version": PARSER_VERSION,
            "text_source": event.get("text_source"),
            "reasons": event.get("review_reasons", []),
            "changes": {},
            "action": "pending",
        }
        entries.append(entry)
        if entry["reasons"]:
            entry["action"] = "hold_existing" if ids else "review_unrecorded"
            entry["existing"] = [baseline[i] for i in ids if i in baseline]
            continue
        if len(ids) > 1:
            entry.update(
                action="hold_existing",
                reasons=["Multiple stored events share this notification"],
            )
            continue
        if ids:
            old = baseline.get(ids[0])
            if not old:
                entry.update(
                    action="hold_existing",
                    reasons=["Notification references a missing event"],
                )
                continue
            proposed = {
                **old,
                **{k: event.get(k) for k in FIELDS},
                "parser_version": PARSER_VERSION,
                "text_source": event["text_source"],
            }
            # Classification and user notes remain exactly as stored.
            proposed.update({key: old.get(key) for key in PRESERVED})
            entry["preserved_classification"] = {key: old.get(key) for key in PRESERVED}
            entry["existing"] = {key: old.get(key) for key in FIELDS}
            entry["changes"] = {
                key: {"before": old.get(key), "after": event.get(key)}
                for key in FIELDS
                if not _equal(old.get(key), event.get(key))
            }
            # Decimal representation alone is not a financial change.
            if _money(old.get("amount")) == _money(event.get("amount")):
                entry["changes"].pop("amount", None)
            entry["action"] = (
                (
                    "propose_update"
                    if set(entry["changes"])
                    & {"amount", "date", "currency", "direction", "status"}
                    else "propose_metadata_update"
                )
                if entry["changes"]
                else "unchanged"
            )
            if ids[0] in proposed_updates and proposed_updates[ids[0]] != proposed:
                entry.update(
                    action="hold_existing",
                    reasons=["Mapped notifications propose conflicting repairs"],
                )
                for previous in entries:
                    if ids[0] in previous["existing_transaction_ids"]:
                        previous.update(
                            action="hold_existing", reasons=entry["reasons"]
                        )
                proposed_updates[ids[0]] = None
            elif ids[0] not in proposed_updates or proposed_updates[ids[0]] is not None:
                proposed_updates[ids[0]] = proposed
            continue
        pending_new.append((entry, event))

    # Any ambiguous notification for a shared event prevents a repair proposal
    # from silently overriding the uncertainty from another linked notification.
    held_ids = {
        tid
        for entry in entries
        if entry["action"] == "hold_existing"
        for tid in entry["existing_transaction_ids"]
    }
    for entry in entries:
        if entry["action"] in (
            "propose_update",
            "propose_metadata_update",
            "unchanged",
        ) and held_ids.intersection(entry["existing_transaction_ids"]):
            entry.update(
                action="hold_existing",
                reasons=["Another linked notification needs review"],
            )
    for tid, proposal in proposed_updates.items():
        if proposal is not None and tid not in held_ids:
            staged[tid] = proposal

    for entry, event in pending_new:
        candidates = [
            row for row in staged.values() if _potential_duplicate(row, event)
        ]
        if candidates:
            exact = [
                row
                for row in candidates
                if event["direction"] == "out"
                and event["reference_id"]
                and event["account_last4"]
                and row.get("account_last4") == event["account_last4"]
                and row.get("reference_id") == event["reference_id"]
                and _same_money(row, event)
            ]
            if len(candidates) == len(exact) == 1:
                entry.update(
                    action="duplicate_notice",
                    linked_transaction_id=exact[0]["transaction_id"],
                )
            else:
                entry.update(
                    action="review_unrecorded",
                    reasons=["Possible duplicate or reference collision"],
                    duplicate_candidates=[row["transaction_id"] for row in candidates],
                )
            continue
        tid = generate_transaction_id(event)
        if tid in staged:
            entry.update(
                action="review_unrecorded",
                reasons=["Generated identity conflicts with a staged event"],
            )
            continue
        entry.update(action="propose_insert", proposed_transaction_id=tid)
        staged[tid] = {
            **event,
            "transaction_id": tid,
            "category": None,
            "classification_source": None,
            "classification_confidence": None,
        }

    # Flag existing duplicate candidates without automatically removing either
    # record. This report's total excludes no uncertain historical event.
    duplicate_groups = []
    keys = defaultdict(list)
    for row in staged.values():
        if row.get("reference_id") and row.get("account_last4"):
            key = (
                row.get("source"),
                row["account_last4"],
                row["reference_id"],
                row.get("direction"),
            )
            keys[key].append(row["transaction_id"])
    for group in keys.values():
        if len(group) > 1:
            duplicate_groups.append(group)

    links = []
    for row in staged.values():
        if row.get("direction") != "in" or row.get("event_kind") not in (
            "refund",
            "reversal",
        ):
            continue
        if not row.get("reference_id") or not row.get("account_last4"):
            continue
        matches = [
            other
            for other in staged.values()
            if other.get("direction") == "out"
            and all(
                other.get(k) == row.get(k)
                for k in ("source", "account_last4", "reference_id", "currency")
            )
            and _money(other.get("amount")) is not None
            and _money(row.get("amount")) is not None
            and _money(other["amount"]) >= _money(row["amount"])
            and other.get("date")
            and row.get("date")
            and str(other["date"]) <= str(row["date"])
        ]
        if len(matches) == 1:
            links.append(
                {
                    "transaction_id": row["transaction_id"],
                    "original_transaction_id": matches[0]["transaction_id"],
                }
            )

    before, after = totals(baseline.values()), totals(staged.values())
    deltas = {}
    for key in sorted(before.keys() | after.keys()):
        deltas[key] = {
            field: after.get(key, {}).get(field, 0) - before.get(key, {}).get(field, 0)
            for field in (
                "out",
                "in",
                "net",
                "count",
                "missing_amount",
                "unknown_direction",
            )
        }
    covered_ids = {raw["gmail_message_id"] for raw in raw_emails}
    orphaned = [
        t["transaction_id"]
        for t in transactions
        if t["raw_email_id"] not in covered_ids
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "parser_version": PARSER_VERSION,
        "parser_sha256": hashlib.sha256(
            Path(__file__).with_name("transaction_event.py").read_bytes()
        ).hexdigest(),
        "snapshot_sha256": hashlib.sha256(
            json.dumps(
                [raw_emails, transactions, list(notifications)],
                default=str,
                sort_keys=True,
            ).encode()
        ).hexdigest(),
        "raw_email_count": len(raw_emails),
        "stored_transaction_count": len(transactions),
        "staged_transaction_count": len(staged),
        "actions": dict(Counter(e["action"] for e in entries)),
        "before": before,
        "staged": after,
        "delta": deltas,
        "entries": entries,
        "proposed_links": links,
        "duplicate_groups_for_review": duplicate_groups,
        "transactions_without_raw_evidence": orphaned,
        "recorded_coverage": list(coverage),
        "policy": "Proposals only. Held records remain in totals. Classifications and notes preserved. No changes applied.",
    }


def read_snapshot():
    conn = get_connection()
    conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET LOCAL statement_timeout='30s'")
            cur.execute("SHOW transaction_read_only")
            if cur.fetchone()["transaction_read_only"] != "on":
                raise RuntimeError("Read-only snapshot required")
            cur.execute(
                "SELECT * FROM raw_emails ORDER BY received_at, gmail_message_id"
            )
            raw = [dict(r) for r in cur.fetchall()]
            cur.execute("SELECT * FROM transactions ORDER BY id")
            transactions = [dict(r) for r in cur.fetchall()]
            optional = {}
            for table in ("transaction_notifications", "ingestion_checkpoints"):
                cur.execute("SELECT to_regclass(%s) AS name", (table,))
                if cur.fetchone()["name"] is None:
                    optional[table] = []
                else:
                    # Table names are fixed literals above, never user input.
                    order_key = (
                        "raw_email_id"
                        if table == "transaction_notifications"
                        else "scope"
                    )
                    cur.execute(f"SELECT * FROM {table} ORDER BY {order_key}")
                    optional[table] = [dict(r) for r in cur.fetchall()]
            return (
                raw,
                transactions,
                optional["transaction_notifications"],
                optional["ingestion_checkpoints"],
            )
    finally:
        conn.rollback()
        conn.close()


def markdown_report(report):
    lines = [
        "# Staged historical repair report",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "**Read-only preview. No database records were changed.**",
        "",
        f"Reparsed {report['raw_email_count']} stored emails against {report['stored_transaction_count']} transaction rows.",
        "This is stored-email coverage, not a fresh reconciliation against Gmail or bank statements.",
        "",
        "## Proposed actions",
        "",
        "| Action | Notifications |",
        "|---|---:|",
    ]
    lines.extend(
        f'| {action.replace("_", " ")} | {count} |'
        for action, count in sorted(report["actions"].items())
    )
    changed_fields = Counter(
        key for entry in report["entries"] for key in entry["changes"]
    )
    lines += [
        "",
        "Changed fields proposed: "
        + ", ".join(f"{key} ({count})" for key, count in changed_fields.most_common())
        + ".",
        f"Staged transaction rows: {report['staged_transaction_count']}.",
        "Uncertain existing records remain unchanged in the staged totals. Uncertain new records are excluded.",
        "Classification, confidence, source, and notes are preserved on proposed updates.",
        "",
        "## Current and staged totals",
        "",
        "These are captured direction totals by stored currency, not economic spending, income, or savings.",
        "",
        "| Currency / period | Current out | Staged out | Δ out | Current in | Staged in | Δ net |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for key, delta in report["delta"].items():
        before, after = report["before"].get(key, {}), report["staged"].get(key, {})
        lines.append(
            f"| {key} | {before.get('out',0):,.2f} | {after.get('out',0):,.2f} | {delta['out']:+,.2f} | {before.get('in',0):,.2f} | {after.get('in',0):,.2f} | {delta['net']:+,.2f} |"
        )
    lines += [
        "",
        "## Review queue",
        "",
        "The detailed JSON contains exact old/new values and original record IDs; this summary uses hashed message references.",
        "",
        "| Message reference | Action | Changed fields / review reason |",
        "|---|---|---|",
    ]
    for entry in report["entries"]:
        if entry["action"] in ("unchanged", "propose_metadata_update"):
            continue
        details = (
            ", ".join(entry["changes"])
            or "; ".join(entry["reasons"])
            or "New notification mapping or event"
        )
        lines.append(
            f"| {entry['message_ref']} | {entry['action'].replace('_',' ')} | {details.replace('|','/')} |"
        )
    reasons = Counter(
        reason for entry in report["entries"] for reason in entry["reasons"]
    )
    lines += ["", "## Review reasons", ""]
    lines.extend(f"- {reason}: {count}" for reason, count in reasons.most_common())
    lines += [
        "",
        "## Limits and next steps",
        "",
        f"- Proposed refund/reversal links: {len(report['proposed_links'])}.",
        f"- Existing reference groups needing duplicate review: {len(report['duplicate_groups_for_review'])}.",
        f"- Transactions without stored raw evidence: {len(report['transactions_without_raw_evidence'])}.",
        "- No verified ingestion coverage is inferred from email dates. "
        + (
            "Recorded checkpoints are in the JSON."
            if report["recorded_coverage"]
            else "No completion checkpoints were present."
        ),
        "- Missing or invalid amounts are tracked in JSON; totals cannot quantify them.",
        "- Review flags describe uncertainty under the current parser, not proof that existing financial records are wrong.",
        "- Metadata-only proposals are summarized above; their exact old/new values are in report.json.",
        "- Resolve held records and duplicate candidates before approving any backfill.",
        "- A future apply step must recheck the source snapshot and preserve manual corrections. This tool has no apply mode.",
        f"- Parser: {report['parser_version']}; SHA-256: {report['parser_sha256']}.",
        "",
    ]
    return "\n".join(lines)


def write_report(report, output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    for name, content in [
        ("report.json", json.dumps(report, default=str, indent=2)),
        ("REPORT.md", markdown_report(report)),
    ]:
        path = output / name
        with path.open("x") as stream:
            path.chmod(0o600)
            stream.write(content + "\n")
    return output / "REPORT.md"
