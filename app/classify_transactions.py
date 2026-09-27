"""
Transaction Classification Engine (Phase 3)

Assigns categories to transactions using a hybrid approach:
1. Rule-based: merchant pattern matching via merchant_categories table
2. LLM-based: Claude API for unmatched merchants (with confidence scores)

Usage:
    # Classify uncategorized transactions (default)
    poetry run python app/classify_transactions.py

    # Classify with limit
    poetry run python app/classify_transactions.py --limit 20

    # Dry run (preview without writing to DB)
    poetry run python app/classify_transactions.py --dry-run

    # Reclassify everything
    poetry run python app/classify_transactions.py --all

    # Show classification stats
    poetry run python app/classify_transactions.py --stats
"""

import sys
import os

sys.path.append(os.getcwd())

import re
import json
import math
import argparse
import logging
from typing import Dict, Any, Optional


from app import db_transactions
from app.config import ANTHROPIC_MODEL, CATEGORIES, MERCHANT_PREFIX_PATTERN
from app.db_merchant_categories import (
    lookup_merchant_category,
    get_classification_stats,
)

log = logging.getLogger(__name__)

# Known prefixes added by payment gateways
MERCHANT_PREFIXES = re.compile(MERCHANT_PREFIX_PATTERN, re.IGNORECASE)

# UPI VPA domain suffixes (everything after @)
UPI_VPA_PATTERN = re.compile(r"^[^\s]+@\S+")


def normalize_merchant(merchant: str) -> str:
    """
    Normalize a merchant name for consistent matching.

    Steps:
        1. Strip whitespace, lowercase
        2. Remove payment gateway prefixes (PYU*, CAS*, etc.)
        3. For UPI VPAs: extract the human-readable business name
        4. Clean up extra whitespace

    Examples:
        "PYU*Swiggy Food" -> "swiggy food"
        "paytm-blinkit@ptybl Blinkit" -> "blinkit"
        "BOOKMYSHOW" -> "bookmyshow"
        "bharatpe907720040533@yesbankltd GOLDEN HANDI VENTURE" -> "golden handi venture"
    """
    if not merchant:
        return ""

    text = merchant.strip()

    # Remove payment gateway prefixes
    text = MERCHANT_PREFIXES.sub("", text)

    # Handle UPI VPAs: "vpa@bank BUSINESS NAME"
    # If the text starts with a VPA pattern, extract the name after it
    if UPI_VPA_PATTERN.match(text):
        parts = text.split(None, 1)  # split on first whitespace
        if len(parts) == 2:
            # Use the human-readable name after the VPA
            text = parts[1]
        else:
            # VPA only, no readable name — keep the part before @
            text = parts[0].split("@")[0]
            # Try to extract meaningful part from VPA prefix
            # e.g., "paytm-blinkit" -> "blinkit", "netflixupi" -> "netflix"
            text = re.sub(r"^paytm[-.]?", "", text)
            text = re.sub(r"\.?payu$", "", text)
            text = re.sub(r"\.?rzp$", "", text)
            text = re.sub(r"\.?eazypay$", "", text)
            text = re.sub(r"\d+$", "", text)  # trailing numbers

    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)  # collapse whitespace
    return text


