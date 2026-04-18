"""
Natural Language Query service.
Translates a user question into SQL via Claude, validates, executes, and
returns a structured result ready for the template layer.
"""

import json
import logging
import os
import re
from datetime import date
from typing import Any, Dict, List

import anthropic
from psycopg2.extras import RealDictCursor

from app.config import ANTHROPIC_MODEL
from app.db_connection import get_connection

log = logging.getLogger(__name__)


# ── Prompt construction ──────────────────────────────────────────────────────

_SCHEMA = """
Table: transactions
  id          INTEGER   – primary key
  date        DATE      – transaction date
  amount      NUMERIC   – always positive (INR)
  direction   TEXT      – 'out' = money spent/debited, 'in' = money received/credited
  source      TEXT      – 'hdfc_upi' | 'hdfc_credit_card' | 'hdfc_debit_card'
  status      TEXT      – always 'success' for stored rows
  merchant    TEXT      – payee name (may start with PYU*, CAS*, RAZ* prefixes)
  category    TEXT      – one of:
                          'Food & Dining', 'Groceries', 'Entertainment',
                          'Subscriptions', 'Shopping', 'Transport & Fuel',
                          'Health & Personal', 'Transfers', 'Income', 'Other'
  created_at  TIMESTAMPTZ

Business rules:
  • direction='out' means an expense; direction='in' means income or a refund
  • All amounts are in INR; do NOT convert units
  • Use ILIKE for merchant matching (case-insensitive)
  • Oldest data: 2025-12-20. Current date: {today}
  • For rolling windows (e.g. "last 3 months") calculate relative to current date
"""

_FEW_SHOTS = [
    {
        "role": "user",
        "content": "What is my highest expense ever?",
    },
    {
        "role": "assistant",
        "content": json.dumps({
            "sql": "SELECT merchant, amount, date, category FROM transactions WHERE direction = 'out' ORDER BY amount DESC LIMIT 1",
            "explanation": "The single largest debit transaction in your history.",
            "result_type": "table",
        }),
    },
    {
        "role": "user",
        "content": "What is my 3-month average on Food & Dining?",
    },
    {
        "role": "assistant",
        "content": json.dumps({
            "sql": "SELECT ROUND(AVG(monthly_total), 2) AS avg_monthly FROM (SELECT DATE_TRUNC('month', date) AS month, SUM(amount) AS monthly_total FROM transactions WHERE direction = 'out' AND category = 'Food & Dining' AND date >= CURRENT_DATE - INTERVAL '3 months' GROUP BY 1) sub",
            "explanation": "Average monthly spend on Food & Dining over the last 3 months.",
            "result_type": "scalar",
        }),
    },
    {
        "role": "user",
        "content": "How much did I spend at Swiggy in total?",
    },
    {
        "role": "assistant",
        "content": json.dumps({
            "sql": "SELECT SUM(amount) AS total FROM transactions WHERE direction = 'out' AND merchant ILIKE '%swiggy%'",
            "explanation": "Total lifetime spend at Swiggy.",
            "result_type": "scalar",
        }),
    },
    {
        "role": "user",
        "content": "Top 5 merchants I spent the most on last 90 days",
    },
    {
        "role": "assistant",
        "content": json.dumps({
            "sql": "SELECT merchant, SUM(amount) AS total, COUNT(*) AS txn_count FROM transactions WHERE direction = 'out' AND date >= CURRENT_DATE - INTERVAL '90 days' GROUP BY merchant ORDER BY total DESC LIMIT 5",
            "explanation": "Top 5 merchants by total spend in the last 90 days.",
            "result_type": "table",
        }),
    },
    {
        "role": "user",
        "content": "Which month did I spend the most?",
    },
    {
        "role": "assistant",
        "content": json.dumps({
            "sql": "SELECT TO_CHAR(DATE_TRUNC('month', date), 'Mon YYYY') AS month, SUM(amount) AS total FROM transactions WHERE direction = 'out' GROUP BY DATE_TRUNC('month', date) ORDER BY total DESC LIMIT 1",
            "explanation": "The calendar month with the highest total outgoing spend.",
            "result_type": "table",
        }),
    },
    {
        "role": "user",
        "content": "How many transactions did I make in January 2026?",
    },
    {
        "role": "assistant",
        "content": json.dumps({
            "sql": "SELECT COUNT(*) AS transaction_count FROM transactions WHERE EXTRACT(YEAR FROM date) = 2026 AND EXTRACT(MONTH FROM date) = 1",
            "explanation": "Total number of transactions recorded in January 2026.",
            "result_type": "scalar",
        }),
    },
]

_SYSTEM = """You are a SQL assistant for a personal finance app backed by PostgreSQL.
Given a natural language question, return ONLY a JSON object — no markdown, no prose.

Schema:
{schema}

Output format (strict JSON, no other text):
{{
  "sql": "<SELECT statement>",
  "explanation": "<one sentence describing what the query finds>",
  "result_type": "scalar" | "table" | "none"
}}

Rules:
  • Only SELECT statements — never INSERT, UPDATE, DELETE, DROP, CREATE, ALTER, TRUNCATE
  • result_type="scalar" ONLY for pure aggregate questions: total spend, average, count, sum
    e.g. "how much did I spend on food?" → scalar
  • result_type="table" for any question asking WHICH transaction, WHICH merchant, WHICH month,
    or WHERE money was spent — even if it returns a single row.
    e.g. "highest expense", "most expensive transaction", "largest purchase" → table with
    merchant, amount, date, category columns so the user knows WHERE money went
  • result_type="none" with sql="" if the question cannot be answered from the available data
  • Always include LIMIT (max 50) for table results; omit LIMIT for scalar aggregates
  • Prefer explicit column aliases for readability
"""


