"""
Shared display-formatting helpers.

Split out from dashboard_service.py because anomaly_detector.py and
nlq_service.py both need format_currency but dashboard_service.py also
imports anomaly_detector.detect_anomalies — importing format_currency
straight from dashboard_service.py created a circular import that was
being worked around with function-local imports. This module has no
dependencies on the rest of the app, so nothing can cycle through it.
"""


def display_merchant(value):
    """Drop a leading UPI handle from "handle@bank NAME" strings; leave anything else untouched."""
    if not isinstance(value, str):
        return value
    parts = value.strip().split(None, 1)
    if len(parts) == 2 and "@" in parts[0]:
        return parts[1]
    return value


def format_currency(amount, show_sign=False) -> str:
    """Format amount in Indian numbering system (1,23,456.00)."""
    if amount is None:
        return "0.00"
    val = float(amount)
    negative = val < 0
    val = abs(val)

    integer_part = int(val)
    decimal_part = f"{val - integer_part:.2f}"[1:]  # .XX

    s = str(integer_part)
    if len(s) > 3:
        last3 = s[-3:]
        rest = s[:-3]
        groups = []
        while rest:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        result = ",".join(groups) + "," + last3
    else:
        result = s

    formatted = result + decimal_part

    if show_sign:
        if negative:
            return f"-₹{formatted}"
        else:
            return f"+₹{formatted}"
    elif negative:
        return f"-₹{formatted}"
    return f"₹{formatted}"
