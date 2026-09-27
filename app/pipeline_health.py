"""Read-only stage health and coverage, independent of bank activity volume."""

from datetime import date, datetime, timedelta, timezone
import os
from zoneinfo import ZoneInfo

from psycopg2.extras import RealDictCursor

from app.db_connection import get_connection
from app.db_pipeline import ingestion_scope
from app.transaction_event import PARSER_VERSION


def assess_health(snapshot, *, now=None):
    now = now or datetime.now(
        ZoneInfo(os.environ.get("EXPENSO_TIMEZONE", "Asia/Kolkata"))
    )
    reasons = []
    state = "healthy"
    rank = {"healthy": 0, "unknown": 1, "incomplete": 2, "failed": 3}

    def flag(level, message):
        nonlocal state
        if rank[level] > rank[state]:
            state = level
        reasons.append(message)

    if snapshot.get("schema_missing"):
        flag("unknown", "Pipeline tracking migration has not been applied")
    else:
        coverage = snapshot.get("coverage")
        if not coverage:
            flag("unknown", "No completed ingestion coverage has been recorded")
        elif coverage["covered_through"] < now.date() - timedelta(days=1):
            flag("incomplete", "Ingestion coverage is behind the last complete day")
        elif now - coverage["last_success_at"] > timedelta(hours=36):
            flag("incomplete", "No recent complete ingestion scan")
        for stage in ("ingestion", "extraction", "classification"):
            run = snapshot.get("stages", {}).get(stage)
            if not run:
                flag("unknown", f"No recorded {stage} run")
            elif run["status"] == "running":
                flag("incomplete", f"{stage} is running or was interrupted")
            elif run["status"] != "success":
                flag(
                    "failed" if run["status"] == "failed" else "incomplete",
                    f'Latest {stage} run is {run["status"]}',
                )
            elif now - run["finished_at"] > timedelta(hours=36):
                flag("incomplete", f"{stage} has not completed recently")
        counts = snapshot.get("counts", {})
        for key, message in (
            ("extraction_backlog", "Raw messages are waiting for extraction"),
            (
                "classification_backlog",
                "Validated transactions are waiting for classification",
            ),
            ("review_backlog", "Messages require extraction review"),
            ("failed_messages", "Message processing failures remain retryable"),
            (
                "pending_ingestion_windows",
                "An earlier incomplete mailbox window needs retry",
            ),
        ):
            if counts.get(key, 0):
                flag("incomplete", message)
        if counts.get("recent_candidates", 0) and not counts.get("recent_accepted", 0):
            if counts.get("recent_rejected", 0) != counts["recent_candidates"]:
                flag(
                    "unknown",
                    "Recent candidate messages have produced no confirmed events",
                )
    counts = snapshot.get("counts", {})
    return {
        **snapshot,
        "status": state,
        "reasons": reasons,
        "activity": (
            "no_recent_candidates"
            if not counts.get("recent_candidates")
            else "candidates_present"
        ),
        "quality_evidence": (
            "insufficient_sample"
            if counts.get("recent_accepted", 0) < 5
            else "sample_available"
        ),
    }


