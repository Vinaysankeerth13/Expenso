from dotenv import load_dotenv
import os
import base64
import re
from googleapiclient.discovery import build
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
import pickle
import pathlib

load_dotenv()

# Env vars
CREDENTIALS_PATH = os.getenv("GMAIL_CREDENTIALS")
TOKEN_PATH = os.getenv("TOKEN_PATH", "token.pickle")

# Gmail API scope
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

def get_service():
    creds = None
    if os.path.exists(TOKEN_PATH):
        with open(TOKEN_PATH, "rb") as token:
            creds = pickle.load(token)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_PATH, "wb") as token:
            pickle.dump(creds, token)
    return build("gmail", "v1", credentials=creds)

def fetch_emails(max_results=20):
    service = get_service()
    results = service.users().messages().list(userId="me", maxResults=max_results).execute()
    messages = results.get("messages", [])
    emails = []
    for m in messages:
        msg = service.users().messages().get(userId="me", id=m["id"]).execute()
        payload = msg.get("payload", {})
        parts = payload.get("parts", [])
        data = None
        if parts:
            data = parts[0]["body"].get("data")
        else:
            data = payload["body"].get("data")
        if data:
            text = base64.urlsafe_b64decode(data).decode("utf-8", errors="ignore")
            # quick cleanup
            clean_text = re.sub(r"<[^>]+>", "", text)  # remove HTML
            emails.append(clean_text.strip())
    return emails

if __name__ == "__main__":
    emails = fetch_emails(5)
    for e in emails[:3]:
        print("="*40)
        print(e[:500])  # print first 500 chars of cleaned email
