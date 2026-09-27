"""Conservative HDFC event parsing; uncertain notifications require review."""

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from typing import Any, Dict, Optional

PARSER_VERSION = "event_v3"
_MONEY = r"(?P<currency>INR|Rs\.?|₹|USD|EUR|GBP|AUD|CAD|SGD|AED|JPY|CHF|[$€£])\s*(?P<amount>-?\d[\d,]*(?:\.\d+)?)"
_VERB = r"(?P<verb>debited|credited|refunded|reversed)\b"
_BEFORE = re.compile(
    _MONEY + r"\s*(?:(?:has|have)\s+been\s+|is\s+|was\s+)?(?:successfully\s+)?" + _VERB,
    re.I,
)
_AFTER = re.compile(
    _VERB + r"\s+(?:(?:with|by|for|of)\s+)?(?:an?\s+amount\s+of\s+)?" + _MONEY, re.I
)
_DATE = re.compile(
    r"(?<![\w-])(?:\d{2}-\d{2}-(?:\d{4}|\d{2})|\d{1,2}\s+[A-Za-z]{3},?\s+\d{4})(?![\w-])"
)
_CARD_PURCHASE = re.compile(
    r"\bYou (?P<verb>made a transaction) of "
    + _MONEY
    + r"\s+at\s+(?P<merchant>.+?)\s+on\s+(?P<day>"
    + _DATE.pattern
    + r")\s+\d{2}:\d{2}:\d{2}\b",
    re.I,
)


class _BodyText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        self.parts.append(" ")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _text(value):
    parser = _BodyText()
    parser.feed(value or "")
    return " ".join(" ".join(parser.parts).split())


def _movement(text):
    matches = (
        list(_BEFORE.finditer(text))
        + list(_AFTER.finditer(text))
        + list(_CARD_PURCHASE.finditer(text))
    )
    # An amount followed by a second movement cannot be collapsed into one row.
    verbs = list(
        re.finditer(
            r"\b(?:debited|credited|refunded|reversed|made a transaction)\b", text, re.I
        )
    )
    if len(matches) != 1 or len(verbs) != 1:
        return None
    return matches[0]


def parse_amount(text: str) -> Optional[Decimal]:
    """Return only an amount directly attached to a single movement verb."""
    match = _movement(text)
    if not match:
        return None
    raw = match["amount"]
    if "," in raw and not re.fullmatch(
        r"-?(?:\d{1,3}(?:,\d{3})+|\d{1,2}(?:,\d{2})*,\d{3})(?:\.\d+)?", raw
    ):
        return None
    try:
        return Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None


