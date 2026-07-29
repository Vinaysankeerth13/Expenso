"""App-wide constants shared across services and scripts."""

# Anthropic model used for both classification and NLQ
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"

# Payment-gateway prefixes stripped from merchant names before display/matching
# (e.g. "PYU*Swiggy Food" -> "Swiggy Food"). Shared by classify_transactions.py
# (merchant normalization for rule matching) and dashboard_service.py (display
# cleanup) — keep this as the one place that knows the full prefix list.
MERCHANT_PREFIX_PATTERN = r"^(PYU\*|CAS\*|RSP\*|RAZ\*|Razorpay\*)"

# All valid transaction categories in display order
CATEGORIES = [
    "Food & Dining",
    "Groceries",
    "Entertainment",
    "Subscriptions",
    "Shopping",
    "Transport & Fuel",
    "Health & Personal",
    "Transfers",
    "Income",
    "Other",
]