def get_pipeline_health(scope=None):
    if scope is None:
        from app.ingest import HDFC_SENDERS, get_keywords

        scope = ingestion_scope(HDFC_SENDERS, get_keywords())
    conn = get_connection()
    conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT bool_and(to_regclass(name) IS NOT NULL) AS ready
                    FROM unnest(ARRAY['pipeline_runs','email_processing_outcomes',
                        'ingestion_checkpoints','transaction_notifications','extraction_reviews']) AS names(name)"""
            )
            if not cur.fetchone()["ready"]:
                return assess_health({"schema_missing": True})
            cur.execute(
                "SELECT coverage_start, covered_through, last_success_at FROM ingestion_checkpoints WHERE scope=%s",
                (scope,),
            )
            coverage = cur.fetchone()
            cur.execute(
                """SELECT DISTINCT ON (stage) stage, status, started_at, finished_at, counts, error_code
                FROM pipeline_runs WHERE stage != 'ingestion' OR scope=%s ORDER BY stage, id DESC""",
                (scope,),
            )
            stages = {row["stage"]: dict(row) for row in cur.fetchall()}
            cur.execute(
                """SELECT stage, MAX(finished_at) AS last_success_at FROM pipeline_runs
                WHERE status='success' AND (stage != 'ingestion' OR scope=%s) GROUP BY stage""",
                (scope,),
            )
            last_success = {
                row["stage"]: row["last_success_at"] for row in cur.fetchall()
            }
            cur.execute(
                """SELECT
                COUNT(*) AS raw_messages,
                COUNT(*) FILTER (WHERE NOT EXISTS (SELECT 1 FROM transactions t WHERE t.raw_email_id=r.gmail_message_id)
                    AND NOT EXISTS (SELECT 1 FROM transaction_notifications n WHERE n.raw_email_id=r.gmail_message_id)
                    AND NOT EXISTS (SELECT 1 FROM extraction_reviews v WHERE v.raw_email_id=r.gmail_message_id
                        AND v.parser_version=%s AND NOT EXISTS (SELECT 1 FROM email_processing_outcomes o
                            WHERE o.raw_email_id=v.raw_email_id AND o.outcome='failed'))) AS extraction_backlog,
                COUNT(*) FILTER (WHERE received_at >= NOW()-INTERVAL '7 days' AND received_at <= NOW()) AS recent_candidates,
                COUNT(*) FILTER (WHERE received_at >= NOW()-INTERVAL '7 days' AND received_at <= NOW() AND (
                    EXISTS (SELECT 1 FROM transactions t WHERE t.raw_email_id=r.gmail_message_id AND status='success') OR
                    EXISTS (SELECT 1 FROM transaction_notifications n WHERE n.raw_email_id=r.gmail_message_id))) AS recent_accepted,
                COUNT(*) FILTER (WHERE received_at >= NOW()-INTERVAL '7 days' AND received_at <= NOW() AND
                    EXISTS (SELECT 1 FROM email_processing_outcomes o WHERE o.raw_email_id=r.gmail_message_id AND outcome='rejected')) AS recent_rejected
                FROM raw_emails r WHERE bank='hdfc' OR bank IS NULL""",
                (PARSER_VERSION,),
            )
            counts = dict(cur.fetchone())
            cur.execute(
                """SELECT
                (SELECT COUNT(*) FROM extraction_reviews v WHERE NOT EXISTS
                    (SELECT 1 FROM email_processing_outcomes o WHERE o.raw_email_id=v.raw_email_id AND outcome='rejected')) AS review_backlog,
                (SELECT COUNT(*) FROM email_processing_outcomes WHERE outcome='failed') AS failed_messages,
                (SELECT COUNT(*) FROM transactions WHERE status='success' AND
                    (category IS NULL OR (classification_confidence=0 AND classification_source IS DISTINCT FROM 'manual'))) AS classification_backlog"""
            )
            counts.update(dict(cur.fetchone()))
            cur.execute(
                """SELECT COUNT(*) AS count FROM pipeline_runs r
                WHERE r.stage='ingestion' AND r.scope=%s AND r.status != 'success'
                  AND NOT EXISTS (SELECT 1 FROM pipeline_runs c WHERE c.stage='ingestion'
                    AND c.scope=r.scope AND c.status='success' AND c.id > r.id
                    AND c.window_start <= r.window_start AND c.window_end >= r.window_end)""",
                (scope,),
            )
            counts["pending_ingestion_windows"] = cur.fetchone()["count"]
            cur.execute(
                "SELECT outcome, COUNT(*) AS count FROM email_processing_outcomes GROUP BY outcome"
            )
            outcomes = {row["outcome"]: row["count"] for row in cur.fetchall()}
            return assess_health(
                {
                    "coverage": dict(coverage) if coverage else None,
                    "stages": stages,
                    "last_success": last_success,
                    "counts": counts,
                    "outcomes": outcomes,
                }
            )
    finally:
        conn.rollback()
        conn.close()
