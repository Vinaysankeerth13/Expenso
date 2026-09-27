"""Disposable PostgreSQL fixture shared by storage and pipeline regressions."""

import importlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class TemporaryPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = shutil.which("initdb")
        pg_bin = Path(
            os.environ.get(
                "PG_BIN",
                (
                    str(Path(binary).parent)
                    if binary
                    else "/Applications/Postgres.app/Contents/Versions/17/bin"
                ),
            )
        )
        if not (pg_bin / "initdb").exists():
            raise unittest.SkipTest(
                "Set PG_BIN to run disposable PostgreSQL regressions"
            )
        cls.tmp = tempfile.TemporaryDirectory(prefix="expenso-test-", dir="/tmp")
        cls.addClassCleanup(cls.tmp.cleanup)
        base = Path(cls.tmp.name)
        data = base / "data"
        subprocess.run(
            [
                str(pg_bin / "initdb"),
                "-D",
                str(data),
                "-A",
                "trust",
                "-U",
                "phase1",
                "--no-locale",
                "--encoding=UTF8",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                str(pg_bin / "pg_ctl"),
                "-D",
                str(data),
                "-l",
                str(base / "pg.log"),
                "-o",
                f"-F -k {base} -p 55441 -c listen_addresses=''",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
        )
        cls.addClassCleanup(
            subprocess.run,
            [str(pg_bin / "pg_ctl"), "-D", str(data), "-m", "immediate", "-w", "stop"],
            check=True,
            capture_output=True,
        )
        from app import db_connection

        overrides = patch.multiple(
            db_connection,
            DB_NAME="postgres",
            DB_USER="phase1",
            DB_PASS="",
            DB_HOST=str(base),
            DB_PORT="55441",
        )
        overrides.start()
        cls.addClassCleanup(overrides.stop)
        cls.db = importlib.import_module("app.db_transactions")
        cls.raw = importlib.import_module("app.db_raw_emails")
        cls.connect = staticmethod(db_connection.get_connection)
        cls.execute_migrations()

    @classmethod
    def execute_migrations(cls):
        conn = cls.connect()
        try:
            with conn, conn.cursor() as cur:
                for migration in sorted((ROOT / "migrations").glob("*.sql")):
                    cur.execute(migration.read_text())
        finally:
            conn.close()

    def setUp(self):
        self.sql(
            "TRUNCATE transactions, raw_emails, pipeline_runs RESTART IDENTITY CASCADE"
        )
        self.raw.insert_raw_emails(
            [
                {
                    "id": "fixture",
                    "threadId": "thread",
                    "from": "bank@example.invalid",
                    "subject": "Synthetic",
                    "snippet": "Synthetic",
                    "raw_body": "Synthetic body",
                    "date": "1 September 2026 12:00:00 +0000",
                }
            ]
        )

    def sql(self, statement, params=()):
        conn = self.connect()
        try:
            with conn, conn.cursor() as cur:
                cur.execute(statement, params)
                return cur.fetchall() if cur.description else None
        finally:
            conn.close()

    def event(self, ident="event", **changes):
        return dict(
            transaction_id=ident,
            raw_email_id="fixture",
            date="2026-09-01",
            amount="100.00",
            currency="INR",
            direction="out",
            status="success",
            merchant="Synthetic Shop",
            **changes,
        )
