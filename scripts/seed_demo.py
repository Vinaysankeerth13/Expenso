"""Build a throwaway demo database of synthetic transactions for demos and screenshots.

    poetry run python scripts/seed_demo.py
    DB_NAME=expenso_demo poetry run uvicorn app.main:app --port 8001

Every person, handle, amount and message is invented. The script drops and
recreates only a database whose name starts with 'expenso_demo'. Health checks,
comparisons and anomaly callouts are relative to today, so rerun it on the day
you record (ideally after the 10th of a month, so the current month has data).
"""

import argparse
import random
import sys
from datetime import datetime, time, timedelta
from pathlib import Path

import psycopg2
from psycopg2.extras import Json, execute_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import settings  # noqa: E402,F401  (loads .env)
from app import db_connection  # noqa: E402
from app.db_pipeline import ingestion_scope, pipeline_timezone, pipeline_today  # noqa: E402
from app.ingest import HDFC_SENDERS, get_keywords  # noqa: E402
from app.reporting_periods import month_start, windows  # noqa: E402
from app.transaction_event import PARSER_VERSION  # noqa: E402

UPI_ACCOUNT, CARD = "4821", "7702"

# (merchant string as the bank reports it, category, channel)
BRANDS = {
    "swiggy": ("swiggy.stores@axb SWIGGY", "Food & Dining", "hdfc_upi"),
    "zomato": ("zomato.order@hdfcbank ZOMATO", "Food & Dining", "hdfc_upi"),
    "starbucks": ("STARBUCKS COFFEE", "Food & Dining", "hdfc_credit_card"),
    "chaipoint": ("chaipoint@icici CHAI POINT", "Food & Dining", "hdfc_upi"),
    "bigbasket": ("bigbasket@hdfcbank BIGBASKET", "Groceries", "hdfc_upi"),
    "blinkit": ("blinkit.grofers@hdfcbank BLINKIT", "Groceries", "hdfc_upi"),
    "uber": ("UBER INDIA", "Transport & Fuel", "hdfc_credit_card"),
    "fuel": ("indianoil.fuel@sbi INDIAN OIL", "Transport & Fuel", "hdfc_upi"),
    "amazon": ("AMAZON PAY INDIA", "Shopping", "hdfc_credit_card"),
    "myntra": ("MYNTRA DESIGNS", "Shopping", "hdfc_credit_card"),
    "decathlon": ("DECATHLON SPORTS", "Shopping", "hdfc_credit_card"),
    "croma": ("CROMA RETAIL", "Shopping", "hdfc_credit_card"),
    "flights": ("MAKEMYTRIP INDIA", "Transport & Fuel", "hdfc_credit_card"),
    "ikea": ("IKEA INDIA", "Shopping", "hdfc_credit_card"),
    "netflix": ("NETFLIX.COM", "Subscriptions", "hdfc_credit_card"),
    "spotify": ("SPOTIFY INDIA", "Subscriptions", "hdfc_credit_card"),
    "googleone": ("GOOGLE ONE", "Subscriptions", "hdfc_credit_card"),
    "airtel": ("airtel.postpaid@airtel AIRTEL", "Subscriptions", "hdfc_upi"),
    "bookmyshow": ("BOOKMYSHOW", "Entertainment", "hdfc_credit_card"),
    "services": ("QUICKFIX HOME SERVICES", "Other", "hdfc_credit_card"),
    "cultfit": ("CULTFIT HEALTHIFYME", "Health & Personal", "hdfc_credit_card"),
    "apollo": ("apollo.pharmacy@icici APOLLO PHARMACY", "Health & Personal", "hdfc_upi"),
    "rent": ("suresh.k.rentals@okicici SURESH KUMAR", "Transfers", "hdfc_upi"),
    "kabir": ("kabir.sharma92@okhdfcbank KABIR SHARMA", "Transfers", "hdfc_upi"),
    "ananya": ("ananya.iyer@oksbi ANANYA IYER", "Transfers", "hdfc_upi"),
}

