# Expenso — Product Document

## Overview

Expenso is a personal finance tracking system that automatically ingests HDFC Bank transaction emails from Gmail, extracts and categorises every transaction, and presents the data through a clean web dashboard. It also lets you ask plain-English questions about your spending via a built-in AI query interface.

The system is fully automated. Once set up, it runs every night without intervention — fetching new emails, extracting transactions, classifying them by category, and surfacing anomalies when spending spikes above normal.

---

## Core Features

### 1. Automated Email Ingestion
Connects to Gmail via OAuth2 and fetches HDFC Bank transaction alert emails (both sender addresses: `alerts@hdfcbank.bank.in` and `alerts@hdfcbank.net`). Runs in incremental mode — only fetches emails since the last stored date, so no duplicates are ever created. Raw email bodies and metadata are stored in the database as an immutable audit trail.

### 2. Transaction Extraction
Parses the snippet and body of each email using regex patterns tuned for HDFC's alert format. Extracts:
- Amount (`Rs. 1,234.00` or `INR 1,234.00`)
- Date (`DD-MM-YY` or `DD Mon, YYYY`)
- Direction (debited = out, credited = in)
- Source (UPI, credit card, debit card)
- Merchant / payee name
- UPI reference ID

Only `success` transactions (actual money movement) are stored. Failed and attempted transactions are discarded. Transaction IDs are deterministic so re-running extraction is always safe.

### 3. Three-Tier Classification Engine
Every transaction gets assigned to one of 10 categories:

| Category | Examples |
|---|---|
| Food & Dining | Swiggy, restaurants, cafes |
| Groceries | Blinkit, Instamart, kirana stores |
| Entertainment | BookMyShow, cinemas |
| Subscriptions | Netflix, Apple, Claude.ai |
| Shopping | Amazon, Myntra, Uniqlo |
| Transport & Fuel | Petrol stations, IRCTC |
| Health & Personal | Apollo Pharmacy, salons |
| Transfers | Person-to-person UPI payments |
| Income | Salary credits, refunds |
| Other | Unclassifiable transactions |

**Tier 1 — Rule-based:** Exact and substring match against a curated `merchant_categories` table (74+ patterns seeded manually). Runs instantly, confidence = 1.0.

**Tier 2 — LLM fallback:** Merchants not matched by rules are sent to Claude Haiku with the merchant name, amount, and source. Returns a category + confidence score. Any result with confidence ≥ 0.9 is automatically promoted to a rule for future use.

**Tier 3 — Manual override:** Users can change a transaction's category inline from the Transactions page. The correction is immediately auto-learned as a new rule.

### 4. Web Dashboard
A server-rendered web application with three main views:

**Year View (`/year/{year}`)**
- Hero stat card: total annual spend, income, savings
- Variable-size spending heatmap — each month shown as a block sized and coloured by relative spend
- Category breakdown list with amounts
- Monthly bar chart: spent vs. income

**Month View (`/month/{year}/{month}`)**
- Three stat cards: total spend, month-over-month % change with directional indicator, top category share
- Spend anomaly callouts — warning banners when a category is >50% above its 3-month rolling average with >₹500 deviation (current month only)
- Daily spending bar chart
- Category donut chart with legend
- 10 most recent transactions with merchant avatars

**Transactions View (`/transactions`)**
- Summary pills: total spend, count, average
- Live search by merchant name (debounced, no page reload)
- Category filter dropdown
- Paginated table (20 per page) with inline category editing

### 5. Natural Language Query Bot (`/ask`)
Ask any question about your spending in plain English. The system translates it to SQL via Claude Haiku, executes it against the database, and returns a formatted answer.

Examples:
- *"What is my highest expense ever?"* → table with merchant, amount, date, category
- *"What is my 3-month average on Food & Dining?"* → scalar formatted as ₹X
- *"Top 5 merchants last 90 days"* → ranked table with totals and transaction counts
- *"Which month did I spend the most?"* → month name and total
- *"How much did I spend on shopping this year?"* → formatted currency total

Suggestion chips on the page provide starting points. Each answer appends to a conversation-style view (HTMX, no page reload). All generated SQL is validated before execution (SELECT-only, 8-second timeout, 50-row cap).

