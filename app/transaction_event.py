import re
import logging
from typing import Dict, List, Optional, Any
from datetime import datetime

# Setup logging
log = logging.getLogger("transaction_event")

def parse_amount(text: str) -> Optional[float]:
    # Look for Rs. 123.45 or INR 123.45
    # Handle commas: 1,234.50
    matches = re.findall(r"(?:Rs\.?|INR)\s*([\d,]+\.?\d*)", text, re.IGNORECASE)
    if matches:
        # Take the first match usually?
        raw = matches[0].replace(",", "")
        try:
            return float(raw)
        except ValueError:
            return None
    return None

def parse_date(text: str) -> Optional[str]:
    # Formats seen: 
    # "28-11-25" -> DD-MM-YY
    # "28 Nov, 2025" -> DD Mon, YYYY
    
    # Try regex specific patterns
    # DD-MM-YY
    match_dmy = re.search(r"(\d{2}-\d{2}-\d{2})", text)
    if match_dmy:
        try:
            dt = datetime.strptime(match_dmy.group(1), "%d-%m-%y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # DD Mon, YYYY
    match_full = re.search(r"(\d{1,2}\s+[A-Za-z]{3},?\s+\d{4})", text)
    if match_full:
        try:
            raw_dt = match_full.group(1).replace(",", "")
            dt = datetime.strptime(raw_dt, "%d %b %Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
            
    return None

def extract_transaction(email: Dict) -> Dict[str, Any]:
    """
    Convert raw email JSON to TransactionEvent JSON.
    Prioritizes 'snippet'.
    """
    snippet = email.get("snippet", "")
    # Clean whitespace
    snippet = " ".join(snippet.split())
    
    signals = []
    
    # Defaults
    source = "unknown"
    status = "unknown"
    direction = "unknown"
    merchant = None
    ref_id = None
    amount = parse_amount(snippet)
    date_str = parse_date(snippet)
    
    text_processed = "snippet"

    # --- Identify Signals & Source ---
    lsnippet = snippet.lower()
    
    is_upi = "upi" in lsnippet or "vpa" in lsnippet
    is_cc = "credit card" in lsnippet
    is_dc = "debit card" in lsnippet
    
    if is_upi:
        source = "hdfc_upi"
    elif is_cc:
        source = "hdfc_credit_card"
    elif is_dc:
        source = "hdfc_debit_card"
        
    # --- Identify Direction & Status ---
    if "debited" in lsnippet:
        signals.append("debited")
        direction = "out"
        status = "success"
    elif "credited" in lsnippet:
        signals.append("credited")
        direction = "in"
        status = "success"
    elif "attempted" in lsnippet:
        signals.append("attempted")
        status = "attempted"
    elif "failed" in lsnippet or "declined" in lsnippet:
        signals.append("failed")
        status = "failed"
        
    # --- Extraction Specifics ---
    
    # 1. UPI Extraction
    if source == "hdfc_upi":
        # pattern: "debited ... to VPA (.*?) on"
        # pattern: "debited ... to (.*?) on"
        if direction == "out":
            m_vpa = re.search(r"to VPA\s+(.*?)\s+on\s+\d", snippet, re.IGNORECASE)
            if m_vpa:
                merchant = m_vpa.group(1).strip()
            else:
                # Fallback: "to (.*?) on"
                m_gen = re.search(r"\sto\s+(.*?)\s+on\s+\d", snippet, re.IGNORECASE)
                if m_gen:
                    merchant = m_gen.group(1).strip()

        # Reference number
        # "UPI transaction reference number is 114833820377"
        m_ref = re.search(r"reference number is\s+(\d+)", snippet, re.IGNORECASE)
        if m_ref:
            ref_id = m_ref.group(1)
            signals.append("upi_ref_present")

    # 2. Card Extraction
    elif source in ["hdfc_credit_card", "hdfc_debit_card"]:
        # "debited ... towards (.*?) on"
        m_merch = re.search(r"towards\s+(.*?)\s+on\s+\d", snippet, re.IGNORECASE)
        if m_merch:
            merchant = m_merch.group(1).strip()
            
    # Normalize
    if not amount:
        # maybe parse from body if really needed, but user said use snippet fallback body
        # For now, strict snippet only as requested "text_source: snippet"
        # If we failed on snippet, could verify raw_body
        pass

    return {
        "raw_email_id": email.get("id"),
        "source": source,
        "text_source": text_processed,
        "status": status,
        "amount": amount,
        "currency": "INR", # Assuming INR for HDFC default
        "direction": direction,
        "date": date_str,
        "merchant": merchant,
        "reference_id": ref_id,
        "signals": signals,
        "parser_version": "event_v1"
    }

if __name__ == "__main__":
    # Quick Test with user provided examples
    examples = [
        {
            "id": "19acb44df9c52a51",
            "snippet": "Dear Customer, Rs.20.00 has been debited from account 3947 to VPA paytmqr6u0kzd@ptys SRI BHAJRANG KIRANA AND GENERAL STORES on 28-11-25. Your UPI transaction reference number is 114833820377. If you"
        },
        {
            "id": "19acaf1215f1aa1b",
            "snippet": "Dear Customer, Greetings from HDFC Bank! Rs.544.00 is debited from your HDFC Bank Credit Card ending 1540 towards PISTA HOUSE on 28 Nov, 2025 at 20:19:42. To know your available balance, outstanding"
        }
    ]
    
    import json
    for ex in examples:
        res = extract_transaction(ex)
        print(json.dumps(res, indent=2))