# Low-confidence decisions that land in Needs review: (merchant, category, confidence, count, amounts)
REVIEW = [
    ("rapido.bike@ybl RAPIDO", "Other", 0.5, 7, (90, 210)),
    ("kabir.sharma92@okhdfcbank KABIR SHARMA", "Other", 0.5, 5, (300, 1800)),
    ("ananya.iyer@oksbi ANANYA IYER", "Other", 0.5, 4, (250, 1200)),
    ("sri.sai.stores@ybl SRI SAI STORES", "Groceries", 0.6, 4, (120, 480)),
    ("nammametro@hdfcbank NAMMA METRO", "Transport & Fuel", 0.75, 3, (300, 500)),
    ("twc.blr@axisbank THIRD WAVE COFFEE", "Food & Dining", 0.8, 3, (220, 420)),
    ("chumbak@razorpay CHUMBAK", "Shopping", 0.7, 2, (690, 1490)),
    ("urbancompany@ybl URBAN COMPANY", "Other", 0.6, 1, (499, 499)),
    ("LENSKART SOLUTIONS", "Shopping", 0.7, 1, (1800, 1800)),
    ("pharmeasy@icici PHARMEASY", "Health & Personal", 0.8, 1, (640, 640)),
    ("pedals.rentals@ybl PEDALS CYCLE RENTAL", "Other", 0.5, 1, (150, 150)),
    ("snapmint@axisbank SNAPMINT", "Other", 0.4, 1, (1250, 1250)),
]


