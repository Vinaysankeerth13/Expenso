"""Debug script to verify database date retrieval and date calculation logic."""
import sys
import os
sys.path.append(os.getcwd())
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
from app import db_raw_emails

print(f"Current Working Directory: {os.getcwd()}")
print(f"date.today(): {date.today()}")

try:
    last_dt = db_raw_emails.get_last_email_date()
    print(f"Last Email Date (DB): {last_dt}")
    if last_dt:
        print(f"Last Email Date (DB) .date(): {last_dt.date()}")
        start_date = last_dt.date() + timedelta(days=1)
        print(f"Calculated Start Date: {start_date}")
    else:
        print("Last Email Date (DB): None")

    yesterday = date.today() - relativedelta(days=1)
    print(f"Calculated End Date (Yesterday): {yesterday}")

except Exception as e:
    print(f"Error accessing DB: {e}")