### 6. Spend Anomaly Detection
Runs on the current month's data. For each category, compares the month-to-date spend against the 3-month rolling average. Flags a category as anomalous when:
- Velocity > 50% above average, AND
- Absolute deviation > ₹500

Flagged categories appear as amber callout cards on the month dashboard. The threshold and lookback window are configurable.

### 7. Nightly Automation
A macOS launchd job runs `daily_job.sh` at 00:01 every night. The job executes four phases in sequence:

1. **Ingestion** — Incremental Gmail fetch (emails since last stored date)
2. **Extraction** — Parse unprocessed emails into transactions
3. **Classification** — Categorise new uncategorised transactions
4. **Health check** — Alert via macOS notification if no emails have been ingested for 3+ days

Each phase runs independently. A failure in Phase 1 does not prevent Phase 2 from processing existing backlog. Logs are written to `logs/launchd.log` and `logs/launchd.error.log`.

---

## Architecture

```
Gmail API
    │
    ▼
app/ingest.py          ← OAuth2 auth, query building, fetch with retry
    │
    ▼
raw_emails table       ← Immutable. Never modified after insert.
    │
    ▼
app/extract_transactions.py   ← Regex parsing via transaction_event.py
    │
    ▼
transactions table     ← Source of truth for all dashboard queries
    │
    ▼
app/classify_transactions.py  ← Rule → LLM → manual, auto-learning
    │
    ▼
app/anomaly_detector.py       ← Rolling-window velocity analysis
    │
    ▼
FastAPI (app/main.py)
    ├── app/routes/dashboard.py   → Jinja2 full-page renders
    ├── app/routes/api.py         → HTMX partial responses
    ├── app/services/dashboard_service.py  → Presentation layer
    └── app/services/nlq_service.py        → Text-to-SQL via Claude
```

**Key design principles:**
- **Immutability** — Raw emails are never modified. Transactions are re-derivable from them.
- **Idempotency** — All insert operations use `ON CONFLICT DO NOTHING / DO UPDATE`. Re-running any phase is always safe.
- **Conservative accounting** — Only `status = 'success'` transactions are stored.
- **Self-healing** — Incremental processing means a missed night auto-catches up the next run.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend framework | FastAPI + Uvicorn |
| Templating | Jinja2 |
| Frontend interactivity | HTMX 2.0.4 |
| CSS | Tailwind CSS (CDN) |
| Charts | Chart.js 4.4.7 |
| Database | PostgreSQL |
| DB driver | psycopg2 |
| Gmail integration | Google API Python Client (OAuth2) |
| AI (classification + NLQ) | Anthropic Claude Haiku (`claude-haiku-4-5-20251001`) |
| Scheduling | macOS launchd |
| Dependency management | Poetry |
| Language | Python 3.13 |

---

## Database Schema

### `raw_emails`
Stores every fetched email exactly as received. Never updated.

| Column | Type | Description |
|---|---|---|
| `gmail_message_id` | TEXT PK | Gmail's unique message ID |
| `thread_id` | TEXT | Gmail thread ID |
| `sender` | TEXT | From address |
| `subject` | TEXT | Email subject |
| `snippet` | TEXT | Gmail preview snippet (used for parsing) |
| `raw_body` | TEXT | Full decoded email body |
| `received_at` | TIMESTAMPTZ | Email header date |
| `source` | TEXT | Always `'gmail'` |
| `bank` | TEXT | Bank identifier, default `'hdfc'` |
| `account_last4` | TEXT | Last 4 digits of account (if present) |

### `transactions`
Every extracted, successful financial event.

