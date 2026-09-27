# Expenso

A fully automated personal finance tracker that ingests Bank transaction emails from Gmail, extracts and categorises every transaction, and surfaces insights through a clean web dashboard — including anomaly detection and a plain-English query interface.

---

## Dashboard

![Year Overview](docs/screenshots/Year_view.png)

*Annual overview: captured outflow and inflow, net cash flow, a month-by-month spending-intensity grid, category totals, and monthly outflow vs inflow. All screenshots use the synthetic demo database (see [Demo data](#demo-data)).*

---

## Features

### Automated Email Ingestion → Transaction Extraction
Connects to Gmail via OAuth2 and fetches every page of matching HDFC Bank alerts, with a recent overlap for late arrivals. Validated successful events enter the ledger; failed, incomplete, or ambiguous messages retain their raw evidence and processing outcome for review.

### Three-Tier Classification Engine
Every transaction is assigned to one of 10 categories:

![Classifier](docs/diagrams/Classifier.png)

- **Tier 1 — Rules:** curated substring patterns plus exact merchant rules you create. Instant, confidence = 1.0.
- **Tier 2 — Claude Haiku:** classifies merchants no rule matches. Responses are validated, and model decisions never create rules on their own. Incoming money is never assumed to be income, and names are never assumed to be transfers.
- **Tier 3 — You:** anything unclassified or below 0.9 confidence is marked **Needs review**. Fix it inline; tick **Use for all payments to this merchant** to fix every automated payment to that merchant and future ones in one edit, with a single Undo.

### Monthly Dashboard with Anomaly Callouts

![Monthly Overview](docs/screenshots/Monthly_Overview.png)

Compares each category's spend over the elapsed days of the month with the same days in the previous three months. Categories more than 50% (and ₹500) above their average are listed; jumps of 100% or more are flagged. The month-over-month change and anomaly callouts appear only when every compared period has verified scan coverage, and a banner links to transactions that need review.

![Daily Spending Trend](docs/screenshots/Daily_Spend_Trend.png)

*Days without verified coverage show as gaps, never as zero spending.*

### Transactions and the Needs Review List

![Transactions](docs/screenshots/Transactions.png)

Live search, category filter, and pagination. The **Needs review** tab lists unclassified and low-confidence decisions grouped by merchant, largest groups first, so one merchant-wide fix clears the most rows.

### Data Quality: Review Queue and Data Status
Bank alerts the parser cannot read confidently are quarantined on the **Review** page with reasons, outside all totals. After a parser update, **Retry with current parser** reprocesses a message in one click. The **Data status** page shows pipeline health, scan coverage, and backlogs.

### Natural Language Query Bot

![Ask Expenso](docs/screenshots/ask.png)

Ask anything about your spending in plain English. The system translates it to SQL via Claude Haiku, executes it against PostgreSQL, and returns a formatted answer. Supports complex multi-step analytical queries using CTEs.

![NLQ Flow](docs/diagrams/NLQ-Flow.png)

Example questions:
- *"What is my highest expense ever?"*
- *"Over the last 3 months, which three spending categories have grown the fastest, and which merchant is driving each?"*
- *"Top 5 merchants last 90 days"*
- *"Which month did I spend the most?"*

All generated SQL is validated (SELECT/WITH only, no DDL), capped at 50 rows, and executed with an 8-second timeout.

### Nightly Automation
A macOS launchd job runs ingestion, extraction, classification, and health checks at 00:01 every night. It continues useful backlog work after a failure, returns a nonzero aggregate exit status, and uses completion coverage rather than the age of the last bank email. Scheduled checks send configured email alerts on failures; manual checks are quiet unless `--notify` is supplied.

---

## System Architecture

![Pipeline](docs/diagrams/pipeline.png)

**Key design principles:**
- **Immutability** — Raw emails are never modified. Transactions are re-derivable from them.
- **Idempotency** — All inserts use `ON CONFLICT DO NOTHING / DO UPDATE`. Re-running any phase is safe.
- **Self-healing** — Incremental ingestion means a missed night auto-catches up the next run.
- **Conservative accounting** — Only `status = 'success'` transactions are ever stored.

---

## Database setup and regression tests

Create the `expenso` database, then apply migrations in filename order:

```bash
psql -U postgres -c 'CREATE DATABASE expenso;'
psql -v ON_ERROR_STOP=1 -1 -U postgres -d expenso \
  -f migrations/000_create_raw_emails.sql \
  -f migrations/001_create_transactions.sql \
  -f migrations/002_add_classification_fields.sql \
  -f migrations/003_event_validation_and_review.sql \
  -f migrations/004_pipeline_tracking.sql \
  -f migrations/005_historical_correction_audit.sql \
  -f migrations/006_classification_review.sql \
  -f migrations/007_classification_edit_batches.sql
poetry run python -m unittest discover -s tests -v
```

Python entry points load the project-root `.env` before reading configuration;
exported variables take precedence. `EXPENSO_ENV_FILE` selects an alternate file.
Database regression tests create and stop a disposable PostgreSQL cluster, using
synthetic records only. Set `PG_BIN` to its binaries if they are not on PATH;
Postgres.app 17 is also detected. Missing binaries skip the database tests.

Extraction replay preserves existing classifications, including manual edits.
Batch counts report committed input records, including updates and repeated IDs,
not newly created rows. Unassigned categories display as **Unclassified**.

### Upgrading an existing installation

For an installation that already has migrations 001 and 002, apply the additive
event-validation, pipeline-tracking, repair-audit and classification migrations
before running the updated app or job:

```bash
psql -v ON_ERROR_STOP=1 -1 -U postgres -d expenso \
  -f migrations/003_event_validation_and_review.sql \
  -f migrations/004_pipeline_tracking.sql \
  -f migrations/005_historical_correction_audit.sql \
  -f migrations/006_classification_review.sql \
  -f migrations/007_classification_edit_batches.sql
```

Do not rerun migration 002 as an upgrade step: its seed UPSERTs replace existing
merchant rules. Migration 003 preserves historical rows and adds validation for
new/updated records; existing invalid records require a separately reviewed
backfill before validating the constraint across all history.

The parser uses exact decimal amounts, explicit movement wording, complete dates,
and decoded message bodies. Incomplete, conflicting, unsupported-currency, and
possible-duplicate messages are kept on the **Review** page (`/review`) with
reasons, outside financial totals. Raw email evidence is retained. After a parser
fix, reparse an item with **Retry with current parser**, or all of them at once:

```bash
poetry run python app/extract_transactions.py --retry-review --limit 100
```

Notification mappings prevent repeated ingestion from changing totals. Bank
references are scoped to channel, account suffix, and event kind. Refunds and
reversals retain separate events and link to an unambiguous original, in either
arrival order. Similar card notices and repeated credits require review rather
than automatic merging. A matching legacy notification retains its original ID
and manual classification; conflicting reprocessing is queued without rewriting
its original financial record. This does not repair previously collapsed events
or certify that every bank template is supported.

### Classification and manual corrections

Classification decisions do not create merchant rules automatically. Incoming
movement alone does not establish income; new incoming transactions are marked
Other with review required. Unknown names are not assumed to be transfers.
Model-derived rules retain their original confidence and model provenance;
existing model rules match only the exact normalized merchant. Curated substring
rules match literal text, including `%` and `_` characters.

Category edits default to **this transaction only**. For outgoing payments, the
optional **Use for all payments to this merchant** checkbox creates an exact rule
for future payments and also recategorizes the merchant's other *automated*
payments (rule or model decisions, including unclassified ones). Payments you
categorized yourself are never changed. The checkbox shows how many payments it
will update. Transaction, batch and rule changes commit together, and each batch
row is recorded in `classification_edits` linked to the edit you made
(`batch_id`, migration 007). **Undo** restores the edited transaction, the rule,
and every batch row that has not been changed since; later edits are kept. The
Undo control appears after a successful edit (as a notice above the list for batch
edits); history browsing is not implemented.

Unclassified and low/unknown-confidence automated decisions show **Needs review**,
regardless of whether they came from a rule or the model. Manual decisions survive
both replay and `--all` classification, including concurrent automated updates.
Temporary classification failures remain retryable. `--dry-run` performs no
transaction, rule, or audit writes, but can still call the paid model for previews.
Model responses are validated and calls have explicit timeout/retry limits.

Apply migrations 006 and 007 before using these controls. They add exact/substring
rule scope and edit history; they do not rewrite stored transaction classifications.

### Comparisons and unresolved messages

Month-over-month changes and anomaly callouts compare only periods inside recorded
scan coverage. An unresolved (review or failed) message blocks comparisons only for
periods that end within 7 days before its receipt or start by the day after, since
bank alerts arrive with or shortly after the movement; a message with an unknown
receipt time blocks all comparisons.

### Pipeline completion and recovery

The normal ingestion path streams pages without a message cap. An explicit
`--max-results` cap fails as incomplete when a continuation remains; it never
advances the checkpoint. Stored pages survive a later failure, and the next run
retries the window with idempotent writes. Failed or interrupted initial windows
retain their original start date even when the next run happens days later.

Incremental scans use a seven-day overlap and initially search the last 30 complete
days if no completion checkpoint exists. Use `--overlap-days` or `--initial-days`
to change those bounds, or run explicit monthly scans to reconcile older history.
Late arrivals older than the overlap require an explicit historical rescan.
Disconnected completed windows do not imply that the gap was searched.

Dates use `EXPENSO_TIMEZONE` (default `Asia/Kolkata`) with epoch search bounds;
Gmail's date-string searches otherwise use Pacific time. See the
[Gmail filtering documentation](https://developers.google.com/workspace/gmail/api/guides/filtering)
and [pagination contract](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages/list).
Coverage means all pages of the configured sender/keyword search were persisted,
not that every account or bank template is represented.

```bash
poetry run python -m app.ingest --mode incremental
poetry run python -m app.pipeline extraction
poetry run python -m app.pipeline classification
poetry run python scripts/check_pipeline_health.py
```

`/pipeline` displays recorded stage outcomes, coverage, retryable failures, and
extraction/classification/review backlogs. `/health` is liveness; `/ready` checks
database/schema availability. A complete empty search may be healthy, while
parsing quality remains unknown without enough accepted records. The parsing
quality CLI exits 2 for insufficient evidence; the nightly job logs this as
unknown, not a failed phase. Other failures remain nonzero.

`pipeline_runs` retains per-stage attempts and counts; `email_processing_outcomes`
records accepted, duplicate, rejected, review, and failed outcomes. Transient
classification fallbacks remain retryable. Run the scheduled wrapper for durable
extraction/classification stage tracking; direct utility calls do not create stage
run records. Set `EXPENSO_PYTHON` or `EXPENSO_POETRY` for the shell runner when needed.

### Staged historical repair report

```bash
poetry run python scripts/stage_historical_repair.py
```

This reads a repeatable, read-only database snapshot, reparses stored emails, and
writes a private Markdown summary plus detailed JSON under
`reports/historical-repair/<timestamp>/` (ignored by Git; files are owner-only).
It works before migrations 003/004 are applied, performs no Gmail or AI calls,
and has no apply mode. It shows proposed changes, preserved manual corrections,
duplicate/reversal candidates, unresolved records, and current/staged totals by
currency and month. Parser and snapshot hashes identify the evidence used.
Uncertain old records remain in staged totals; uncertain new records stay out.
Review flags are not proof that the historical transaction was incorrect.

### Applying an investigated repair

`scripts/apply_reviewed_repair.py` is a separate, explicit apply tool for a reviewed
manifest; it does not automatically approve staged proposals. The manifest lists
exact raw rows, existing transaction snapshots, reviewed actions and reasons, and
expected financial fields for each confirmed or missing movement.

```bash
poetry run python scripts/apply_reviewed_repair.py path/to/manifest.json --output reports/historical-repair/reviewed/preview.json
# Use the digest printed by the preview, after reviewing its exact actions.
poetry run python scripts/apply_reviewed_repair.py path/to/manifest.json --expect-digest PREVIEW_DIGEST --output reports/historical-repair/reviewed/applied.json
```

Apply rejects changed evidence, stale previews, previously applied batches, and
nearby amount/date duplicate candidates. It applies migration 005 if needed and
commits the batch with database audit receipts in one transaction. Confirmations
preserve every existing transaction field, including manual classifications and
legacy channel metadata. Quarantine retains the row and changes only its status
to `unknown`, excluding it from successful totals; original values remain in
`historical_correction_audit`. New transactions start unclassified. The audit table
and private JSON receipts retain before/after evidence for investigation or a
separately reviewed reversal. No mailbox ingestion or paid classification runs.

Email-based repair does not establish final bank settlement or statement completeness.

## Demo data

To run the dashboard without a Gmail account or real data, build a synthetic demo
database (fictional people, invented amounts) and point the app at it:

```bash
poetry run python scripts/seed_demo.py
DB_NAME=expenso_demo poetry run uvicorn app.main:app --port 8001
```

The script only ever drops and recreates a database whose name starts with
`expenso_demo`. Comparisons and health checks are relative to today, so rerun it
before a demo. The Ask page needs `ANTHROPIC_API_KEY`.

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI + Uvicorn |
| Templating | Jinja2 |
| Frontend interactivity | HTMX 2.0.4 (no JS framework) |
| CSS | Hand-written design system (CSS variables, no framework) |
| Charts | Chart.js |
| Database | PostgreSQL + psycopg2 |
| Gmail integration | Google API Python Client (OAuth2) |
| AI | Anthropic Claude Haiku (`claude-haiku-4-5-20251001`) |
| Scheduling | macOS launchd |
| Language | Python 3.13 + Poetry |

---

> For a full technical deep-dive — schema, data flow, and architecture decisions — see [PRODUCT.md](PRODUCT.md).