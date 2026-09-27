"""Durable pipeline attempts, completion checkpoints and processing outcomes."""

from contextlib import contextmanager
from datetime import date, datetime, timedelta
import hashlib
import json
import os
from zoneinfo import ZoneInfo

from psycopg2.extras import Json, RealDictCursor

from app.db_connection import get_connection


class IncompleteRun(RuntimeError):
    """Persisted work is valid, but the requested scope was not completed."""


def pipeline_timezone():
    return ZoneInfo(os.environ.get("EXPENSO_TIMEZONE", "Asia/Kolkata"))


def pipeline_today():
    return datetime.now(pipeline_timezone()).date()


def ingestion_scope(senders, keywords):
    if isinstance(senders, str):
        senders = [senders]
    contract = {
        "senders": sorted(senders),
        "keywords": sorted(keywords or []),
        "timezone": os.environ.get("EXPENSO_TIMEZONE", "Asia/Kolkata"),
        "version": 1,
    }
    return hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()


def get_checkpoint(scope):
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT * FROM ingestion_checkpoints WHERE scope=%s", (scope,))
            row = cur.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def pending_ingestion_window(scope):
    """Keep interrupted/failed first scans retryable as calendar days advance."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """SELECT MIN(r.window_start) AS start, MAX(r.window_end) AS finish,
                COUNT(*) AS count FROM pipeline_runs r
                WHERE r.stage='ingestion' AND r.scope=%s AND r.status != 'success'
                  AND r.window_start IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM pipeline_runs completed WHERE completed.stage='ingestion'
                      AND completed.scope=r.scope AND completed.status='success' AND completed.id > r.id
                      AND completed.window_start <= r.window_start AND completed.window_end >= r.window_end
                  )""",
                (scope,),
            )
            return dict(cur.fetchone())
    finally:
        conn.close()


def incremental_window(scope, *, today=None, overlap_days=7, initial_days=30):
    if overlap_days < 1 or initial_days < 1:
        raise ValueError("Lookback periods must be positive")
    today = today or pipeline_today()
    end = today - timedelta(days=1)
    checkpoint = get_checkpoint(scope)
    start = (
        checkpoint["covered_through"] - timedelta(days=overlap_days - 1)
        if checkpoint
        else end - timedelta(days=initial_days - 1)
    )
    pending = pending_ingestion_window(scope)
    if pending["start"]:
        start = min(start, pending["start"])
    # Future-dated manual ranges must never cause incremental ingestion to stop.
    return min(start, end - timedelta(days=overlap_days - 1)), end


@contextmanager
def ingestion_lock(scope):
    """A session lock prevents overlapping runs from racing the same checkpoint."""
    conn = get_connection()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s)", (int(scope[:15], 16),))
            if not cur.fetchone()[0]:
                raise IncompleteRun("An ingestion run for this scope is already active")
        yield
    finally:
        # Closing releases the session lock, including after process failure.
        conn.close()


def _finish(run_id, status, counts, error_code=None, checkpoint=None):
    conn = get_connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE pipeline_runs SET status=%s, counts=%s,
                error_code=%s, finished_at=NOW() WHERE id=%s""",
                (status, Json(counts), error_code, run_id),
            )
            if checkpoint and status == "success":
                scope, start, end = checkpoint
                # Disconnected historical windows are recorded as successful
                # runs but cannot claim continuous coverage across a gap.
                cur.execute(
                    """INSERT INTO ingestion_checkpoints
                    (scope, coverage_start, covered_through, run_id)
                    VALUES (%s,%s,%s,%s) ON CONFLICT (scope) DO UPDATE SET
                    coverage_start=LEAST(ingestion_checkpoints.coverage_start, EXCLUDED.coverage_start),
                    covered_through=GREATEST(ingestion_checkpoints.covered_through, EXCLUDED.covered_through),
                    last_success_at=NOW(), run_id=EXCLUDED.run_id
                    WHERE EXCLUDED.coverage_start <= ingestion_checkpoints.covered_through + 1
                      AND EXCLUDED.covered_through >= ingestion_checkpoints.coverage_start - 1""",
                    (scope, start, end, run_id),
                )
    finally:
        conn.close()


@contextmanager
def stage_run(stage, *, scope=None, start=None, end=None, advance_checkpoint=False):
    counts = {}
    conn = get_connection()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO pipeline_runs(stage, scope, window_start, window_end)
                VALUES (%s,%s,%s,%s) RETURNING id""",
                (stage, scope, start, end),
            )
            run_id = cur.fetchone()[0]
    finally:
        conn.close()
    try:
        yield counts
    except BaseException as exc:
        counts["errors"] = counts.get("errors", 0) + 1
        if "fetched" in counts:
            counts["unpersisted"] = (
                counts["fetched"] - counts.get("stored", 0) - counts.get("skipped", 0)
            )
        try:
            _finish(
                run_id,
                "incomplete" if isinstance(exc, IncompleteRun) else "failed",
                counts,
                type(exc).__name__,
            )
        except Exception:
            # The original failure still escapes; a stale 'running' row is not success.
            import logging

            logging.getLogger(__name__).exception("Could not record pipeline failure")
        raise
    else:
        _finish(
            run_id,
            "success",
            counts,
            checkpoint=(scope, start, end) if advance_checkpoint else None,
        )


def record_outcome(conn, event, outcome, error_code=None):
    from app.transaction_event import PARSER_VERSION

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO email_processing_outcomes
            (raw_email_id, parser_version, outcome, error_code) VALUES (%s,%s,%s,%s)
            ON CONFLICT (raw_email_id) DO UPDATE SET parser_version=EXCLUDED.parser_version,
            outcome=EXCLUDED.outcome, error_code=EXCLUDED.error_code,
            attempts=email_processing_outcomes.attempts+1, updated_at=NOW()""",
            (event["raw_email_id"], PARSER_VERSION, outcome, error_code),
        )
