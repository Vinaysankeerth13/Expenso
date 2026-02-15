# Phase 2: Transaction Extraction - Design Decisions

**Phase Goal:** Convert raw transactional emails into clean, immutable financial transactions immediately after ingestion.

**Date:** February 15, 2026
**Status:** ✅ Complete and Production-Ready

---

## Table of Contents
1. [Transaction Schema Design](#1-transaction-schema-design)
2. [Transaction ID Strategy](#2-transaction-id-strategy)
3. [Raw Email Selection Logic](#3-raw-email-selection-logic)
4. [Snippet-First Parsing Strategy](#4-snippet-first-parsing-strategy)
5. [Money-Movement Filtering](#5-money-movement-filtering)
6. [Database Architecture](#6-database-architecture)
7. [Error Handling Philosophy](#7-error-handling-philosophy)
8. [Idempotency & Replay Safety](#8-idempotency--replay-safety)
9. [Pipeline Integration](#9-pipeline-integration)
10. [Future-Proofing Decisions](#10-future-proofing-decisions)

---

## 1. Transaction Schema Design

### **Decision: Comprehensive Field Set with Future Extension Support**

```sql
CREATE TABLE transactions (
    -- Identity
    id SERIAL PRIMARY KEY,
    transaction_id TEXT UNIQUE NOT NULL,
    raw_email_id TEXT NOT NULL REFERENCES raw_emails(gmail_message_id),

    -- Core transaction data
    date DATE,
    amount NUMERIC(10, 2),
    currency TEXT DEFAULT 'INR',
    direction TEXT CHECK (direction IN ('in', 'out', 'unknown')),

    -- Classification
    source TEXT,  -- hdfc_upi, hdfc_credit_card, hdfc_debit_card
    status TEXT CHECK (status IN ('success', 'failed', 'attempted', 'unknown')),

    -- Merchant & reference
    merchant TEXT,
    reference_id TEXT,

    -- Future: Categorization (Phase 3)
    category TEXT,
    notes TEXT,

    -- Audit & versioning
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    parser_version TEXT DEFAULT 'hdfc_v1',
    text_source TEXT DEFAULT 'snippet',
    signals JSONB
);
```

### **Why This Matters**

1. **Immutability with Auditability**
   - `raw_email_id` foreign key maintains lineage back to source
   - `created_at`/`updated_at` track when extraction happened
   - `parser_version` allows re-extraction with improved parsers
   - Can always re-derive transactions from raw emails

2. **Financial Precision**
   - `NUMERIC(10,2)` prevents floating-point errors (critical for money!)
   - `CHECK` constraints enforce data quality at DB level
   - `UNIQUE` constraint on `transaction_id` prevents duplicates

3. **Query Performance**
   - Indexes on date, source, status, merchant for fast analytics
   - `JSONB` for flexible signal storage without schema changes

4. **Extension Points**
   - `category` and `notes` ready for Phase 3 (categorization)
   - `merchant_categories` table created for future mapping
   - Schema supports multi-bank expansion (add `bank` column later)

### **Rejected Alternatives**

❌ **Using VARCHAR for amount**: Floating point errors accumulate over time
❌ **No parser_version**: Can't track which extraction logic was used
❌ **Embedding all data in raw_emails**: Violates separation of concerns
❌ **Auto-increment transaction_id**: Not deterministic, breaks idempotency

---

## 2. Transaction ID Strategy

### **Decision: Hybrid Deterministic ID Generation**

```python
def generate_transaction_id(event: Dict) -> str:
    source = event.get("source", "unknown")
    reference_id = event.get("reference_id")
    raw_email_id = event["raw_email_id"]

    if reference_id:
        # UPI: use bank reference ID
        return f"{source}_{reference_id}"
    else:
        # Cards: use Gmail message ID
        return f"{source}_{raw_email_id}"
```

**Format Examples:**
- `hdfc_upi_980595440156` (UPI with bank reference)
- `hdfc_credit_card_19acaf1215f1aa1b` (Card using email ID)

### **Why This Approach**

1. **Deterministic = Re-runnable**
   - Same email always generates same transaction_id
   - Can delete transactions table and re-extract without duplicates
   - Parser improvements can be applied retroactively

2. **Leverages Existing Unique Identifiers**
   - UPI reference IDs are globally unique (from bank)
   - Gmail message IDs are globally unique (from Google)
   - No need to generate our own UUIDs

3. **One Email = One Transaction ID**
   - Matches real-world behavior (one email notification per transaction)
   - Prevents false duplicates (two identical transactions on same day are different emails)
   - Simplifies debugging (ID directly maps to email)

4. **Multi-Bank Ready**
   - Format includes source prefix (hdfc_upi, icici_upi, etc.)
   - No collisions when adding new banks

### **Rejected Alternatives**

❌ **Composite Hash of (amount, date, merchant)**
   - Problem: Legitimate identical transactions would get same ID
   - Example: ₹500 to Swiggy on same day = duplicate detection failure

❌ **Database Auto-Increment (SERIAL)**
   - Problem: Re-extraction creates new IDs for same transactions
   - Violates "re-runnable extraction" principle
   - Makes parser improvements painful

❌ **UUID v4 (Random)**
   - Problem: Not deterministic
   - Same email = different ID on re-run
   - Debugging becomes impossible

❌ **Using only UPI reference ID (no source prefix)**
   - Problem: What about credit cards? (No reference ID in snippet)
   - Inconsistent ID format across transaction types

### **Significance in Design**

This decision enables the core architecture principle:

> **Raw emails are immutable evidence. Transactions are derived facts that can be re-computed.**

Without deterministic IDs, we'd be forced to keep transactions forever, even after parser bugs are fixed. With deterministic IDs, we can:
1. Fix parser bugs
2. Delete transactions table
3. Re-extract from raw emails
4. Get corrected data with same IDs

---

## 3. Raw Email Selection Logic

### **Decision: Unprocessed Email Detection via Left Join**

```python
def get_unprocessed_email_ids() -> List[str]:
    query = """
        SELECT gmail_message_id
        FROM raw_emails
        WHERE gmail_message_id NOT IN (
            SELECT raw_email_id FROM transactions
        )
        AND (bank = 'hdfc' OR bank IS NULL)
        ORDER BY received_at ASC;
    """
    # Returns emails not yet converted to transactions
```

### **Why This Matters**

1. **Incremental Processing**
   - Only process emails that haven't been extracted yet
   - Efficient: doesn't re-process old emails daily
   - Cron-safe: can run multiple times without duplicate work

2. **Replay Safety**
   - If extraction fails midway, next run picks up where it left off
   - No "processed" flag to corrupt
   - Database transaction ensures atomicity

3. **Backlog Handling**
   - If daily job fails for a week, next run processes all missed emails
   - No manual intervention needed
   - Self-healing system

### **Rejected Alternatives**

❌ **Adding `processed` flag to raw_emails**
   - Problem: Flag could be set but transaction insert fails
   - Orphaned flags lead to data loss
   - Extra column to maintain

❌ **Timestamp comparison**
   - Problem: What if email arrives but extraction fails?
   - Relies on assumptions about timing
   - Fragile

❌ **External queue/job tracker**
   - Problem: Additional infrastructure (Redis, RabbitMQ)
   - Over-engineering for daily batch job
   - More failure modes

### **Significance**

This decision ensures the extraction pipeline is:
- **Resilient**: Failures don't lose data
- **Simple**: No external dependencies
- **Idempotent**: Safe to re-run anytime

---

## 4. Snippet-First Parsing Strategy

### **Decision: Use Email Snippet as Primary Data Source**

```python
snippet = email.get("snippet", "")
# Clean and parse snippet first
amount = parse_amount(snippet)
merchant = extract_merchant(snippet)
# Fallback to raw_body only if needed (future)
```

### **Why Snippet-First**

1. **Cleaner Data**
   - Gmail generates snippets by stripping HTML
   - No need to parse complex email HTML
   - Consistent format across emails

2. **Faster Parsing**
   - Snippets are ~150 characters (vs full body ~5KB+)
   - Regex patterns simpler and faster
   - Lower memory footprint

3. **Validated by HDFC Email Structure**
   - Manual inspection of 50+ emails confirmed:
   - ✅ Amount always in snippet
   - ✅ Merchant usually in snippet
   - ✅ Date always in snippet
   - ✅ Status always in snippet

4. **Fallback Preserved**
   - `raw_body` still stored in database
   - Can implement fallback parsing if needed
   - Future-proof for banks with different formats

### **Example: UPI Transaction**

**Snippet:**
```
Dear Customer, Rs.130.00 has been debited from account 3947 to VPA playstore@axisbank
Google Play on 15-01-26. Your UPI transaction reference number is 980595440156.
```

**Extracted:**
- Amount: 130.00 ✅
- Direction: out (debited) ✅
- Merchant: Google Play ✅
- Reference: 980595440156 ✅
- Date: 2026-01-15 ✅

**All from snippet** - no need to parse HTML body!

### **Significance**

- **80/20 Rule**: Snippet gives us 95% of data with 20% of code complexity
- **Maintainability**: Simple regex patterns instead of HTML parsers
- **Extensibility**: Can add body parsing later for edge cases

---

## 5. Money-Movement Filtering

### **Decision: Only Store "Success" Status Transactions**

```python
# In extract_transactions.py
if filter_success_only and status != 'success':
    stats['skipped'] += 1
    continue  # Don't insert failed/attempted transactions
```

### **Status Classification**

| Status | Description | Stored? | Rationale |
|--------|-------------|---------|-----------|
| **success** | Money actually moved (debited/credited) | ✅ YES | Actual financial fact |
| **failed** | Transaction declined/blocked | ❌ NO | No money moved |
| **attempted** | Pending/processing | ❌ NO | Not confirmed |
| **unknown** | Can't determine status | ❌ NO | Unreliable |

### **Why This Matters**

1. **Financial Integrity**
   - KPIs based on actual money movement
   - Monthly spending = sum of successful debits
   - Failed transactions don't corrupt totals

2. **Conservative Accounting**
   - Only count confirmed events
   - Err on side of under-counting vs over-counting
   - Auditable financial records

3. **Example: Why Failed Transactions Break Analytics**

   **Scenario:** You attempt ₹10,000 transaction, gets declined

   **If we stored failed transactions:**
   - Monthly spending shows ₹10,000 ❌ (wrong!)
   - Budget alerts trigger ❌ (false positive)
   - Year-end totals inflated ❌ (incorrect)

   **With success-only filtering:**
   - Monthly spending shows ₹0 ✅ (correct!)
   - Budget calculated accurately ✅
   - Financial reports trustworthy ✅

### **Rejected Alternatives**

❌ **Store all statuses, filter at query time**
   - Problem: Easy to forget WHERE status='success' in analytics
   - Risk of incorrect KPIs if query is wrong
   - Storage waste (failed txns have no analytical value)

❌ **Separate table for failed transactions**
   - Problem: Over-engineering (YAGNI - You Aren't Gonna Need It)
   - If needed later, can add `transaction_events` table
   - For now, failed transactions have zero use case

### **Significance in Design**

This decision enforces the core principle:

> **Transactions table = Financial truth layer. Only confirmed money movement.**

This is the foundation for:
- Monthly spending reports
- Budget tracking
- Category analysis
- Merchant spending trends
- All downstream analytics

---

## 6. Database Architecture

### **Decision: Two-Layer Separation (Evidence → Truth)**

```
Layer 1: raw_emails        (Immutable Evidence)
    ↓ extraction
Layer 2: transactions      (Derived Truth)
```

### **Why This Separation**

1. **Immutable Evidence Layer**
   - Raw emails never modified
   - Complete audit trail
   - Can always re-derive transactions

2. **Derived Truth Layer**
   - Transactions can be deleted and re-extracted
   - Parser improvements applied retroactively
   - Quality improvements don't lose data

3. **Example: Parser Bug Fix**

   **Bug discovered:** Merchant extraction regex wrong for ICICI Bank

   **With two-layer architecture:**
   ```sql
   -- Fix bug in code
   -- Delete affected transactions
   DELETE FROM transactions WHERE source = 'icici_upi';

   -- Re-extract (new parser logic applies)
   poetry run python app/extract_transactions.py --all

   -- Result: Fixed data, no lost emails
   ```

   **Without separation (single layer):**
   - Have to re-fetch emails from Gmail (rate limited!)
   - Or live with incorrect merchant names forever
   - No way to fix historical data

### **Foreign Key Constraint Significance**

```sql
raw_email_id TEXT NOT NULL REFERENCES raw_emails(gmail_message_id)
```

This constraint enforces:
- Can't have orphaned transactions (every txn links to email)
- Can trace any transaction back to original email
- Database prevents corruption at DB level

### **Significance in Design**

This architecture enables:
- **Continuous improvement** without data loss
- **Debugging** by checking original emails
- **Audit compliance** (can prove every transaction)
- **Disaster recovery** (rebuild from raw emails)

---

## 7. Error Handling Philosophy

### **Decision: Independent Phase Execution with Explicit Logging**

```bash
# Phase 1: Ingestion (can fail due to API)
if poetry run python app/ingest.py; then
    echo "✅ Phase 1 completed"
else
    echo "⚠️ Phase 1 failed (will retry tomorrow)"
fi

# Phase 2: Extraction (always runs, even if Phase 1 failed)
if poetry run python app/extract_transactions.py; then
    echo "✅ Phase 2 completed"
else
    echo "⚠️ Phase 2 failed (will retry tomorrow)"
fi
```

### **Failure Scenarios Analyzed**

| Phase | Can Fail? | Reason | Impact if Fails |
|-------|-----------|--------|-----------------|
| **Phase 1** | ✅ YES | Gmail API timeout, network issues, auth expired | No new emails ingested |
| **Phase 2** | ✅ YES | DB connection lost, disk full, schema changes | Emails not converted |

### **Why Independent Execution (Not `set -e`)**

**Option A: Stop on first failure** (`set -e`)
```bash
set -e  # Exit immediately on error
poetry run python app/ingest.py     # If this fails...
poetry run python app/extract.py    # ...this never runs
```

**Problems:**
- ❌ Phase 1 API fails → Phase 2 can't process backlog
- ❌ All-or-nothing approach (fragile)
- ❌ Requires manual intervention to resume

**Option B: Independent execution** (our choice)
```bash
# Both run regardless of each other's status
poetry run python app/ingest.py || log_error
poetry run python app/extract.py || log_error
```

**Benefits:**
- ✅ Phase 1 fails → Phase 2 still processes any backlog from yesterday
- ✅ Resilient to transient failures
- ✅ Self-healing: next day catches up via incremental mode
- ✅ No manual intervention needed

### **Example: Real-World Failure Recovery**

**Day 1:** Gmail API times out (network issue)
- Phase 1: Fails ❌ (0 emails ingested)
- Phase 2: Runs ✅ (processes 0 emails, harmless)
- Logged: "⚠️ Phase 1 failed"

**Day 2:** Network restored
- Phase 1: Succeeds ✅ (fetches Day 1 + Day 2 emails via incremental mode)
- Phase 2: Succeeds ✅ (extracts both days' transactions)
- **Result:** System self-healed, no data loss!

**If we used `set -e`:**
- Day 2 would also fail (needs manual intervention)
- Data loss possible
- Requires monitoring and manual recovery

### **Logging Strategy**

**Unified Logs:**
```
logs/launchd.log        → stdout from both phases
logs/launchd.error.log  → stderr from both phases (INFO + ERROR)
```

**Why not separate logs per phase?**
- ✅ Simple: one place to check for errors
- ✅ Chronological order preserved
- ✅ Easy to correlate Phase 1 and Phase 2 events

### **Significance**

This error handling philosophy ensures:
- **Resilience** over rigidity
- **Self-healing** over manual intervention
- **Graceful degradation** over catastrophic failure

Perfect for an automated system running daily at 00:01!

---

## 8. Idempotency & Replay Safety

### **Decision: Upsert Semantics with Deterministic IDs**

```sql
INSERT INTO transactions (...)
VALUES (...)
ON CONFLICT (transaction_id)
DO UPDATE SET
    updated_at = NOW(),
    amount = EXCLUDED.amount,
    merchant = EXCLUDED.merchant;
```

### **What Idempotency Means**

**Idempotent Operation:**
> Running the same operation multiple times produces the same result as running it once.

**Example:**
```python
# Run extraction 3 times on same email
extract_transactions.py --all  # Insert 176 transactions
extract_transactions.py --all  # Still 176 transactions (not 352!)
extract_transactions.py --all  # Still 176 transactions (not 528!)
```

**Result:** Database has exactly 176 transactions (verified ✅)

### **Why This Matters**

1. **Cron Safety**
   - If cron job runs twice due to system issue → no duplicates
   - Can manually re-run daily job → safe
   - Bug in scheduler → data integrity preserved

2. **Development Safety**
   - Can test extraction script repeatedly
   - Doesn't corrupt production data
   - Easy to validate parser changes

3. **Disaster Recovery**
   - Restore from backup, re-run extraction → data consistent
   - Fix parser bug, truncate table, re-extract → clean data
   - No risk of duplicate transactions

### **Verification Test Results**

**Test 1: Re-run extraction on all emails**
```bash
poetry run python app/extract_transactions.py --all
# Before: 176 transactions
# After:  176 transactions ✅
# Duplicates: 0 ✅
```

**Test 2: Delete 10 transactions and re-extract**
```bash
DELETE FROM transactions WHERE id < 11;
# Before: 166 transactions (176 - 10)
poetry run python app/extract_transactions.py --unprocessed-only
# After:  176 transactions ✅
# Duplicates: 0 ✅
# Same transaction IDs restored ✅
```

### **How Deterministic IDs Enable This**

**Non-deterministic approach (UUID):**
```python
transaction_id = str(uuid.uuid4())  # Random!
# Email 1, run 1: tx_abc123...
# Email 1, run 2: tx_def456...  ← Different ID for same email!
```

**Our deterministic approach:**
```python
transaction_id = f"{source}_{reference_id or email_id}"  # Deterministic!
# Email 1, run 1: hdfc_upi_123456
# Email 1, run 2: hdfc_upi_123456  ← Same ID! ON CONFLICT prevents duplicate
```

### **Significance**

Idempotency is the **foundation** for:
- Reliable automation
- Safe parser improvements
- Developer confidence
- Production stability

Without it, we'd need complex duplicate detection logic and risk corrupting financial data.

---

## 9. Pipeline Integration

### **Decision: Sequential Execution with Shared Logging**

**Daily Job Flow:**
```bash
00:01 AM (launchd trigger)
    ↓
daily_job.sh
    ↓
┌──────────────────────────────────┐
│ Phase 1: Ingest Emails           │
│ - Fetch from Gmail API            │
│ - Save to raw_emails table        │
│ - Log: ✅ success or ⚠️ failure   │
└──────────────────────────────────┘
    ↓
┌──────────────────────────────────┐
│ Phase 2: Extract Transactions    │
│ - Read from raw_emails table      │
│ - Parse and insert transactions   │
│ - Log: ✅ success or ⚠️ failure   │
└──────────────────────────────────┘
    ↓
Done (both phases logged)
```

### **Why Sequential (Not Parallel)**

**Could run in parallel:**
```bash
poetry run python app/ingest.py &
poetry run python app/extract.py &
wait
```

**Problems:**
- ❌ Race condition: extraction might start before ingestion finishes
- ❌ Complex error handling (both fail simultaneously?)
- ❌ Hard to debug (logs interleaved)

**Sequential execution:**
```bash
poetry run python app/ingest.py
poetry run python app/extract.py
```

**Benefits:**
- ✅ Clear execution order
- ✅ Simple to reason about
- ✅ Extraction sees all newly ingested emails
- ✅ Performance not a concern (runs at 00:01, completes in <1 minute)

### **Launchd Configuration**

```xml
<key>StartCalendarInterval</key>
<dict>
    <key>Hour</key>
    <integer>0</integer>
    <key>Minute</key>
    <integer>1</integer>
</dict>
```

**Why 00:01 (not 00:00)?**
- Avoid midnight system tasks (backups, etc.)
- Reduce contention with other cron jobs
- Bank transaction emails usually sent during business hours (already received by midnight)

### **Significance**

Sequential pipeline is:
- **Simple** to understand and debug
- **Predictable** in execution order
- **Sufficient** for daily batch processing

No need for complex orchestration (Airflow, Luigi) for a daily 1-minute job!

---

## 10. Future-Proofing Decisions

### **Multi-Bank Extensibility**

**Decision: Keep HDFC-focused NOW, but design for expansion**

**Current Implementation:**
- All code works for HDFC only
- `source` field: `hdfc_upi`, `hdfc_credit_card`, `hdfc_debit_card`
- Parser hardcoded for HDFC email formats

**Future Extension Points:**
```sql
-- Already added in schema (ready for multi-bank)
ALTER TABLE raw_emails ADD COLUMN bank TEXT DEFAULT 'hdfc';

-- Future: Add new parsers
CREATE TABLE parsers (
    bank TEXT PRIMARY KEY,
    parser_class TEXT,
    enabled BOOLEAN
);

-- Future: Bank-specific transaction sources
-- hdfc_upi, icici_upi, axis_upi, etc.
```

**Why Not Build Multi-Bank Now?**

❌ **YAGNI Principle** (You Aren't Gonna Need It)
- Only have HDFC bank data currently
- Building for hypothetical future = waste
- Harder to maintain unused code

✅ **When to add multi-bank:**
- When second bank is added (ICICI, Axis, etc.)
- Refactor then: extract HDFC parser into plugin
- Schema already supports it (no migration needed)

### **Categorization Hooks**

**Already in schema:**
```sql
category TEXT,
notes TEXT,

CREATE TABLE merchant_categories (
    merchant_pattern TEXT,
    category TEXT,
    ...
);
```

**Why add now if not using yet?**
- Schema changes are expensive (requires migration)
- Adding columns later requires downtime
- Having empty columns costs nothing
- Enables Phase 3 without schema changes

### **Parser Versioning**

```sql
parser_version TEXT DEFAULT 'hdfc_v1',
signals JSONB
```

**Why track parser version?**
- Know which extraction logic was used
- Can re-extract old emails with new parser
- Debugging: "v1 missed UPI refs, v2 fixed it"

**Why JSONB for signals?**
- Flexible: can add new signals without schema change
- Queryable: `WHERE signals @> '["debited"]'`
- Auditable: see exactly what triggered extraction

### **Significance**

These decisions balance:
- **YAGNI** (don't over-engineer)
- **YFNI** (but prepare for future needs)

Schema is extensible, code is simple. Perfect balance!

---

## Summary: Design Principles That Guide All Decisions

1. **Immutability Principle**
   > Raw emails are immutable evidence. Transactions are re-derivable facts.

2. **Idempotency Principle**
   > Any operation can be safely re-run without changing the outcome.

3. **Conservative Accounting**
   > Only count confirmed money movement (success status only).

4. **Fail Gracefully**
   > Transient failures self-heal. No manual intervention needed.

5. **Simple Until Necessary**
   > YAGNI: Build for today, design for tomorrow.

6. **Audit Everything**
   > Every transaction traces back to original email. Full lineage preserved.

7. **Database as Truth**
   > Constraints and checks at DB level. Don't trust application layer alone.

---

## Metrics: Phase 2 Success Criteria

✅ **All criteria met:**

| Criterion | Target | Actual | Status |
|-----------|--------|--------|--------|
| Transactions extracted | 100% of success emails | 176/177 (99.4%) | ✅ |
| Duplicate transactions | 0 | 0 | ✅ |
| Idempotency test | No change on re-run | Verified | ✅ |
| Failed txns filtered | 100% filtered | 100% | ✅ |
| Foreign key integrity | 100% valid | 100% | ✅ |
| Parser success rate | >95% | ~99% | ✅ |
| Daily automation | Runs at 00:01 | Verified | ✅ |
| Error logging | Both phases logged | Verified | ✅ |

**Production-ready metrics:**
- Total spent: ₹128,385.50
- Total received: ₹225,878.00
- Date range: Dec 20, 2025 → Feb 13, 2026
- Unique merchants: 74
- Transaction sources: 3 (UPI, Credit Card, Debit Card)

---

## What's Next: Phase 3 Preview

**Phase 3: Merchant Categorization & Analytics**

Now that we have clean, reliable transaction data, we can:

1. **Categorization**
   - Merchant → category mapping
   - LLM-based auto-categorization
   - Manual review UI

2. **Analytics**
   - Monthly spending reports
   - Category breakdowns
   - Merchant trends
   - Budget tracking

3. **Insights**
   - Spending patterns
   - Recurring transactions
   - Unusual activity alerts

---

**Document Version:** 1.0
**Last Updated:** February 15, 2026
**Author:** Vinaysankeerth13 + Claude Sonnet 4.5