class Seeder:
    def __init__(self, today, rng):
        self.today, self.rng = today, rng
        self.yesterday = today - timedelta(days=1)
        self.start = month_start(today.year, today.month, -9)
        self.tz = pipeline_timezone()
        self.emails, self.txns = [], []

    def add(self, day, amount, merchant, category, *, direction="out", channel="hdfc_upi",
            source="rule", confidence=1.0):
        n = len(self.txns) + 1
        amount = round(float(amount), 2)
        received = datetime.combine(day, time(self.rng.randint(8, 22), self.rng.randint(0, 59)), self.tz)
        ref = str(self.rng.randint(10**11, 10**12 - 1))
        account = CARD if channel == "hdfc_credit_card" else UPI_ACCOUNT
        verb = "debited from" if direction == "out" else "credited to"
        who = f" to VPA {merchant}" if merchant and direction == "out" else ""
        text = (
            f"Rs.{amount:.2f} debited via Credit Card **{CARD} at {merchant} on {day:%d-%m-%y}."
            if channel == "hdfc_credit_card"
            else f"Rs.{amount:.2f} has been {verb} account {account}{who} on {day:%d-%m-%y}. "
            f"Your UPI transaction reference number is {ref}."
        )
        message_id = f"demo-msg-{n:05d}"
        self.emails.append((message_id, f"demo-thread-{n:05d}", "alerts@hdfcbank.bank.in",
                            "You have done a UPI txn. Check details!" if channel == "hdfc_upi"
                            else f"Rs.{amount:.2f} debited via Credit Card **{CARD}",
                            text, text, received, "hdfc", account))
        self.txns.append((f"demo_{n:05d}", message_id, day, amount, "INR", direction, channel,
                          "success", merchant, ref, category, source, confidence, PARSER_VERSION,
                          "raw_body", account, "debit" if direction == "out" else "credit", received))

    def brand(self, day, key, low, high, **kw):
        merchant, category, channel = BRANDS[key]
        self.add(day, self.rng.uniform(low, high), merchant, category, channel=channel, **kw)

    def build(self):
        rng, day = self.rng, self.start
        while day <= self.yesterday:
            dom = day.day
            if dom == 1:
                self.add(day, 118500, "ACME ANALYTICS PVT LTD", "Income", direction="in",
                         source="manual")
            if dom == 2:
                self.brand(day, "cultfit", 1499, 1499)
            if dom == 3:
                self.brand(day, "rent", 24000, 24000, source="manual")
            for key, on, price in (("netflix", 5, 649), ("spotify", 8, 119),
                                   ("googleone", 12, 130), ("airtel", 18, 599)):
                if dom == on:
                    self.brand(day, key, price, price)
            if day.weekday() == 5:
                self.brand(day, "bigbasket", 1400, 2300)
            if rng.random() < 0.3:
                self.brand(day, "blinkit", 180, 650)
            if rng.random() < 0.45:
                self.brand(day, rng.choice(("swiggy", "zomato")), 220, 620)
            if rng.random() < 0.2:
                self.brand(day, rng.choice(("starbucks", "chaipoint")), 150, 420)
            if rng.random() < 0.25:
                self.brand(day, "uber", 160, 480)
            if day.weekday() == 0 and rng.random() < 0.5:
                self.brand(day, "fuel", 400, 600)
            if rng.random() < 0.05:
                self.brand(day, "apollo", 200, 900)
            if rng.random() < 0.08:
                self.brand(day, "amazon", 350, 2800)
            if rng.random() < 0.03:
                self.brand(day, "myntra", 900, 2400)
            if rng.random() < 0.05:
                self.brand(day, "bookmyshow", 300, 800)
            if rng.random() < 0.06:
                self.brand(day, "services", 350, 1200)
            if rng.random() < 0.04:
                self.brand(day, rng.choice(("kabir", "ananya")), 200, 1500, source="manual")
            if rng.random() < 0.04:
                self.add(day, rng.uniform(150, 1200), None, "Income", direction="in", source="manual")
            # A couple of big one-offs, so the year has visible high and low months.
            if (day.month, dom) == (3, 14):
                self.brand(day, "croma", 58990, 58990)
            if (day.month, dom) == (5, 10):
                self.brand(day, "flights", 32400, 32400)
            day += timedelta(days=1)

        # Low-confidence decisions spread over the last ~100 days.
        span = 100
        for merchant, category, confidence, count, (low, high) in REVIEW:
            for i in range(count):
                offset = int(span * (i + 0.5) / count) + rng.randint(0, 3)
                self.add(self.yesterday - timedelta(days=offset), rng.uniform(low, high), merchant,
                         category, source="llm", confidence=confidence,
                         channel="hdfc_credit_card" if "@" not in merchant else "hdfc_upi")
        for i in range(3):
            self.add(self.yesterday - timedelta(days=9 + 21 * i), rng.uniform(180, 900), None, "Other",
                     direction="in", confidence=0.5)
        self.tune_current_month()

    # This month's story, whatever the date: two flagged jumps and one smaller rise.
    TARGETS = {
        "Shopping": (1.4, ["ikea"]),
        "Entertainment": (2.0, ["bookmyshow"]),
        "Food & Dining": (0.7, ["zomato", "swiggy", "zomato"]),
    }

    def tune_current_month(self):
        periods = windows(self.today.year, self.today.month,
                          {"covered_through": self.yesterday}, today=self.today, lookback=3)
        if not periods or (periods[0][1] - periods[0][0]).days < 2:
            return
        current, previous = periods[0], periods[1:]

        def spend(category, window):
            return sum(t[3] for t in self.txns if t[5] == "out" and t[10] == category
                       and window[0] <= t[2] <= window[1])

        categories = {t[10] for t in self.txns if t[5] == "out"}
        for category in sorted(categories):
            now = spend(category, current)
            average = sum(spend(category, w) for w in previous) / len(previous)
            if category in self.TARGETS:
                rise, brands = self.TARGETS[category]
                # The app also needs a jump of at least ₹500 before it flags anything.
                top_up = max(average * (1 + rise), average + 800) - now
                for i, key in enumerate(brands):
                    day = current[0] + timedelta(days=(i + 1) * (current[1] - current[0]).days // (len(brands) + 1))
                    self.brand(day, key, top_up / len(brands), top_up / len(brands))
            elif now > average * 1.35:
                # Raise the baseline so nothing else looks like a jump.
                lift = now / 1.1 - average
                merchant, channel = next((t[8], t[6]) for t in self.txns if t[10] == category and t[5] == "out")
                for start, end in previous:
                    self.add(start + (end - start) / 2, lift, merchant, category, channel=channel)


def admin(statement):
    conn = psycopg2.connect(dbname="postgres", user=db_connection.DB_USER, password=db_connection.DB_PASS,
                            host=db_connection.DB_HOST, port=db_connection.DB_PORT)
    conn.autocommit = True
    try:
        conn.cursor().execute(statement)
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="expenso_demo", help="demo database name (must start with expenso_demo)")
    parser.add_argument("--seed", type=int, default=7, help="random seed for repeatable data")
    args = parser.parse_args()
    if not args.db.startswith("expenso_demo") or not args.db.replace("_", "").isalnum():
        parser.error("Refusing: the database name must start with 'expenso_demo'")

    admin(f'DROP DATABASE IF EXISTS "{args.db}" WITH (FORCE)')
    admin(f'CREATE DATABASE "{args.db}"')
    db_connection.DB_NAME = args.db
    conn = db_connection.get_connection()
    try:
        with conn, conn.cursor() as cur:
            for migration in sorted((ROOT / "migrations").glob("*.sql")):
                cur.execute(migration.read_text())

        today = pipeline_today()
        seeder = Seeder(today, random.Random(args.seed))
        seeder.build()
        now = datetime.now(seeder.tz)
        scope = ingestion_scope(HDFC_SENDERS, get_keywords())
        with conn, conn.cursor() as cur:
            execute_values(cur, """INSERT INTO raw_emails(gmail_message_id, thread_id, sender, subject,
                snippet, raw_body, received_at, bank, account_last4) VALUES %s""", seeder.emails)
            execute_values(cur, """INSERT INTO transactions(transaction_id, raw_email_id, date, amount,
                currency, direction, source, status, merchant, reference_id, category, classification_source,
                classification_confidence, parser_version, text_source, account_last4, event_kind, created_at)
                VALUES %s""", seeder.txns)
            execute_values(cur, "INSERT INTO email_processing_outcomes(raw_email_id, parser_version, outcome) VALUES %s",
                           [(e[0], PARSER_VERSION, "accepted") for e in seeder.emails])
            # One quarantined alert for the Review page: a declined payment an older parser
            # could not interpret. Retrying it with the current parser rejects it as a
            # non-transaction, so it never enters totals. Old enough not to block this
            # month's comparisons.
            text = ("Dear Customer, your UPI payment of Rs.2450.00 to CROMA RETAIL was declined by the bank. "
                    "Please retry after some time.")
            received = datetime.combine(month_start(today.year, today.month, -5) + timedelta(days=11),
                                        time(19, 40), seeder.tz)
            cur.execute("""INSERT INTO raw_emails(gmail_message_id, thread_id, sender, subject, snippet, raw_body,
                received_at, bank, account_last4) VALUES ('demo-msg-review','demo-thread-review',
                'alerts@hdfcbank.bank.in','UPI transaction update',%s,%s,%s,'hdfc',%s)""",
                        (text, text, received, UPI_ACCOUNT))
            cur.execute("""INSERT INTO email_processing_outcomes(raw_email_id, parser_version, outcome)
                VALUES ('demo-msg-review', %s, 'review')""", (PARSER_VERSION,))
            cur.execute("""INSERT INTO extraction_reviews(raw_email_id, parser_version, reasons, candidate)
                VALUES ('demo-msg-review', %s, %s, %s)""",
                        (PARSER_VERSION, Json(["Movement is not confirmed", "Missing or implausible transaction date"]),
                         Json({"merchant": "CROMA RETAIL", "date": None, "currency": "INR", "amount": "2450.00"})))
            ran = now - timedelta(hours=1)
            run_ids = {}
            for stage in ("ingestion", "extraction", "classification"):
                cur.execute(
                    """INSERT INTO pipeline_runs(stage, scope, started_at, finished_at, status, window_start,
                    window_end, counts) VALUES (%s,%s,%s,%s,'success',%s,%s,%s) RETURNING id""",
                    (stage, scope if stage == "ingestion" else None, ran, ran + timedelta(minutes=2),
                     seeder.start if stage == "ingestion" else None,
                     seeder.yesterday if stage == "ingestion" else None, Json({"demo": True})),
                )
                run_ids[stage] = cur.fetchone()[0]
            cur.execute(
                """INSERT INTO ingestion_checkpoints(scope, coverage_start, covered_through, last_success_at, run_id)
                VALUES (%s,%s,%s,%s,%s)""",
                (scope, seeder.start, seeder.yesterday, ran + timedelta(minutes=2), run_ids["ingestion"]),
            )
    finally:
        conn.close()
    print(f"Seeded {len(seeder.txns)} synthetic transactions into '{args.db}' "
          f"({seeder.start} to {seeder.yesterday}).")
    print(f"Run: DB_NAME={args.db} poetry run uvicorn app.main:app --port 8001")


if __name__ == "__main__":
    main()