def classify_transaction(transaction: Dict[str, Any]) -> Dict[str, Any]:
    """
    Classify a single transaction.

    Strategy:
        1. Incoming movements require review of their purpose
        2. Rule-based: look up normalized merchant in merchant_categories
        3. LLM fallback: call Claude for unmatched merchants

    Returns:
        {"category": str, "confidence": float, "source": str, "rationale": str}
    """
    direction = transaction.get("direction", "unknown")
    merchant = transaction.get("merchant") or ""

    # Direction alone cannot distinguish earnings from refunds or transfers.
    if direction == "in":
        return {
            "category": "Other",
            "confidence": 0.5,
            "source": "rule",
            "rationale": "Incoming movement; purpose needs review (income, refund or transfer)",
        }

    # Step 2: Normalize and look up rules
    normalized = normalize_merchant(merchant)
    log.debug(f"Normalized merchant: '{merchant}' -> '{normalized}'")

    if normalized:
        match = lookup_merchant_category(normalized)
        if match:
            confidence = float(match.get("confidence") or 0)
            if (
                not math.isfinite(confidence)
                or not 0 <= confidence <= 1
                or match["category"] not in CATEGORIES
            ):
                return {
                    "category": "Other",
                    "confidence": 0,
                    "source": "rule",
                    "rationale": "Invalid merchant rule requires review",
                }
            if match.get("source") == "llm" and match["category"] == "Transfers":
                return {
                    "category": "Other",
                    "confidence": 0.5,
                    "source": "llm",
                    "rationale": "Model-inferred transfer needs review of payment purpose",
                }
            return {
                "category": match["category"],
                "confidence": confidence,
                "source": "llm" if match.get("source") == "llm" else "rule",
                "rationale": f"Matched pattern: '{match['merchant_pattern']}'",
            }

    # Step 3: LLM classification
    amount = transaction.get("amount")
    source = transaction.get("source", "unknown")
    llm_result = classify_with_llm(merchant, normalized, amount, source)

    if llm_result:
        # A model prediction is a transaction decision, never permission to
        # create a merchant-wide rule. Rules require an explicit user choice.
        if llm_result["category"] == "Transfers":
            return {
                "category": "Other",
                "confidence": 0.5,
                "source": "llm",
                "rationale": "Payment purpose cannot be established from a name; review required",
            }
        return {
            "category": llm_result["category"],
            "confidence": llm_result["confidence"],
            "source": "llm",
            "rationale": llm_result.get("rationale", "LLM classification"),
        }

    # Fallback
    return {
        "category": "Other",
        "confidence": 0.0,
        "source": "rule",
        "rationale": "No match found and LLM unavailable",
    }


def classify_with_llm(
    merchant: str, normalized: str, amount: Optional[float], source: str
) -> Optional[Dict[str, Any]]:
    """
    Use Claude API to classify a merchant into a category.

    Returns:
        {"category": str, "confidence": float, "rationale": str} or None on failure
    """
    try:
        import anthropic
    except ImportError:
        log.warning("anthropic SDK not installed. Skipping LLM classification.")
        return None

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        log.warning("ANTHROPIC_API_KEY not set. Skipping LLM classification.")
        return None

    categories_str = ", ".join(CATEGORIES)
    prompt = f"""Classify this transaction into exactly one category.

Categories: {categories_str}

Transaction details:
- Merchant name (raw): {merchant}
- Merchant name (normalized): {normalized}
- Amount: {amount} INR
- Source: {source}

Respond with ONLY valid JSON (no markdown, no explanation outside JSON):
{{"category": "<one of the categories above>", "confidence": <0.0 to 1.0>, "rationale": "<one short sentence>"}}

Rules:
- A personal name or UPI address does not establish payment purpose. Do not infer Transfers from names.
- Use Other with confidence below 0.9 when the purpose is uncertain.
- "Food & Dining" is for restaurants, food delivery, cafes
- "Groceries" is for grocery delivery, supermarkets, kirana stores
- "Subscriptions" is for recurring digital services (Netflix, Spotify, etc.)
- Use "Other" only if truly unclassifiable
- Confidence should reflect how certain you are (0.5 = unsure, 0.9+ = very sure)"""

    try:
        with anthropic.Anthropic(
            api_key=api_key, timeout=20.0, max_retries=1
        ) as client:
            response = client.messages.create(
                model=ANTHROPIC_MODEL,
                max_tokens=150,
                messages=[{"role": "user", "content": prompt}],
            )

        text = response.content[0].text.strip()
        # Strip markdown code fences if present
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        result = json.loads(text)

        # Validate the complete response before persisting any classification.
        if not isinstance(result, dict):
            return None
        confidence = result.get("confidence")
        if (
            isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not math.isfinite(confidence)
            or not 0 <= confidence <= 1
        ):
            return None
        if not isinstance(result.get("rationale", ""), str):
            return None
        if result.get("category") not in CATEGORIES:
            log.warning(f"LLM returned invalid category: {result.get('category')}")
            return None

        return {
            "category": result["category"],
            "confidence": float(confidence),
            "rationale": result.get("rationale", "LLM classification"),
        }
    except json.JSONDecodeError as e:
        log.error(f"LLM returned invalid JSON: {e}")
        return None
    except Exception as e:
        log.error(f"LLM classification failed: {e}")
        return None