def parse_date(text: str) -> Optional[str]:
    """Parse complete date tokens; never treat the prefix of 2026 as 2020."""
    matches = list(_DATE.finditer(text))
    if len(matches) != 1:
        return None
    token = matches[0][0].replace(",", "")
    for fmt in ("%d-%m-%Y", "%d-%m-%y", "%d %b %Y"):
        try:
            return datetime.strptime(token, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def validate_event(event: Dict, today=None) -> list[str]:
    """Fields required for an event to contribute to the INR reporting ledger."""
    reasons = []
    if event.get("status") != "success":
        reasons.append("Movement is not confirmed")
    try:
        amount = Decimal(str(event.get("amount")))
        if (
            not amount.is_finite()
            or not Decimal("0") < amount <= Decimal("99999999.99")
            or amount != amount.quantize(Decimal(".01"))
        ):
            raise ValueError
    except (InvalidOperation, ValueError, TypeError):
        reasons.append("Missing or invalid transaction amount")
    try:
        day = date.fromisoformat(str(event.get("date")))
        if day < date(2000, 1, 1) or day > (today or date.today()) + timedelta(days=1):
            raise ValueError
    except (ValueError, TypeError):
        reasons.append("Missing or implausible transaction date")
    if event.get("currency") != "INR":
        reasons.append("Unsupported or unknown currency")
    if event.get("direction") not in ("in", "out"):
        reasons.append("Unknown movement direction")
    if not event.get("raw_email_id"):
        reasons.append("Missing source notification")
    return reasons


def generate_transaction_id(event: Dict) -> str:
    """Reference-scoped event identity, with email identity as a safe fallback.

    Account suffixes are scoped to bank/channel. Unknown accounts are never
    assumed to be the same account. Separate credit occurrences may share a
    reference, so credit notifications retain their own identity and duplicate
    candidates require review instead of silently collapsing partial refunds.
    """
    reference = event.get("reference_id")
    account = event.get("account_last4")
    kind = event.get("event_kind") or (
        "debit" if event.get("direction") == "out" else "credit"
    )
    if reference and account:
        key = [event.get("source"), account, reference, event.get("direction"), kind]
        if event.get("direction") == "in":
            key.append(event["raw_email_id"])
    else:
        key = [event["raw_email_id"], event.get("direction"), kind]
    digest = hashlib.sha256(json.dumps(key, separators=(",", ":")).encode()).hexdigest()
    return f"event_v2_{digest}"


def _parse(text, email, text_source):
    # Ignore standard security/support instructions after the transaction notice.
    text = re.split(
        r"\b(?:if you|to report|for assistance)\b", text, maxsplit=1, flags=re.I
    )[0]
    lower = text.lower()
    source = (
        "hdfc_upi"
        if re.search(r"\b(?:upi|vpa)\b", lower)
        else (
            "hdfc_credit_card"
            if "credit card" in lower
            else "hdfc_debit_card" if "debit card" in lower else "unknown"
        )
    )
    status, direction, kind = "unknown", "unknown", "unknown"
    match = _movement(text)
    # Remove only established non-transaction boilerplate, not uncertainty in
    # the movement itself. In particular, pending/future movements still fail.
    status_text = re.sub(r"\bwe would like to inform you that\b", "", lower)
    status_text = re.sub(r"\byou may use\b", "", status_text)
    status_text = _DATE.sub("", status_text)  # May is also a month.
    blocked = re.search(
        r"\b(?:not|never)\s+(?:(?:been|be)\s+)?(?:debited|credited|refunded|reversed)\b|\b(?:will|would|may|should|shall)\b|\b(?:pending|processing)\b|\b(?:not|never)\s+(?:true\s+that\s+)?you made\b",
        status_text,
    )
    if re.search(r"\b(?:failed|declined|unsuccessful)\b", lower):
        status = "failed"
    elif re.search(r"\b(?:attempted|attempt)\b", lower):
        status = "attempted"
    elif match and not blocked:
        verb = match["verb"].lower()
        direction = "out" if verb in ("debited", "made a transaction") else "in"
        kind = (
            "debit"
            if direction == "out"
            else (
                "reversal"
                if re.search(r"\brevers(?:al|ed)\b", lower)
                else "refund" if re.search(r"\brefund(?:ed)?\b", lower) else "credit"
            )
        )
        status = "success"
    currency = match["currency"].upper() if match else None
    currency = "INR" if currency in ("RS", "RS.", "₹", "INR") else currency
    currency = {"€": "EUR", "£": "GBP"}.get(currency, currency)
    # Dates must belong to the movement sentence, following its verb, not a
    # statement/balance date earlier in the notification.
    date_text = text[match.end() :] if match else text
    date_text = re.split(r"[.!?](?:\s|$)", date_text, maxsplit=1)[0]
    on_date = re.search(r"\bon\s+(" + _DATE.pattern + r")", date_text, re.I)
    parsed_date = parse_date(on_date[1]) if on_date else None
    card_purchase = match and match["verb"].lower() == "made a transaction"
    if card_purchase:
        parsed_date = parse_date(match["day"])
        if source != "hdfc_credit_card":
            status = "unknown"
    if (
        not parsed_date
        and match
        and match["verb"].lower() == "credited"
        and source == "hdfc_upi"
    ):
        details = re.search(
            r"\bTransaction Details:\s*a\.\s*Date:\s*(" + _DATE.pattern + r")",
            text,
            re.I,
        )
        if details:
            parsed_date = parse_date(details[1])
    reference = re.search(
        r"\b(?:reference (?:number|no\.?)\s*(?:is\b\s*)?:?|ref(?:erence)?\s*[:#])\s*((?!is\b)[A-Za-z0-9-]*\d[A-Za-z0-9-]*)\b",
        text,
        re.I,
    )
    account = re.search(
        r"\b(?:account|a/c|(?:credit |debit )?card)\s*(?:number|no\.?)?\s*(?:ending(?: in)?\s*)?[*xX]*(\d{4})(?!\d)",
        text,
        re.I,
    )
    merchant_match = re.search(
        r"\b(?:to|towards|from)\s+VPA\s+(.*?)\s+on\s+\d", text, re.I
    )
    if not merchant_match:
        merchant_match = re.search(r"\btowards\s+(.*?)\s+on\s+\d", text, re.I)
    event = {
        "raw_email_id": email.get("id"),
        "source": source,
        "text_source": text_source,
        "status": status,
        "amount": parse_amount(text),
        "currency": currency,
        "direction": direction,
        "date": parsed_date,
        "merchant": (
            match["merchant"].strip()
            if card_purchase
            else (
                merchant_match[1].strip().replace("(", "").replace(")", "")
                if card_purchase or merchant_match
                else None
            )
        ),
        "reference_id": reference[1] if reference else None,
        "account_last4": account[1] if account else email.get("account_last4"),
        "event_kind": kind,
        "signals": [match["verb"].lower()] if match else [],
        "parser_version": PARSER_VERSION,
    }
    reasons = validate_event(event)
    if card_purchase and not account:
        reasons.append("Completed card template requires an account suffix")
    exclusion = None
    if not match:
        if re.search(r"\b(?:OTP is|is the OTP for)\b", text, re.I):
            exclusion = "Authentication request; no completed movement"
        elif re.search(
            r"\b(?:was declined|has been declined|could not be completed successfully)\b",
            text,
            re.I,
        ):
            exclusion = "Explicitly declined or unsuccessful payment"
        elif re.search(
            r"limit for Online International|Login PIN has been successfully set|regenerate your Online Password|too many wrong password attempts|successfully registered your device|biometric login has been successfully set|modified Card usage settings",
            text,
            re.I,
        ):
            exclusion = (
                "Account or security settings notification; no completed movement"
            )
    if exclusion:
        event["exclusion_reason"] = exclusion
        reasons.append(exclusion)
    received = email.get("received_at")
    if parsed_date and isinstance(received, (date, datetime)):
        received_day = received.date() if isinstance(received, datetime) else received
        if date.fromisoformat(parsed_date) > received_day + timedelta(days=1):
            reasons.append("Transaction date is after the notification date")
    if source == "unknown" or email.get("bank", "hdfc") not in ("hdfc", None):
        reasons.append("Unsupported bank or transaction template")
    if len(_DATE.findall(date_text)) > 1:
        reasons.append("Multiple dates require review")
    event["review_reasons"] = reasons
    return event


def extract_transaction(email: Dict) -> Dict[str, Any]:
    """Use a complete body when available, with explicit snippet/body conflicts."""
    snippet = _parse(_text(email.get("snippet")), email, "snippet")
    body_text = _text(email.get("raw_body"))
    if not body_text:
        return snippet
    body = _parse(body_text, email, "raw_body")
    # Never fill individual missing fields by combining unrelated text fragments.
    if not body["review_reasons"]:
        for key in (
            "amount",
            "currency",
            "direction",
            "date",
            "reference_id",
            "account_last4",
        ):
            if snippet.get(key) not in (None, "unknown") and snippet[key] != body[key]:
                body["review_reasons"].append("Snippet and full message disagree")
                break
        if snippet["status"] in ("failed", "attempted") or snippet.get(
            "exclusion_reason"
        ):
            body["review_reasons"].append("Snippet and full message disagree on status")
        return body
    # A full body with conflicting movement evidence must not be overridden by
    # a plausible truncated snippet. Non-transaction boilerplate may fall back.
    if re.search(
        r"\b(?:debited|credited|refunded|reversed|made a transaction|failed|declined)\b",
        body_text,
        re.I,
    ) or body.get("exclusion_reason"):
        if body.get("exclusion_reason") and snippet.get("amount") is not None:
            body.pop("exclusion_reason")
            body["review_reasons"].append("Snippet and full message disagree on status")
        return body
    return snippet