def _build_messages(question: str) -> List[Dict]:
    system = _SYSTEM.format(schema=_SCHEMA.format(today=date.today().isoformat()))
    messages = _FEW_SHOTS + [{"role": "user", "content": question}]
    return system, messages


# ── SQL safety guard ─────────────────────────────────────────────────────────

_BLOCKED = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def _validate_sql(sql: str) -> str:
    """Raise ValueError if SQL looks unsafe; return normalised SQL."""
    sql = sql.strip().rstrip(";")
    if not sql.upper().startswith("SELECT"):
        raise ValueError("Only SELECT queries are allowed.")
    if _BLOCKED.search(sql):
        raise ValueError("Query contains a disallowed keyword.")
    # Enforce LIMIT cap: if no LIMIT present on non-aggregate queries, add one
    if "LIMIT" not in sql.upper():
        sql = f"{sql} LIMIT 50"
    return sql


# ── Execution ────────────────────────────────────────────────────────────────

def _execute(sql: str) -> List[Dict[str, Any]]:
    """Runs the validated SELECT query with an 8-second timeout and returns all rows."""
    conn = get_connection()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SET statement_timeout = '8s'")
            cur.execute(sql)
            return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


# ── Row formatting ───────────────────────────────────────────────────────────

def _format_row(row: Dict[str, Any]) -> List[str]:
    from decimal import Decimal
    import datetime
    from app.services.dashboard_service import format_currency

    result = []
    for col, val in row.items():
        col_lower = col.lower()
        if val is None:
            result.append("—")
        elif isinstance(val, Decimal):
            if any(k in col_lower for k in ("count", "num", "number", "txn")):
                result.append(str(int(val)))
            else:
                result.append(format_currency(val))
        elif isinstance(val, float):
            if any(k in col_lower for k in ("count", "num", "number", "txn")):
                result.append(str(int(val)))
            else:
                result.append(format_currency(val))
        elif isinstance(val, datetime.date):
            result.append(val.strftime("%-d %b, %Y") if hasattr(val, "day") else str(val))
        else:
            result.append(str(val))
    return result


# ── Public API ───────────────────────────────────────────────────────────────

def ask(question: str) -> Dict[str, Any]:
    """
    Process a natural language question and return a result dict:
      {
        "question":    str,
        "explanation": str,
        "result_type": "scalar" | "table" | "none" | "error",
        "scalar":      str | None,        # formatted value for scalar results
        "columns":     [str] | None,      # column names for table results
        "rows":        [[any]] | None,    # row data for table results
        "error":       str | None,
      }
    """
    base = {
        "question": question,
        "explanation": "",
        "result_type": "error",
        "scalar": None,
        "columns": None,
        "rows": None,
        "error": None,
    }

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        base["error"] = "ANTHROPIC_API_KEY not configured."
        return base

    # 1. Ask Claude
    try:
        system, messages = _build_messages(question)
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=512,
            system=system,
            messages=messages,
        )
        raw = response.content[0].text.strip()
        # Strip accidental markdown fences
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.error("Claude returned invalid JSON: %s", exc)
        base["error"] = "Could not parse the query response. Please try rephrasing."
        return base
    except Exception as exc:
        log.error("Claude call failed: %s", exc)
        base["error"] = "Failed to reach the AI service. Please try again."
        return base

    result_type = parsed.get("result_type", "none")
    explanation = parsed.get("explanation", "")
    sql = parsed.get("sql", "").strip()

    base["explanation"] = explanation
    base["result_type"] = result_type

    if result_type == "none" or not sql:
        return base

    # 2. Validate SQL
    try:
        sql = _validate_sql(sql)
    except ValueError as exc:
        log.warning("SQL validation failed: %s | sql=%s", exc, sql)
        base["result_type"] = "error"
        base["error"] = "The generated query was unsafe and was blocked."
        return base

    # 3. Execute
    try:
        rows_raw = _execute(sql)
    except Exception as exc:
        log.error("Query execution failed: %s | sql=%s", exc, sql)
        base["result_type"] = "error"
        base["error"] = "The query ran into a database error. Try rephrasing your question."
        return base

    if not rows_raw:
        base["result_type"] = "none"
        return base

    # 4. Format results
    from app.services.dashboard_service import format_currency

    if result_type == "scalar":
        first_row = rows_raw[0]
        val = list(first_row.values())[0]
        col_name = list(first_row.keys())[0].lower()
        # Format as count (integer) if the column name signals a count
        if any(k in col_name for k in ("count", "num", "number")):
            base["scalar"] = str(val)
        else:
            # Default: treat as currency amount
            try:
                base["scalar"] = format_currency(val)
            except Exception:
                base["scalar"] = str(val)
    else:
        # table
        base["columns"] = [c.replace("_", " ").title() for c in rows_raw[0].keys()]
        base["rows"] = [_format_row(row) for row in rows_raw]

    return base