def batch_classify(
    limit: int = 1000, reclassify_all: bool = False, dry_run: bool = False
) -> Dict[str, int]:
    """
    Classify transactions in batch.

    Args:
        limit: Max transactions to process
        reclassify_all: If True, reclassify all transactions (not just uncategorized)
        dry_run: If True, print results without writing to DB
    """
    stats = {
        "total": 0,
        "rule_classified": 0,
        "llm_classified": 0,
        "failed": 0,
        "skipped": 0,
    }

    if reclassify_all:
        log.info("Reclassifying all transactions...")
        transactions = db_transactions.get_all_success_transactions(limit)
    else:
        log.info("Classifying uncategorized transactions...")
        transactions = db_transactions.get_uncategorized_transactions(limit)

    if not transactions:
        log.info("No transactions to classify.")
        return stats

    stats["total"] = len(transactions)
    log.info(f"Processing {len(transactions)} transactions...")

    for txn in transactions:
        if txn.get("classification_source") == "manual":
            stats["skipped"] += 1
            continue
        txn_id = txn["transaction_id"]
        merchant = txn.get("merchant") or "(none)"

        result = classify_transaction(txn)
        category = result["category"]
        confidence = result["confidence"]
        source = result["source"]
        rationale = result["rationale"]

        if confidence <= 0:
            # Temporary model failures must not become permanent, successful
            # classifications. Existing zero-confidence fallbacks are retried.
            stats["failed"] += 1
            continue

        if dry_run:
            print(
                f"  {merchant:50s} -> {category:20s} "
                f"(conf={confidence:.1f}, src={source}, {rationale})"
            )
            stats[f"{source}_classified"] += 1
            continue

        success = db_transactions.update_transaction_category(
            transaction_id=txn_id,
            category=category,
            confidence=confidence,
            classification_source=source,
            notes=rationale,
        )

        if success:
            stats[f"{source}_classified"] += 1
        else:
            stats["failed"] += 1

    return stats


def print_classification_stats():
    """Print classification coverage statistics."""
    stats = get_classification_stats()
    if not stats:
        print("No stats available.")
        return

    total = stats.get("total_transactions", 0)
    classified = stats.get("classified", 0)
    unclassified = stats.get("unclassified", 0)
    coverage = (classified / total * 100) if total > 0 else 0

    print("\n" + "=" * 60)
    print("CLASSIFICATION STATISTICS")
    print("=" * 60)
    print(f"Total transactions:  {total}")
    print(f"Classified:          {classified} ({coverage:.1f}%)")
    print(f"Unclassified:        {unclassified}")

    print(f"\n--- By Category ---")
    for row in stats.get("by_category", []):
        cat = row["category"]
        count = row["count"]
        spent = row.get("total_spent") or 0
        print(f"  {cat:25s}  {count:4d} txns  {spent:>12,.2f} INR")

    print(f"\n--- By Source ---")
    for row in stats.get("by_source", []):
        print(f"  {row['source']:15s}  {row['count']:4d} txns")

    print("=" * 60 + "\n")


def print_batch_stats(stats: Dict[str, int]):
    """Print batch classification results."""
    print("\n" + "=" * 60)
    print("CLASSIFICATION RESULTS")
    print("=" * 60)
    print(f"Total processed:     {stats['total']}")
    print(f"Rule-based:          {stats['rule_classified']}")
    print(f"LLM-based:           {stats['llm_classified']}")
    print(f"Failed:              {stats['failed']}")
    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(
        description="Classify transactions into spending categories"
    )
    parser.add_argument(
        "--unclassified-only",
        action="store_true",
        default=True,
        help="Classify only uncategorized transactions (default)",
    )
    parser.add_argument(
        "--all", action="store_true", help="Reclassify all transactions"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Max transactions to process (default: 1000)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview classifications without writing to DB",
    )
    parser.add_argument(
        "--stats", action="store_true", help="Show classification statistics and exit"
    )

    args = parser.parse_args()

    if args.stats:
        print_classification_stats()
        return

    if args.dry_run:
        print("\n** DRY RUN — no changes will be written to database **\n")

    stats = batch_classify(
        limit=args.limit,
        reclassify_all=args.all,
        dry_run=args.dry_run,
    )

    print_batch_stats(stats)
    if stats["failed"]:
        raise RuntimeError("Classification failures remain retryable")

    if not args.dry_run:
        print_classification_stats()


if __name__ == "__main__":
    main()