| Column | Type | Description |
|---|---|---|
| `id` | SERIAL PK | Integer primary key |
| `transaction_id` | TEXT UNIQUE | Deterministic: `{source}_{reference_id or email_id}` |
| `raw_email_id` | TEXT FK | Reference back to `raw_emails` |
| `date` | DATE | Transaction date |
| `amount` | NUMERIC(10,2) | Always positive |
| `currency` | TEXT | Default `'INR'` |
| `direction` | TEXT | `'out'` (debit) or `'in'` (credit) |
| `source` | TEXT | `hdfc_upi`, `hdfc_credit_card`, `hdfc_debit_card` |
| `status` | TEXT | Always `'success'` for stored rows |
| `merchant` | TEXT | Raw payee name from email |
| `reference_id` | TEXT | UPI transaction reference |
| `category` | TEXT | One of the 10 categories |
| `classification_source` | TEXT | `'rule'`, `'llm'`, or `'manual'` |
| `classification_confidence` | FLOAT | 1.0 for rules/manual, 0.0–1.0 for LLM |
| `notes` | TEXT | LLM rationale or manual notes |
| `parser_version` | TEXT | Parser version tag |
| `signals` | JSONB | Extraction signals used |
| `created_at` / `updated_at` | TIMESTAMPTZ | Audit timestamps |

### `merchant_categories`
Lookup table for rule-based classification. Grows automatically as users manually correct categories and as LLM high-confidence results are promoted.

| Column | Type | Description |
|---|---|---|
| `merchant_pattern` | TEXT UNIQUE | Normalised pattern for substring matching |
| `category` | TEXT | Mapped category |
| `confidence` | FLOAT | Match confidence |
| `source` | TEXT | `'manual'`, `'llm'`, or `'rule'` |

---

## Project Structure

```
Expenso/
├── app/
│   ├── config.py                  # Shared constants (model name, categories)
│   ├── db_connection.py           # Single DB connection factory
│   ├── db_raw_emails.py           # raw_emails table operations
│   ├── db_transactions.py         # transactions table operations
│   ├── db_merchant_categories.py  # merchant_categories table operations
│   ├── db_dashboard.py            # Dashboard aggregate queries
│   ├── db_anomaly.py              # Anomaly detection queries
│   ├── main.py                    # FastAPI app, middleware, logging setup
│   ├── ingest.py                  # Gmail ingestion pipeline
│   ├── extract_transactions.py    # Email → transaction extraction
│   ├── transaction_event.py       # Regex parsing logic
│   ├── classify_transactions.py   # Three-tier classification engine
│   ├── anomaly_detector.py        # Spend velocity anomaly detection
│   ├── routes/
│   │   ├── dashboard.py           # Full-page routes (year, month, transactions, ask)
│   │   └── api.py                 # HTMX partial routes
│   ├── services/
│   │   ├── dashboard_service.py   # Presentation layer (formatting, data assembly)
│   │   └── nlq_service.py         # Natural language → SQL → result
│   ├── templates/
│   │   ├── base.html              # Layout, nav, footer
│   │   ├── year.html              # Annual overview page
│   │   ├── month.html             # Monthly detail page
│   │   ├── transactions.html      # Transaction browser page
│   │   ├── ask.html               # NLQ query page
│   │   └── partials/              # HTMX response fragments
│   └── static/                    # app.js, app.css
├── migrations/
│   ├── 001_create_transactions.sql
│   └── 002_add_classification_fields.sql
├── scripts/
│   ├── daily_job.sh               # Nightly pipeline orchestrator
│   ├── com.srisan.expenso.daily.plist  # launchd job definition
│   ├── check_ingestion_health.sh  # Staleness monitor with macOS alert
│   └── verify_scheduler.sh        # Scheduler status checker
├── logs/                          # launchd.log, launchd.error.log
├── samples/                       # Raw email JSON files (per-month)
└── pyproject.toml
```

---

## Setup & Running

### Prerequisites
- Python 3.13+
- PostgreSQL running locally (default: `localhost:5432`, db `expenso`)
- Google Cloud project with Gmail API enabled
- Anthropic API key

### Environment Variables (`.env`)
```
DB_NAME=expenso
DB_USER=postgres
DB_PASS=your_password
DB_HOST=localhost
DB_PORT=5432
ANTHROPIC_API_KEY=sk-ant-...
GMAIL_CREDENTIALS=./credentials.json
TOKEN_PATH=./token.json
```

### First-time Setup
```bash
# Install dependencies
poetry install

# Run database migrations
psql -U postgres -d expenso -f migrations/001_create_transactions.sql
psql -U postgres -d expenso -f migrations/002_add_classification_fields.sql

# Authenticate Gmail (opens browser on first run)
poetry run python app/ingest.py --mode monthly --year 2026 --month 1

# Extract and classify
poetry run python app/extract_transactions.py --unprocessed-only
poetry run python app/classify_transactions.py
```

### Running the Web App
```bash
poetry run uvicorn app.main:app --reload --port 8000
```
Visit `http://localhost:8000`

### Enabling the Nightly Scheduler (macOS)
```bash
cp scripts/com.srisan.expenso.daily.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.srisan.expenso.daily.plist
```
The job runs at 00:01 every night and logs to `logs/launchd.log`.

### Manual Pipeline Run
```bash
# Ingest emails since last run
poetry run python app/ingest.py --mode incremental

# Extract unprocessed emails
poetry run python app/extract_transactions.py --unprocessed-only

# Classify new transactions
poetry run python app/classify_transactions.py

# Check pipeline health
bash scripts/check_ingestion_health.sh
```

---

## Data Flow — End to End

```
HDFC sends email alert
        │
        ▼
Gmail inbox (filtered by sender address)
        │
        ▼
ingest.py — OAuth2, Gmail API query, fetch full message
        │
        ▼
raw_emails table — stored with ON CONFLICT DO NOTHING
        │
        ▼
extract_transactions.py — reads unprocessed emails
        │
        ▼
transaction_event.py — regex-parses snippet for amount,
                        date, direction, merchant, source
        │
        ▼
transactions table — only status='success' inserted,
                     deterministic ID prevents duplicates
        │
        ▼
classify_transactions.py
    ├── merchant_categories lookup (exact + substring)
    ├── Claude Haiku if no rule match
    └── Auto-promotes high-confidence LLM results to rules
        │
        ▼
Dashboard / Ask / Anomaly Detection
```

---

## AI Integration

### Classification (Claude Haiku)
Used as a fallback when no merchant rule matches. The prompt includes the merchant name, amount, and payment source. Claude returns a structured JSON response:
```json
{ "category": "Food & Dining", "confidence": 0.95, "rationale": "..." }
```
Results with confidence ≥ 0.9 are saved back to `merchant_categories` so the same merchant is never sent to the LLM again.

### Natural Language Query (Claude Haiku)
The NLQ service sends Claude the database schema, business rules (direction semantics, currency, date range), and 6 few-shot Q&A examples. Claude returns:
```json
{ "sql": "SELECT ...", "explanation": "...", "result_type": "scalar|table|none" }
```
The SQL is validated (SELECT-only, no DDL/DML keywords), capped at 50 rows, executed with an 8-second timeout, and results are formatted using the same Indian numbering currency formatter used across the dashboard.

---

## Monitoring & Reliability

| Concern | Mechanism |
|---|---|
| Duplicate emails | `ON CONFLICT (gmail_message_id) DO NOTHING` |
| Duplicate transactions | Deterministic transaction ID + `ON CONFLICT` upsert |
| API rate limits | Exponential backoff with jitter in `execute_with_retry()` |
| Gmail auth expiry | Token auto-refreshed; invalid token (e.g. `invalid_grant`) is automatically cleared and prompts the user to re-authenticate manually |
| Missed nightly runs | Incremental mode auto-catches up from last stored date |
| Pipeline failure | Each phase is independent; Phase 1 failure doesn't block Phase 2 |
| Staleness detection | Health check alerts via macOS notification if no emails for 3+ days |
| Bad NLQ queries | 8-second statement timeout + SQL allowlist validation |
| Anomaly false positives | Minimum ₹500 absolute deviation threshold prevents noise |

---

## Current Data (as of April 2026)

| Metric | Value |
|---|---|
| Transactions stored | 363 |
| Date range | Dec 20, 2025 → Apr 11, 2026 |
| Total spent | ₹3,74,160 |
| Unique merchants | 74+ |
| Merchant rules | 74+ patterns |
| Classification breakdown | 340 rule / 19 LLM / 4 manual |
| Top categories | Transfers, Food & Dining, Groceries, Shopping, Subscriptions |
